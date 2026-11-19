"""GPS webhook processing: validate, dedupe by ping id, reorder by event time, quarantine clock skew."""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg import Connection
from pydantic import BaseModel, Field, ValidationError

from tower.store import RowError, add_dead_letter, bump

FEED = "gps"


class PingIn(BaseModel):
    ping_id: str = Field(min_length=4, max_length=80)
    shipment_id: str = Field(min_length=3, max_length=40)
    vehicle: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmph: float | None = Field(default=None, ge=0, le=200)
    event_ts: datetime


def process_ping(conn: Connection, raw: dict, now: datetime, max_future: timedelta) -> str:
    """Apply one ping. Returns 'accepted', 'reordered' (stored, older than the newest we hold) or 'duplicate'.

    Raises RowError for pings that must go to the dead-letter queue.
    """
    try:
        p = PingIn.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        raise RowError("bad_ping", f"{'.'.join(map(str, err['loc']))}: {err['msg']}", "Fix the field and replay") from None
    if p.event_ts.tzinfo is None:
        raise RowError("bad_ping", "event_ts has no timezone", "Add the device's UTC offset and replay")
    if p.event_ts > now + max_future:
        skew = p.event_ts - now
        raise RowError(
            "clock_skew_future",
            f"Ping time is {int(skew.total_seconds() // 60)} min in the future (device clock skew)",
            "Replace the ping time with the receipt time",
            {"event_ts": now.isoformat()},
        )
    fresh = conn.execute(
        "INSERT INTO ingest_log (feed, idem_key) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1", (FEED, p.ping_id)
    ).fetchone()
    if not fresh:
        return "duplicate"
    newest = conn.execute("SELECT max(event_ts) AS t FROM gps_ping WHERE shipment_id = %s", (p.shipment_id,)).fetchone()["t"]
    conn.execute(
        "INSERT INTO gps_ping (ping_id, shipment_id, vehicle, lat, lon, speed_kmph, event_ts, received_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (p.ping_id, p.shipment_id, p.vehicle, p.lat, p.lon, p.speed_kmph, p.event_ts, now),
    )
    # never let an older ping overwrite a newer position
    conn.execute(
        "UPDATE shipment SET last_lat = %s, last_lon = %s, last_position_at = %s "
        "WHERE id = %s AND (last_position_at IS NULL OR last_position_at < %s)",
        (p.lat, p.lon, p.event_ts, p.shipment_id, p.event_ts),
    )
    return "reordered" if newest is not None and newest > p.event_ts else "accepted"


def process_batch(pool, raws: list[dict], now: datetime, max_future: timedelta) -> dict[str, int]:
    out = {"accepted": 0, "reordered": 0, "duplicate": 0, "dead_lettered": 0, "dead_letter_repeat": 0}
    with pool.connection() as conn:
        for raw in raws:
            try:
                with conn.transaction():
                    res = process_ping(conn, raw, now, max_future)
                out[res] += 1
            except RowError as e:
                if add_dead_letter(conn, FEED, now, raw if isinstance(raw, dict) else {"raw": str(raw)}, e):
                    out["dead_lettered"] += 1
                else:
                    out["dead_letter_repeat"] += 1
        bump(conn, FEED, now, received=len(raws), accepted=out["accepted"] + out["reordered"] + out["duplicate"],
             rejected=out["dead_lettered"] + out["dead_letter_repeat"], clear_error=True,
             counters={"reordered": out["reordered"], "duplicates": out["duplicate"],
                       "dead_lettered": out["dead_lettered"]})
    return out


def reject_signature(pool, now: datetime, code: str) -> None:
    with pool.connection() as conn:
        bump(conn, FEED, now, received=1, rejected=1, error=f"webhook rejected: {code}",
             counters={"bad_signature": 1})

