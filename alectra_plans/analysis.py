"""Full analysis: hourly records -> per-month plan comparison + Tesla shift simulation."""

from __future__ import annotations

from datetime import date

from . import rates
from .models import UsageRecord
from .rates import MonthResult, compute_months

MONTH_LABELS = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _month_dict(m: MonthResult) -> dict:
    plans = {"tiered": m.tiered_cost, "tou": m.tou_cost, "ulo": m.ulo_cost}
    available = {k: v for k, v in plans.items() if v is not None}
    best = min(available, key=available.get) if available else None
    return {
        "key": m.key,
        "label": f"{MONTH_LABELS[m.month]} {m.year}",
        "kwh": m.kwh,
        "period": str(m.period_effective) if m.period_effective else None,
        "threshold": m.threshold,
        "plans": plans,
        "best": best,
        "blocks": {"tou": m.tou_block_kwh, "ulo": m.ulo_block_kwh},
        "api_amount": m.api_amount_total,
        "null_hours": m.null_hours,
    }


def _totals(month_dicts: list[dict]) -> dict:
    totals: dict[str, float | None] = {"kwh": 0.0, "tiered": 0.0, "tou": 0.0, "ulo": 0.0}
    for md in month_dicts:
        totals["kwh"] += md["kwh"]
        for plan in ("tiered", "tou", "ulo"):
            if md["plans"][plan] is not None:
                totals[plan] += md["plans"][plan]
    for plan in ("tiered", "tou", "ulo"):
        totals[plan] = round(totals[plan], 2) if totals[plan] is not None else None
    totals["kwh"] = round(totals["kwh"], 1)
    available = {k: v for k, v in totals.items() if k in ("tiered", "tou", "ulo") and v is not None}
    best = min(available, key=available.get) if available else None
    totals["best"] = best
    if best:
        totals["savings_vs"] = {
            plan: round(v - totals[best], 2) for plan, v in available.items() if plan != best
        }
    return totals


def analyze(records: list[UsageRecord]) -> dict:
    months = [_month_dict(m) for m in compute_months(records)]
    return {
        "months": months,
        "totals": _totals(months),
        "hourly_profile": _hourly_profile(records),
    }


def _hourly_profile(records: list[UsageRecord]) -> dict:
    """Average kWh per hour of day, weekdays vs weekends/holidays."""
    weekday = [0.0] * 24
    weekend = [0.0] * 24
    counts_wd = [0] * 24
    counts_we = [0] * 24
    for rec in records:
        if rec.kwh is None:
            continue
        h = rec.local.hour
        if rec.local.weekday() >= 5 or rates.is_ontario_holiday(rec.local.date()):
            weekend[h] += rec.kwh
            counts_we[h] += 1
        else:
            weekday[h] += rec.kwh
            counts_wd[h] += 1
    return {
        "weekday": [round(v / c, 3) if c else 0.0 for v, c in zip(weekday, counts_wd)],
        "weekend": [round(v / c, 3) if c else 0.0 for v, c in zip(weekend, counts_we)],
    }


def simulate_tesla_shift(
    records: list[UsageRecord],
    monthly_kwh: dict[str, float],
    baseline_kwh_per_hour: float = 1.0,
) -> dict:
    """Re-price assumed EV charging into the cheap overnight block.

    For each month, remove up to ``monthly_kwh[key]`` from the highest-usage
    hours (keeping ``baseline`` kWh/h as the house load), then price the
    removed block at that month's overnight rate. 23:00-07:00 is a single
    flat block in both TOU (off-peak) and ULO (ultra-low), so this is exact;
    the tiered cost is unchanged because the monthly total is unchanged.
    """
    by_month: dict[str, list[UsageRecord]] = {}
    for rec in records:
        by_month.setdefault(f"{rec.local.year:04d}-{rec.local.month:02d}", []).append(rec)

    out = []
    for m in compute_months(records):
        target = float(monthly_kwh.get(m.key, 0.0) or 0.0)
        entry = {
            "key": m.key,
            "label": f"{MONTH_LABELS[m.month]} {m.year}",
            "kwh": m.kwh,
            "requested_kwh": target,
            "tiered": m.tiered_cost,
            "tou": m.tou_cost,
            "ulo": m.ulo_cost,
            "charged_kwh": 0.0,
        }
        if target > 0:
            period = rates.period_for(date(m.year, m.month, 1))
            if period is not None:
                removed, slices = _shift_month(
                    by_month.get(m.key, []), target, baseline_kwh_per_hour
                )
                if removed > 0:
                    original_tou = sum(
                        take * getattr(period.tou, rates.tou_block(rec.local)) / 100
                        for rec, take in slices
                    )
                    original_ulo = (
                        sum(
                            take * getattr(period.ulo, rates.ulo_block(rec.local)) / 100
                            for rec, take in slices
                        )
                        if period.ulo
                        else 0.0
                    )
                    entry["charged_kwh"] = round(removed, 1)
                    entry["tou_shifted"] = round(
                        (m.tou_cost or 0.0) - original_tou + removed * period.tou.off / 100, 2
                    )
                    if m.ulo_cost is not None and period.ulo is not None:
                        entry["ulo_shifted"] = round(
                            m.ulo_cost - original_ulo + removed * period.ulo.overnight / 100, 2
                        )
        out.append(entry)

    return {
        "months": out,
        "totals": {
            "tiered": round(sum(e["tiered"] or 0 for e in out), 2),
            "tou": round(sum(e["tou"] or 0 for e in out), 2),
            "ulo": round(sum(e["ulo"] or 0 for e in out), 2),
            "tou_shifted": round(sum(e.get("tou_shifted") or e["tou"] or 0 for e in out), 2),
            "ulo_shifted": round(sum(e.get("ulo_shifted") or e["ulo"] or 0 for e in out), 2),
            "charged_kwh": round(sum(e["charged_kwh"] for e in out), 1),
        },
    }


def _shift_month(
    recs: list[UsageRecord], target: float, baseline: float
) -> tuple[float, list[tuple[UsageRecord, float]]]:
    """Remove up to ``target`` kWh from hours above baseline, highest first."""
    order = sorted((r for r in recs if r.kwh is not None), key=lambda r: r.kwh, reverse=True)
    remaining = target
    taken: list[tuple[UsageRecord, float]] = []
    for rec in order:
        if remaining <= 0:
            break
        available = rec.kwh - baseline
        if available <= 0:
            continue
        take = min(available, remaining)
        taken.append((rec, take))
        remaining -= take
    return target - remaining, taken
