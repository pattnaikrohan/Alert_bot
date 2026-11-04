"""How long a pipeline run took against how long it normally takes (what the bot has learned from its own runs).

    finished run   faster · normal · slower · much slower, and by how much
    running run    elapsed so far, when it should finish, and whether it's already past its normal
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..timeutil import iso, local_weekday, utcnow
from .baselines import band_for

OUTPUT = "_pipeline"  # where run durations are learned (metric duration_s)


class Norms:
    """Normal run times per pipeline for one project, worked out once per request."""

    def __init__(self, ctx: Any, now: datetime | None = None):
        self.ctx, self.now = ctx, now or utcnow()
        self._cache: dict[str, dict[str, Any] | None] = {}

    def of(self, site: str) -> dict[str, Any] | None:
        if site not in self._cache:
            b = band_for(self.ctx, OUTPUT, site, "duration_s", "", local_weekday(self.now, self.ctx.settings.tz), before=self.now + timedelta(seconds=1))
            self._cache[site] = None if b is None else {
                "median_s": round(b.median, 1), "low_s": round(max(0.0, b.low), 1), "high_s": round(b.high, 1), "samples": b.samples,
                "basis": b.basis}
        return self._cache[site]

    def compare(self, site: str, duration_s: float | None, *, status: str | None = None, started_at: datetime | None = None) -> dict[str, Any] | None:
        """The run's time against its pipeline's normal; None while the bot is still learning that normal."""
        n = self.of(site)
        if n is None:
            return None
        out: dict[str, Any] = {**n}
        med, low, high = n["median_s"], n["low_s"], n["high_s"]
        if status in ("InProgress", "Queued") and started_at is not None:
            elapsed = max(0.0, (self.now - started_at).total_seconds())
            out.update(running=True, elapsed_s=round(elapsed, 1), expected_end=iso(started_at + timedelta(seconds=med)),
                       remaining_s=round(max(0.0, med - elapsed), 1), progress=round(min(1.5, elapsed / med), 3) if med else None,
                       verdict="overdue" if elapsed > high else "on_track")
            return out
        if duration_s is None:
            return out
        pct = (duration_s / med - 1) * 100 if med else 0.0
        verdict = "faster" if duration_s < low else "normal" if duration_s <= high else "slower" if duration_s <= high * 1.5 else "much_slower"
        out.update(running=False, actual_s=round(duration_s, 1), pct=round(pct), delta_s=round(duration_s - med, 1), verdict=verdict)
        return out
