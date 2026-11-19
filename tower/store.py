"""Bookkeeping shared by every feed: feed_health, dead letters, audit log."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from psycopg import Connection
from psycopg.types.json import Jsonb


class RowError(Exception):
    """A record the pipeline refuses. Carries what goes into the dead letter."""

    def __init__(self, code: str, reason: str, fix: str | None = None, patch: dict | None = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.fix = fix
        self.patch = patch


def payload_hash(payload: dict) -> str:
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:32]


def bump(
    conn: Connection,
    feed: str,
    at: datetime,
    *,
    received: int = 0,
    accepted: int = 0,
    rejected: int = 0,
    error: str | None = None,
    clear_error: bool = False,
    circuit: str | None = None,
    counters: dict[str, int] | None = None,
) -> None:
    """Add to a feed's counters and touch last_at. `error` sets last_error; `clear_error` empties it."""
    counters = {k: v for k, v in (counters or {}).items() if v}
    conn.execute(
        """
        INSERT INTO feed_health (feed, received, accepted, rejected, last_at, last_error, circuit_state, counters)
        VALUES (%(feed)s, %(r)s, %(a)s, %(x)s, %(at)s, %(err)s, COALESCE(%(circ)s, 'closed'), %(c)s::jsonb)
        ON CONFLICT (feed) DO UPDATE SET
            received = feed_health.received + EXCLUDED.received,
            accepted = feed_health.accepted + EXCLUDED.accepted,
            rejected = feed_health.rejected + EXCLUDED.rejected,
            last_at = EXCLUDED.last_at,
            last_error = CASE WHEN %(clear)s THEN NULL ELSE COALESCE(%(err)s, feed_health.last_error) END,
            circuit_state = COALESCE(%(circ)s, feed_health.circuit_state),
            counters = feed_health.counters || (
                SELECT COALESCE(jsonb_object_agg(e.key, COALESCE((feed_health.counters ->> e.key)::bigint, 0) + e.value::bigint), '{}')
                FROM jsonb_each_text(%(c)s::jsonb) e)
        """,
        {"feed": feed, "r": received, "a": accepted, "x": rejected, "at": at, "err": error,
         "circ": circuit, "c": json.dumps(counters), "clear": clear_error},
    )


def add_dead_letter(
    conn: Connection,
    feed: str,
    at: datetime,
    payload: dict,
    error: RowError,
) -> bool:
    """Insert a waiting dead letter. Returns False when an identical bad payload is already on file."""
    row = conn.execute(
        """
        INSERT INTO dead_letter (feed, received_at, payload, payload_hash, reason_code, reason, suggested_fix, suggested_patch)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (feed, payload_hash) DO NOTHING
        RETURNING id
        """,
        (feed, at, Jsonb(payload), payload_hash(payload), error.code, error.reason, error.fix,
         Jsonb(error.patch) if error.patch else None),
    ).fetchone()
    return row is not None


def audit(conn: Connection, actor: str, action: str, target: str, detail: dict | None = None,
          at: datetime | None = None, dc_ids: list[str] | None = None) -> None:
    """Append to the audit log. `dc_ids` lists the DCs the action touched (None = network-wide)."""
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail, dc_ids) VALUES (COALESCE(%s, now()), %s, %s, %s, %s, %s)",
        (at, actor, action, target, Jsonb(detail or {}), dc_ids),
    )
