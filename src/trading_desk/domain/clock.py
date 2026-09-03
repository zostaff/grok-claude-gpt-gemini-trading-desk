"""Injectable time. The daily brakes roll over at midnight; tests must not wait for it."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta


class SystemClock:
    """Real time. The default everywhere outside tests."""

    def today(self) -> date:
        """Current calendar date."""
        return date.today()

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations."""
        return time.monotonic()


@dataclass
class FrozenClock:
    """Controllable time for tests: advance the day or the stopwatch by hand."""

    day: date = field(default_factory=date.today)
    elapsed: float = 0.0

    def today(self) -> date:
        """Current calendar date."""
        return self.day

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations."""
        return self.elapsed

    def advance_days(self, days: int = 1) -> None:
        """Roll the calendar forward."""
        self.day = self.day + timedelta(days=days)

    def advance_seconds(self, seconds: float) -> None:
        """Roll the stopwatch forward."""
        self.elapsed += seconds
