"""Background workers for milestone 2: impact recompute and the outbox dispatcher."""

from __future__ import annotations

import logging
import threading

import httpx

from tower import risk
from tower.clock import Clock
from tower.config import Settings
from tower.logging_ import log

logger = logging.getLogger("tower.workers")
MAX_ATTEMPTS = 5


class ImpactWorker(threading.Thread):
    """Keeps incident_impact and incident_exposure current as shipments, incidents and notifications change."""

    def __init__(self, pool, settings: Settings, clock: Clock, stop: threading.Event):
        super().__init__(name="impact-worker", daemon=True)
        self.pool, self.settings, self.clock, self.stop_event = pool, settings, clock, stop

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                risk.recompute_all(self.pool, self.clock.now())
            except Exception as e:
                log(logger, logging.ERROR, "impact recompute failed", error=type(e).__name__)
            self.stop_event.wait(self.settings.impact_poll_s)


def dispatch_outbox(pool, settings: Settings, client: httpx.Client, limit: int = 50) -> dict:
    """Post pending outbox rows to the chat webhook. A failed post is retried on the next pass, up to 5 attempts."""
    sent = failed = 0
    with pool.connection() as conn, conn.transaction():
        rows = conn.execute("SELECT id, payload FROM outbox WHERE status = 'pending' ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                            (limit,)).fetchall()
        for r in rows:
            try:
                resp = client.post(settings.chat_webhook_url, json={"outbox_id": r["id"], **r["payload"]})
                resp.raise_for_status()
                conn.execute("UPDATE outbox SET status = 'sent', sent_at = now(), attempts = attempts + 1 WHERE id = %s", (r["id"],))
                sent += 1
            except httpx.HTTPError as e:
                conn.execute(
                    "UPDATE outbox SET attempts = attempts + 1, last_error = %s, "
                    "status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END WHERE id = %s",
                    (type(e).__name__, MAX_ATTEMPTS, r["id"]))
                failed += 1
    return {"sent": sent, "failed": failed}


class OutboxWorker(threading.Thread):
    def __init__(self, pool, settings: Settings, stop: threading.Event):
        super().__init__(name="outbox-worker", daemon=True)
        self.pool, self.settings, self.stop_event = pool, settings, stop
        self.client = httpx.Client(timeout=3, headers={"X-Api-Key": settings.carrier_api_key})

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                dispatch_outbox(self.pool, self.settings, self.client)
            except Exception as e:
                log(logger, logging.ERROR, "outbox dispatch failed", error=type(e).__name__)
            self.stop_event.wait(self.settings.outbox_poll_s)
