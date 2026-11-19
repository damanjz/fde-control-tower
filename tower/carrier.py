"""Carrier status poller: timeouts, jittered exponential backoff, per-carrier circuit breaker.

While a circuit is open the poller makes no calls and leaves the last known carrier status on the shipments
(graceful degradation); feed_health says the feed is degraded and when the data was last fresh.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime

import httpx
from pydantic import BaseModel, ValidationError

from tower.breaker import CLOSED, CircuitBreaker
from tower.clock import Clock
from tower.config import Settings
from tower.logging_ import log
from tower.store import audit, bump

logger = logging.getLogger("tower.carrier")


class Consignment(BaseModel):
    ref: str
    status: str
    eta: datetime | None = None


class CarrierResponse(BaseModel):
    carrier: str
    as_of: datetime
    consignments: list[Consignment]


class PollError(Exception):
    pass


def backoff_delay(attempt: int, base: float = 0.25, cap: float = 2.0, rng: random.Random = random) -> float:
    """Exponential with full jitter on top: base*2^attempt plus up to `base` of random extra."""
    return min(cap, base * (2**attempt)) + rng.uniform(0, base)


class CarrierPoller(threading.Thread):
    def __init__(self, pool, settings: Settings, clock: Clock, carrier_id: str, stop: threading.Event,
                 client: httpx.Client | None = None):
        super().__init__(name=f"carrier-{carrier_id}", daemon=True)
        self.pool, self.settings, self.clock, self.carrier_id = pool, settings, clock, carrier_id
        self.stop_event = stop
        self.feed = f"carrier:{carrier_id}"
        self.client = client or httpx.Client(
            base_url=settings.carrier_api_url, timeout=settings.carrier_timeout_s,
            headers={"X-Api-Key": settings.carrier_api_key},
        )
        self.breaker = CircuitBreaker(
            settings.breaker_threshold, settings.breaker_cooldown_s, settings.breaker_max_cooldown_s,
            on_change=self._on_state_change,
        )
        self.last_ok_at: datetime | None = None

    # -- persistence of state changes
    def _on_state_change(self, old: str, new: str) -> None:
        at = self.clock.now()
        note = None
        if new == "open":
            since = f"{self.last_ok_at:%H:%M}" if self.last_ok_at else "never"
            note = f"circuit open: serving last known status (last fresh {since})"
        try:
            with self.pool.connection() as conn:
                bump(conn, self.feed, at, circuit=new, error=note, counters={f"to_{new}": 1})
                audit(conn, "system", "carrier.circuit", self.feed, {"from": old, "to": new}, at=at)
        except Exception as e:
            log(logger, logging.ERROR, "could not persist circuit state", feed=self.feed, error=type(e).__name__)
        log(logger, logging.WARNING, "circuit state change", feed=self.feed, old=old, new=new)

    def sync_state(self) -> None:
        """A new process starts with a fresh, closed breaker. Persist that at startup, so a circuit that was open when the
        old process died does not survive the restart in feed_health (circuit state is otherwise written on transitions)."""
        state, at = self.breaker.state, self.clock.now()
        with self.pool.connection() as conn:
            old = conn.execute(
                "SELECT circuit_state FROM feed_health WHERE feed = %s FOR UPDATE", (self.feed,)).fetchone()
            if old is None or old["circuit_state"] == state:
                return
            conn.execute(
                "UPDATE feed_health SET circuit_state = %s, "
                "last_error = CASE WHEN last_error LIKE 'circuit open:%%' THEN NULL ELSE last_error END WHERE feed = %s",
                (state, self.feed))
            audit(conn, "system", "carrier.circuit", self.feed, {"from": old["circuit_state"], "to": state, "why": "restart"}, at=at)
        log(logger, logging.WARNING, "circuit state reset at startup", feed=self.feed, old=old["circuit_state"], new=state)

    # -- one HTTP call with retries
    def fetch(self) -> CarrierResponse:
        attempts = 1 + self.settings.carrier_retries
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = self.client.get("/v2/consignments", params={"carrier_id": self.carrier_id})
                if r.status_code == 200:
                    return CarrierResponse.model_validate(r.json())
                if r.status_code < 500 and r.status_code != 429:
                    raise PollError(f"HTTP {r.status_code}")  # client error: retrying will not help
                last = PollError(f"HTTP {r.status_code}")
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = PollError(f"{type(e).__name__}")
            except (ValidationError, ValueError) as e:
                raise PollError("bad response body") from e
            if i < attempts - 1:
                time.sleep(backoff_delay(i))
        raise PollError(str(last))

    def poll_once(self) -> str:
        """Returns 'ok', 'failed' or 'skipped' (circuit open)."""
        if not self.breaker.allow():
            return "skipped"
        at = self.clock.now()
        try:
            data = self.fetch()
        except PollError as e:
            with self.pool.connection() as conn:
                bump(conn, self.feed, at, received=1, rejected=1, error=f"poll failed: {e}", circuit=self.breaker.state,
                     counters={"failed_polls": 1})
            self.breaker.record_failure()   # after the bump: an opening circuit then sets its own, more useful note
            log(logger, logging.WARNING, "carrier poll failed", feed=self.feed, error=str(e), circuit=self.breaker.state)
            return "failed"
        with self.pool.connection() as conn:
            with conn.cursor() as c:
                c.executemany(
                    "UPDATE shipment SET carrier_status = %s, carrier_status_at = %s, carrier_eta = %s WHERE id = %s",
                    [(x.status, data.as_of, x.eta, x.ref) for x in data.consignments],
                )
            n = len(data.consignments)
            # a successful poll always closes the breaker: write it on every poll, not only on a transition
            bump(conn, self.feed, at, received=1, accepted=1, clear_error=True, circuit=CLOSED, counters={"consignments": n})
        self.last_ok_at = at
        self.breaker.record_success()
        return "ok"

    def run(self) -> None:
        try:
            self.sync_state()
        except Exception as e:
            log(logger, logging.ERROR, "could not sync circuit state", feed=self.feed, error=type(e).__name__)
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log(logger, logging.ERROR, "carrier poller error", feed=self.feed, error=type(e).__name__)
            self.stop_event.wait(self.settings.carrier_poll_s * random.uniform(0.9, 1.1))
