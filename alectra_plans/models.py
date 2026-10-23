"""Data models shared by the API client and the rate engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class UsageRecord:
    """One hourly smart-meter reading in Ontario *local wall time*.

    The API's ``readDate`` carries a bogus ``+01:00`` offset but the clock
    time is already Ontario local time, so we parse the naive prefix and
    never convert timezones. On DST transition days the local day has 23 or
    25 hours; we accept whatever hours the API reports without filling gaps.
    """

    local: datetime  # naive, local wall time of the hour
    kwh: float | None
    amount: float | None = None  # API-reported billed energy charge, current plan

    @classmethod
    def from_api(cls, entry: dict) -> "UsageRecord":
        raw_date = (entry.get("readDate") or "")[:19]
        local = datetime.fromisoformat(raw_date) if raw_date else None
        if local is None:
            raise ValueError(f"record without usable readDate: {entry!r}")
        return cls(
            local=local,
            kwh=_to_float(entry.get("consumption")),
            amount=_to_float(entry.get("amount")),
        )


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
