"""Per-feed circuit breaker: closed -> open after N consecutive failures -> half_open after a cooldown
(one probe) -> closed on success, or open again with a longer cooldown on failure."""

from __future__ import annotations

import time
from collections.abc import Callable

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 3,
        cooldown_s: float = 4.0,
        max_cooldown_s: float = 16.0,
        now: Callable[[], float] = time.monotonic,
        on_change: Callable[[str, str], None] | None = None,
    ):
        self.threshold = threshold
        self.base_cooldown = cooldown_s
        self.max_cooldown = max_cooldown_s
        self.cooldown = cooldown_s
        self._now = now
        self._on_change = on_change
        self.state = CLOSED
        self.failures = 0
        self.opened_at = 0.0

    def _set(self, new: str) -> None:
        old, self.state = self.state, new
        if old != new and self._on_change:
            self._on_change(old, new)

    def allow(self) -> bool:
        """True when a call may go out. An open circuit lets one probe through once the cooldown has passed."""
        if self.state == CLOSED:
            return True
        if self.state == OPEN and self._now() - self.opened_at >= self.cooldown:
            self._set(HALF_OPEN)
            return True
        return self.state == HALF_OPEN

    def record_success(self) -> None:
        self.failures = 0
        self.cooldown = self.base_cooldown
        self._set(CLOSED)

    def record_failure(self) -> None:
        if self.state == HALF_OPEN:
            self.cooldown = min(self.cooldown * 2, self.max_cooldown)
            self.opened_at = self._now()
            self._set(OPEN)
            return
        self.failures += 1
        if self.state == CLOSED and self.failures >= self.threshold:
            self.opened_at = self._now()
            self._set(OPEN)
