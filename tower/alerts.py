"""Weather / incident alerts feed: poll, validate, upsert idempotently into disruption_event."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime

import httpx
from psycopg import Connection
from pydantic import BaseModel, ValidationError

from tower.clock import Clock
from tower.config import Settings
from tower.logging_ import log
from tower.store import RowError, add_dead_letter, bump

logger = logging.getLogger("tower.alerts")
FEED = "alerts"
SOURCE = "imd"

SEVERITY_ALIASES = {
    "low": "low", "green": "low", "yellow": "medium", "medium": "medium", "moderate": "medium", "amber": "medium",
    "orange": "high", "high": "high", "red": "critical", "critical": "critical", "severe": "critical",
    "1": "low", "2": "medium", "3": "high", "4": "critical",
}


class AlertIn(BaseModel):
    alert_id: str
    source: str = SOURCE
    kind: str
    title: str
    detail: str
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    dc_id: str | None = None
    carrier_id: str | None = None
    supplier_id: str | None = None
    states: list[str] | None = None
    severity: str | int
    issued_at: datetime


def ingest_alert(conn: Connection, raw: dict, now: datetime) -> str:
    """Returns 'new' or 'duplicate'. Raises RowError for alerts that cannot be stored."""
    try:
        a = AlertIn.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        raise RowError("bad_alert", f"{'.'.join(map(str, err['loc']))}: {err['msg']}", "Fix the field and replay") from None
    sev = SEVERITY_ALIASES.get(str(a.severity).strip().lower())
    if sev is None:
        raise RowError("bad_severity", f"Severity not recognised: {a.severity!r}", "Map the severity and replay", {"severity": "medium"})
    if a.kind not in {"weather", "facility", "carrier", "supplier"}:
        raise RowError("bad_kind", f"Alert kind not recognised: {a.kind!r}", "Correct the kind and replay")
    has_geo = a.lat is not None and a.lon is not None and a.radius_km is not None
    if not (has_geo or a.dc_id or a.carrier_id or a.supplier_id):
        raise RowError("no_scope", "Alert has no location, DC, carrier or supplier", "Add the affected scope and replay")
    fresh = conn.execute(
        "INSERT INTO ingest_log (feed, idem_key) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
        (FEED, f"{a.source}:{a.alert_id}"),
    ).fetchone()
    if not fresh:
        return "duplicate"
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO disruption_event (source, external_id, kind, title, detail, lat, lon, radius_km, dc_id,
                                              carrier_id, supplier_id, states, severity, started_at, received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, external_id) DO NOTHING
                """,
                (a.source, a.alert_id, a.kind, a.title, a.detail, a.lat, a.lon, a.radius_km, a.dc_id, a.carrier_id,
                 a.supplier_id, a.states, sev, a.issued_at, now),
            )
    except Exception as e:  # foreign key on dc / carrier / supplier
        conn.execute("DELETE FROM ingest_log WHERE feed = %s AND idem_key = %s", (FEED, f"{a.source}:{a.alert_id}"))
        raise RowError("unknown_scope", f"Alert refers to an unknown DC, carrier or supplier ({type(e).__name__})",
                       "Correct the id and replay") from None
    return "new"


def ingest_alerts(pool, raws: list[dict], now: datetime) -> dict[str, int]:
    out = {"new": 0, "duplicate": 0, "dead_letter": 0, "dead_letter_repeat": 0}
    with pool.connection() as conn:
        for raw in raws:
            try:
                out[ingest_alert(conn, raw, now)] += 1
            except RowError as e:
                if add_dead_letter(conn, FEED, now, raw, e):
                    out["dead_letter"] += 1
                else:
                    out["dead_letter_repeat"] += 1
        bump(conn, FEED, now, received=len(raws), accepted=out["new"] + out["duplicate"],
             rejected=out["dead_letter"] + out["dead_letter_repeat"], clear_error=True,
             counters={"new": out["new"], "duplicates": out["duplicate"]})
    return out


class AlertsPoller(threading.Thread):
    def __init__(self, pool, settings: Settings, clock: Clock, stop: threading.Event, client: httpx.Client | None = None):
        super().__init__(name="alerts-poller", daemon=True)
        self.pool, self.settings, self.clock, self.stop_event = pool, settings, clock, stop
        self.client = client or httpx.Client(
            base_url=settings.carrier_api_url, timeout=settings.carrier_timeout_s,
            headers={"X-Api-Key": settings.carrier_api_key},
        )

    def poll_once(self) -> dict | None:
        at = self.clock.now()
        try:
            r = self.client.get("/alerts")
            r.raise_for_status()
            alerts = r.json()["alerts"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            with self.pool.connection() as conn:
                bump(conn, FEED, at, received=1, rejected=1, error=f"poll failed: {type(e).__name__}",
                     counters={"failed_polls": 1})
            log(logger, logging.WARNING, "alerts poll failed", error=type(e).__name__)
            return None
        return ingest_alerts(self.pool, alerts, at)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log(logger, logging.ERROR, "alerts poller error", error=type(e).__name__)
            self.stop_event.wait(self.settings.alerts_poll_s * random.uniform(0.9, 1.1))
