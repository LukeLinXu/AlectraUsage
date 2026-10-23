import json
from datetime import date, datetime
from pathlib import Path

import pytest

from alectra_plans.analysis import analyze, simulate_tesla_shift
from alectra_plans.models import UsageRecord
from alectra_plans.rates import (
    compute_months,
    is_ontario_holiday,
    period_for,
    tier1_threshold,
    tou_block,
    ulo_block,
)

DATA = Path(__file__).parent / "data"


def load_records(name: str) -> list[UsageRecord]:
    payload = json.loads((DATA / name).read_text())
    entries = payload["Result"]["electricUsages"]
    return [UsageRecord.from_api(e) for e in entries]


# ---------------------------------------------------------------- period table


@pytest.mark.parametrize(
    "d,expected",
    [
        (date(2023, 10, 31), date(2022, 11, 1)),
        (date(2023, 11, 1), date(2023, 11, 1)),
        (date(2024, 6, 15), date(2023, 11, 1)),
        (date(2024, 11, 1), date(2024, 11, 1)),
        (date(2025, 6, 15), date(2024, 11, 1)),  # no May 2025 reset (annual cadence)
        (date(2026, 1, 10), date(2025, 11, 1)),
        (date(2026, 10, 23), date(2025, 11, 1)),
        (date(2026, 11, 1), date(2026, 11, 1)),
    ],
)
def test_period_for(d, expected):
    assert period_for(d).effective == expected


def test_period_for_before_table():
    assert period_for(date(2021, 4, 30)) is None


# ------------------------------------------------------------------ holidays


@pytest.mark.parametrize(
    "d,yes",
    [
        (date(2025, 7, 1), True),  # Canada Day, Tuesday
        (date(2025, 9, 1), True),  # Labour Day, Monday
        (date(2025, 12, 25), True),
        (date(2025, 12, 24), False),
        (date(2026, 12, 28), True),  # Boxing Day observed (Dec 26 = Saturday)
        (date(2026, 12, 27), False),
        (date(2025, 2, 17), True),  # Family Day
        (date(2025, 4, 18), True),  # Good Friday
    ],
)
def test_holidays(d, yes):
    assert is_ontario_holiday(d) is yes


# ------------------------------------------------------------------ blocks


def test_tou_summer_blocks():
    assert tou_block(datetime(2025, 6, 10, 13, 0)) == "on"  # Tue
    assert tou_block(datetime(2025, 6, 10, 8, 0)) == "mid"
    assert tou_block(datetime(2025, 6, 10, 18, 0)) == "mid"
    assert tou_block(datetime(2025, 6, 10, 3, 0)) == "off"
    assert tou_block(datetime(2025, 6, 14, 13, 0)) == "off"  # Saturday


def test_tou_winter_blocks():
    assert tou_block(datetime(2026, 1, 6, 8, 0)) == "on"  # Tue
    assert tou_block(datetime(2026, 1, 6, 13, 0)) == "mid"
    assert tou_block(datetime(2026, 1, 6, 18, 0)) == "on"
    assert tou_block(datetime(2026, 1, 6, 3, 0)) == "off"


def test_tou_holiday_is_off():
    assert tou_block(datetime(2025, 7, 1, 13, 0)) == "off"  # Canada Day


def test_ulo_blocks():
    assert ulo_block(datetime(2025, 6, 10, 23, 30)) == "overnight"
    assert ulo_block(datetime(2025, 6, 11, 2, 0)) == "overnight"
    assert ulo_block(datetime(2025, 6, 14, 2, 0)) == "overnight"  # Saturday too
    assert ulo_block(datetime(2025, 6, 10, 17, 0)) == "on"
    assert ulo_block(datetime(2025, 6, 10, 20, 59)) == "on"
    assert ulo_block(datetime(2025, 6, 10, 21, 0)) == "mid"
    assert ulo_block(datetime(2025, 6, 10, 12, 0)) == "mid"
    assert ulo_block(datetime(2025, 6, 14, 12, 0)) == "weekend_off"
    assert ulo_block(datetime(2025, 9, 1, 17, 0)) == "weekend_off"  # Labour Day Monday


def test_tier_thresholds():
    assert tier1_threshold(6) == 600
    assert tier1_threshold(1) == 1000


# ------------------------------------------------------------------ engine


def test_tiered_month_math():
    recs = [UsageRecord(datetime(2025, 6, d, 12), 100.0) for d in (1, 2, 3, 4, 5, 6, 7, 8)]
    recs[7].kwh = 52.4  # total 752.4 kWh
    months = compute_months(recs)
    assert len(months) == 1
    m = months[0]
    assert m.period_effective == date(2024, 11, 1)  # June 2025 -> Nov 2024 period
    assert m.threshold == 600
    expected = (600 * 9.3 + 152.4 * 11.0) / 100
    assert m.tiered_cost == pytest.approx(expected, abs=0.01)
    # June 1/7/8 2025 are Sun/Sat/Sun -> off-peak at 7.6; the rest are
    # summer weekday 12:00 -> on-peak at 15.8 (Nov-2024 period rates).
    tou = (500 * 15.8 + 252.4 * 7.6) / 100
    assert m.tou_cost == pytest.approx(tou, abs=0.01)
    # ULO at 12:00: weekday mid 12.2, weekend_off 7.6.
    ulo = (500 * 12.2 + 252.4 * 7.6) / 100
    assert m.ulo_cost == pytest.approx(ulo, abs=0.01)


