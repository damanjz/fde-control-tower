"""Scenario clock. Real time unless a clock file exists; then time runs from a scenario start, compressed.

The simulator writes the file; the tower and the mock services read it, so all three agree on "now".
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_START = datetime(2026, 11, 13, 5, 0, tzinfo=IST)


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock(Clock):
    def __init__(self, at: datetime):
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


class ScenarioClock(Clock):
    """start + (wall seconds since wall_start) * factor."""

    def __init__(self, start: datetime, factor: float, wall_start: float | None = None):
        self.start = start
        self.factor = factor
        self.wall_start = time.time() if wall_start is None else wall_start

    def now(self) -> datetime:
        return self.start + timedelta(seconds=max(0.0, time.time() - self.wall_start) * self.factor)

    def to_json(self) -> dict:
        return {"start": self.start.isoformat(), "factor": self.factor, "wall_start": self.wall_start}

    @classmethod
    def from_json(cls, d: dict) -> ScenarioClock:
        return cls(datetime.fromisoformat(d["start"]), float(d["factor"]), float(d["wall_start"]))


class FileClock(Clock):
    """Follows the clock file when present (re-read at most once a second); real time otherwise."""

    def __init__(self, path: Path):
        self.path = path
        self._checked = -10.0
        self._inner: Clock = RealClock()

    def now(self) -> datetime:
        t = time.monotonic()
        if t - self._checked > 1.0:
            self._checked = t
            try:
                self._inner = ScenarioClock.from_json(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                self._inner = RealClock()
        return self._inner.now()


def write_clock(path: Path, clock: ScenarioClock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clock.to_json()), encoding="utf-8")
    tmp.replace(path)


def clear_clock(path: Path) -> None:
    path.unlink(missing_ok=True)
