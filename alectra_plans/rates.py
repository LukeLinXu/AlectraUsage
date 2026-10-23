"""Ontario Regulated Price Plan (RPP) rate tables and cost engine.

All rates in cents per kWh. Sources (verified 2026-10-23):

- Current + historical rates:
  https://www.oeb.ca/rates-and-your-bill/electricity-rates
  https://www.oeb.ca/rates-and-your-bill/electricity-rates/historical-electricity-rates
- Holiday schedule (TOU & ULO treat these as weekend-priced days):
  https://alectrautilities.com/time-use-pricing

Since Nov 1, 2024 the OEB sets RPP prices once a year (effective Nov 1);
before that twice a year (May 1 / Nov 1). ULO rates exist from May 1, 2023.

The tier threshold is seasonal for residential customers:
600 kWh/month May-Oct, 1000 kWh/month Nov-Apr (stable across recent years).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import UsageRecord

TIER1_SUMMER_KWH = 600  # May-Oct
TIER1_WINTER_KWH = 1000  # Nov-Apr
SUMMER_MONTHS = frozenset({5, 6, 7, 8, 9, 10})


@dataclass(frozen=True)
class TouRates:
    off: float
    mid: float
    on: float


@dataclass(frozen=True)
class TieredRates:
    tier1: float
    tier2: float


@dataclass(frozen=True)
class UloRates:
    overnight: float  # 23:00-07:00 every day
    weekend_off: float  # weekends/holidays 07:00-23:00
    mid: float  # weekdays 07:00-16:00 and 21:00-23:00
    on: float  # weekdays 16:00-21:00


@dataclass(frozen=True)
class RppPeriod:
    effective: date
    tou: TouRates
    tiered: TieredRates
    ulo: UloRates | None  # None before ULO existed


# Newest first. Add future periods on top as the OEB announces them.
RPP_PERIODS: list[RppPeriod] = [
    RppPeriod(
        effective=date(2026, 11, 1),
        tou=TouRates(9.1, 14.6, 18.9),
        tiered=TieredRates(11.2, 13.2),
        ulo=UloRates(3.7, 9.1, 14.6, 37.7),
    ),
    RppPeriod(
        effective=date(2025, 11, 1),
        tou=TouRates(9.8, 15.7, 20.3),
        tiered=TieredRates(12.0, 14.2),
        ulo=UloRates(3.9, 9.8, 15.7, 39.1),
    ),
    RppPeriod(
        effective=date(2024, 11, 1),
        tou=TouRates(7.6, 12.2, 15.8),
        tiered=TieredRates(9.3, 11.0),
        ulo=UloRates(2.8, 7.6, 12.2, 28.4),
    ),
    RppPeriod(
        effective=date(2023, 11, 1),
        tou=TouRates(8.7, 12.2, 18.2),
        tiered=TieredRates(10.3, 12.5),
        ulo=UloRates(2.8, 8.7, 12.2, 28.6),
    ),
    RppPeriod(
        effective=date(2022, 11, 1),
        tou=TouRates(7.4, 10.2, 15.1),
        tiered=TieredRates(8.7, 10.3),
        ulo=None,  # ULO launched May 1, 2023 with 2.4/7.4/10.2/24.0
    ),
    RppPeriod(
        effective=date(2021, 5, 1),
        tou=TouRates(8.2, 11.3, 17.0),
        tiered=TieredRates(9.8, 11.5),
        ulo=None,  # frozen rate period ran May 2021 - Oct 2022
    ),
]

# Ontario holidays billed at weekend (off-peak) prices, per Alectra's
# published TOU holiday schedule. 2024-2026 transcribed from Alectra;
# 2027 computed with the standard "next Monday" substitution for
# weekend collisions (Christmas Sat Dec 25 -> Mon Dec 27, Boxing Day
# Sun Dec 26 -> Mon Dec 28) pending the official 2027 schedule.
HOLIDAYS: dict[int, frozenset[date]] = {
    2024: frozenset({
        date(2024, 1, 1), date(2024, 2, 19), date(2024, 3, 29),
        date(2024, 5, 20), date(2024, 7, 1), date(2024, 8, 5),
        date(2024, 9, 2), date(2024, 10, 14), date(2024, 12, 25),
        date(2024, 12, 26),
    }),
    2025: frozenset({
        date(2025, 1, 1), date(2025, 2, 17), date(2025, 4, 18),
        date(2025, 5, 19), date(2025, 7, 1), date(2025, 8, 4),
        date(2025, 9, 1), date(2025, 10, 13), date(2025, 12, 25),
        date(2025, 12, 26),
    }),
    2026: frozenset({
        date(2026, 1, 1), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 18), date(2026, 7, 1), date(2026, 8, 3),
        date(2026, 9, 7), date(2026, 10, 12), date(2026, 12, 25),
        date(2026, 12, 28),
    }),
    2027: frozenset({
        date(2027, 1, 1), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 24), date(2027, 7, 1), date(2027, 8, 2),
        date(2027, 9, 6), date(2027, 10, 11), date(2027, 12, 27),
        date(2027, 12, 28),
    }),
}


def is_ontario_holiday(d: date) -> bool:
    return d in HOLIDAYS.get(d.year, frozenset())


def period_for(d: date) -> RppPeriod | None:
    """Rate period in effect on ``d``, or None if before our table starts."""
    for period in RPP_PERIODS:
        if d >= period.effective:
            return period
    return None


def tier1_threshold(month: int) -> int:
    return TIER1_SUMMER_KWH if month in SUMMER_MONTHS else TIER1_WINTER_KWH


def tou_block(dt: datetime) -> str:
    """Classify an Ontario local wall-clock hour: 'off' | 'mid' | 'on'."""
    if dt.weekday() >= 5 or is_ontario_holiday(dt.date()):
        return "off"
    h = dt.hour
    if dt.month in SUMMER_MONTHS:
        if 11 <= h < 17:
            return "on"
        if 7 <= h < 11 or 17 <= h < 19:
            return "mid"
        return "off"
    if 7 <= h < 11 or 17 <= h < 19:
        return "on"
    if 11 <= h < 17:
        return "mid"
    return "off"


def ulo_block(dt: datetime) -> str:
    """Classify an Ontario local wall-clock hour.

    Returns one of the UloRates field names: 'overnight' | 'weekend_off' | 'mid' | 'on'.
    """
    h = dt.hour
    if h >= 23 or h < 7:
        return "overnight"
    if dt.weekday() >= 5 or is_ontario_holiday(dt.date()):
        return "weekend_off"
    if 16 <= h < 21:
        return "on"
    return "mid"


@dataclass
class MonthResult:
    year: int
    month: int
    kwh: float = 0.0
    period_effective: date | None = None
    threshold: int = 0
    tiered_cost: float = 0.0  # dollars
    tou_cost: float = 0.0
    ulo_cost: float | None = None  # None = ULO unavailable that month
    tou_block_kwh: dict[str, float] = field(default_factory=dict)
    ulo_block_kwh: dict[str, float] = field(default_factory=dict)
    api_amount_total: float = 0.0  # sum of API 'amount' (billed energy charge)
    null_hours: int = 0

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def _tiered_cost(total_kwh: float, threshold: int, rates: TieredRates) -> float:
    dollars = 0.0
    if total_kwh <= threshold:
        dollars = total_kwh * rates.tier1 / 100
    else:
        dollars = (threshold * rates.tier1 + (total_kwh - threshold) * rates.tier2) / 100
    return round(dollars, 2)


def compute_months(records: list[UsageRecord]) -> list[MonthResult]:
    """Aggregate hourly records into per-month plan costs.

    Records with ``kwh=None`` (the API emits some) are counted but add no
    usage. Tiering is applied on the monthly total, which matches Ontario
    rules as long as each month is fetched whole.
    """
    buckets: dict[tuple[int, int], MonthResult] = {}
    for rec in records:
        if rec.kwh is None:
            month = buckets.setdefault(
                (rec.local.year, rec.local.month),
                MonthResult(rec.local.year, rec.local.month),
            )
            month.null_hours += 1
            continue
        month = buckets.setdefault(
            (rec.local.year, rec.local.month), MonthResult(rec.local.year, rec.local.month)
        )
        month.kwh += rec.kwh
        if rec.amount is not None:
            month.api_amount_total += rec.amount
        period = period_for(rec.local.date())
        if period is None:
            continue
        month.period_effective = period.effective
        month.threshold = tier1_threshold(rec.local.month)
        month.tou_cost += rec.kwh * getattr(period.tou, tou_block(rec.local)) / 100
        if period.ulo is not None:
            month.ulo_cost = (month.ulo_cost or 0.0) + rec.kwh * getattr(
                period.ulo, ulo_block(rec.local)
            ) / 100
        tb = tou_block(rec.local)
        month.tou_block_kwh[tb] = month.tou_block_kwh.get(tb, 0.0) + rec.kwh
        if period.ulo is not None:
            ub = ulo_block(rec.local)
            month.ulo_block_kwh[ub] = month.ulo_block_kwh.get(ub, 0.0) + rec.kwh

    results = []
    for (year, m), month in sorted(buckets.items()):
        period = period_for(date(year, m, 1))
        if period is not None:
            month.tiered_cost = _tiered_cost(month.kwh, month.threshold, period.tiered)
        month.tou_cost = round(month.tou_cost, 2)
        if month.ulo_cost is not None:
            month.ulo_cost = round(month.ulo_cost, 2)
        month.kwh = round(month.kwh, 3)
        month.api_amount_total = round(month.api_amount_total, 2)
        results.append(month)
    return results