def test_null_records_are_counted_but_free():
    recs = [
        UsageRecord(datetime(2025, 6, 1, 0), 5.0),
        UsageRecord(datetime(2025, 6, 1, 1), None),
    ]
    months = compute_months(recs)
    assert months[0].kwh == 5.0
    assert months[0].null_hours == 1


# ------------------------------------------------------- real data integration


def test_june_2025_against_api_amount():
    """June 2025 was billed on the Nov-2024 period: tier 1 = 9.3c.

    The API's per-hour ``amount`` field is consumption x 9.3 (tier-1 rate,
    no tiering), so the sum must match that — this validates the period
    assignment against Alectra's own billing data.
    """
    records = load_records("june_2025_usage.json")
    months = compute_months(records)
    assert [m.key for m in months] == ["2025-06"]
    m = months[0]
    assert m.period_effective == date(2024, 11, 1)
    assert m.kwh == pytest.approx(752.4, abs=0.5)
    assert m.api_amount_total == pytest.approx(m.kwh * 9.3 / 100, abs=0.05)
    # OEB-correct tiered cost (not the ~29%-inflated number in the old SUMMARY)
    assert m.tiered_cost == pytest.approx((600 * 9.3 + (m.kwh - 600) * 11.0) / 100, abs=0.01)


def test_january_2026_uses_nov_2025_period():
    records = load_records("january_2026_usage.json")
    months = compute_months(records)
    m = months[0]
    assert m.key == "2026-01"
    assert m.period_effective == date(2025, 11, 1)
    assert m.api_amount_total == pytest.approx(m.kwh * 12.0 / 100, abs=0.10)
    assert m.threshold == 1000


def test_analyze_shape():
    result = analyze(load_records("june_2025_usage.json"))
    assert len(result["months"]) == 1
    assert result["months"][0]["best"] in ("tiered", "tou", "ulo")
    assert result["totals"]["best"] in ("tiered", "tou", "ulo")
    profile = result["hourly_profile"]
    assert len(profile["weekday"]) == 24
    assert sum(profile["weekday"]) > 0


# ------------------------------------------------------------------ tesla


def test_tesla_shift_math_exact():
    """23:00-07:00 is one flat block in TOU and ULO, so the shift is exact:
    the removed kWh leave their original blocks and re-enter at the
    overnight rate; tiered is untouched."""
    records = []
    # 28 days of June 2025 (8 weekend days, 20 weekdays): 1.0 kWh baseline
    # at 03:00 plus an 11.0 kWh peak at 13:00 every day.
    for day in range(1, 29):
        records.append(UsageRecord(datetime(2025, 6, day, 3), 1.0))
        records.append(UsageRecord(datetime(2025, 6, day, 13), 11.0))
    result = simulate_tesla_shift(records, {"2025-06": 100.0})
    entry = result["months"][0]
    # Available above the 1.0 baseline: 28 x 10 kWh -> exactly 100 removable.
    assert entry["charged_kwh"] == pytest.approx(100.0, abs=0.01)

    period = period_for(date(2025, 6, 1))  # Nov-2024 rates: tou 7.6/12.2/15.8, ulo 2.8/7.6/12.2/28.4
    # Baseline month: 03:00 is TOU off / ULO overnight every day; 13:00 is
    # on-peak (TOU) and mid (ULO) on the 20 weekdays, off/weekend_off (7.6) on 8 days.
    base_tou = 28 * 1.0 * 7.6 / 100 + 20 * 11.0 * 15.8 / 100 + 8 * 11.0 * 7.6 / 100
    base_ulo = 28 * 1.0 * 2.8 / 100 + 20 * 11.0 * 12.2 / 100 + 8 * 11.0 * 7.6 / 100
    # Removal is deterministic (ties broken by input order): the first 10
    # 13:00 records, which include 3 weekend days and 7 weekdays.
    removed_tou = 3 * 10.0 * 7.6 / 100 + 7 * 10.0 * 15.8 / 100
    removed_ulo = 3 * 10.0 * 7.6 / 100 + 7 * 10.0 * 12.2 / 100
    assert entry["tou_shifted"] == pytest.approx(
        base_tou - removed_tou + 100 * period.tou.off / 100, abs=0.01
    )
    assert entry["ulo_shifted"] == pytest.approx(
        base_ulo - removed_ulo + 100 * period.ulo.overnight / 100, abs=0.01
    )
    assert result["totals"]["charged_kwh"] == pytest.approx(100.0, abs=0.01)


def test_tesla_shift_respects_baseline():
    records = [UsageRecord(datetime(2025, 6, 1, h), 1.0) for h in range(24)]
    result = simulate_tesla_shift(records, {"2025-06": 50.0})
    assert result["months"][0]["charged_kwh"] == 0.0  # nothing above the 1.0 baseline
