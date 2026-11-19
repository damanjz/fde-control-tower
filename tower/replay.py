"""Dead-letter replay and discard. Replay goes through the same validation as first ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from tower import alerts, bol, erp, gps
from tower.store import RowError, audit, bump

# What a replay did, from the handler's outcome. 'stale': the row is older than the data held, so it was not applied.
RESULT = {"new": "applied", "updated": "applied", "accepted": "applied", "reordered": "applied", "applied": "applied",
          "duplicate": "unchanged", "unchanged": "unchanged", "stale": "stale"}

# ERP fields a suggested patch may fill when they are blank, and the shipment column that already holds them.
BLANK_FILLS = {"promised_at": "promised_at"}


class NotFound(Exception):
    pass


class NotWaiting(Exception):
    pass


def _lock(conn, dl_id: int) -> dict:
    row = conn.execute("SELECT * FROM dead_letter WHERE id = %s FOR UPDATE", (dl_id,)).fetchone()
    if row is None:
        raise NotFound(dl_id)
    if row["status"] != "waiting":
        raise NotWaiting(row["status"])
    return row


def _keep_stored(conn, payload: dict, patch: dict) -> tuple[dict, list[str]]:
    """A suggested patch that fills a blank field must not replace a value the shipment already has: use the stored one."""
    blanks = [k for k in patch if k in BLANK_FILLS and not str(payload.get(k) or "").strip()]
    if not blanks:
        return patch, []
    cur = conn.execute("SELECT * FROM shipment WHERE id = %s", (str(payload.get("shipment_no") or "").strip().upper(),)).fetchone()
    kept = [k for k in blanks if cur is not None and cur[BLANK_FILLS[k]] is not None]
    return {**patch, **{k: cur[BLANK_FILLS[k]].isoformat() for k in kept}}, kept


def replay(pool, dl_id: int, corrected: dict | None, actor: str, now: datetime, max_future: timedelta) -> dict:
    """Re-run a waiting dead letter. With `corrected` those fields are merged over the stored payload;
    without it the suggested patch (if any) is applied. Returns {"ok": bool, "result": ...}, where result is
    'applied', 'unchanged' (the content was already held), 'stale' (older than the data held, not applied) or 'failed'."""
    with pool.connection() as conn:
        row = _lock(conn, dl_id)
        payload: dict = row["payload"]
        applied = corrected if corrected else (row["suggested_patch"] or {})
        kept: list[str] = []
        if not corrected and applied and row["feed"] == erp.FEED:
            applied, kept = _keep_stored(conn, payload, applied)
        merged = {**payload, **applied}
        clean = {k: v for k, v in merged.items() if not k.startswith("_")}
        try:
            with conn.transaction():
                if row["feed"] == erp.FEED:
                    if set(clean) <= {"raw_line"}:
                        raise RowError("cannot_replay", "A malformed row has no fields to replay",
                                       "Supply a corrected payload with the canonical fields")
                    if "headers" in clean:
                        raise RowError("cannot_replay", "A whole-file rejection cannot be replayed",
                                       "Fix the file and drop it in the inbox again")
                    exported = datetime.fromisoformat(payload["_exported_at"])
                    outcome = erp.process_row(conn, erp.RefData.load(conn), clean, exported, source="replay")
                elif row["feed"] == gps.FEED:
                    outcome = gps.process_ping(conn, clean, now, max_future)
                elif row["feed"] == alerts.FEED:
                    outcome = alerts.ingest_alert(conn, clean, now)
                elif row["feed"] == bol.FEED:
                    outcome = bol.apply_bol(conn, bol.payload_to_bol(clean), now)
                else:
                    raise RowError("cannot_replay", f"No replay handler for feed {row['feed']!r}")
        except RowError as e:
            conn.execute(
                "UPDATE dead_letter SET attempts = attempts + 1, reason_code = %s, reason = %s, "
                "suggested_fix = COALESCE(%s, suggested_fix), suggested_patch = COALESCE(%s, suggested_patch) WHERE id = %s",
                (e.code, e.reason, e.fix, Jsonb(e.patch) if e.patch else None, dl_id),
            )
            audit(conn, actor, "dead_letter.replay_failed", f"dead_letter:{dl_id}",
                  {"feed": row["feed"], "reason_code": e.code, "applied": applied}, at=now)
            bump(conn, row["feed"], now, counters={"replayed_failed": 1})
            return {"ok": False, "id": dl_id, "result": "failed", "reason_code": e.code, "reason": e.reason, "suggested_fix": e.fix}
        result = RESULT.get(outcome, outcome)
        conn.execute(
            "UPDATE dead_letter SET status = 'replayed', replayed_at = %s, attempts = attempts + 1 WHERE id = %s",
            (now, dl_id),
        )
        audit(conn, actor, "dead_letter.replay", f"dead_letter:{dl_id}",
              {"feed": row["feed"], "outcome": outcome, "result": result, "applied": applied, "kept_stored": kept,
               "used": "corrected_payload" if corrected else ("suggested_patch" if applied else "original_payload")},
              at=now)
        bump(conn, row["feed"], now, accepted=1, counters={"replayed": 1, f"replayed_{result}": 1})
        return {"ok": True, "id": dl_id, "outcome": outcome, "result": result}


def discard(pool, dl_id: int, actor: str, now: datetime, note: str | None = None) -> dict:
    with pool.connection() as conn:
        row = _lock(conn, dl_id)
        conn.execute("UPDATE dead_letter SET status = 'discarded' WHERE id = %s", (dl_id,))
        audit(conn, actor, "dead_letter.discard", f"dead_letter:{dl_id}",
              {"feed": row["feed"], "reason_code": row["reason_code"], "note": note}, at=now)
        return {"ok": True, "id": dl_id}
