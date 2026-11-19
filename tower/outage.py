"""The UI's Outage switch: 30% of carrier API calls fail and one malformed ERP export lands in the inbox.

Both faults live in the simulator (the mock carrier API on :9100). The tower asks for them over the simulator's admin
API, the same way the scenario driver injects its carrier faults (`PUT /admin/faults`), plus `POST /admin/erp-malformed`.
The switch state is the simulator's own fault table; when it was switched on, by whom, and the feed counters at that
moment come from the audit log, so the UI can show what the outage has done since.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from tower.access import User
from tower.clock import Clock
from tower.config import Settings
from tower.store import audit

ERROR_RATE = 0.3
MALFORMED_ROWS = 40
ACTION = "sim.outage"


class SimUnavailable(Exception):
    pass


def may_toggle(user: User) -> bool:
    return user.role == "vp_operations"


def sim_client(settings: Settings) -> httpx.Client:
    """The simulator's admin API. Tests replace this with a mock transport."""
    return httpx.Client(base_url=settings.carrier_api_url, timeout=3.0, headers={"X-Admin-Key": settings.carrier_admin_key})


def _faults(settings: Settings) -> dict | None:
    try:
        with sim_client(settings) as c:
            r = c.get("/admin/faults")
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError:
        return None


def _feeds(conn) -> dict:
    rows = conn.execute("SELECT feed, received, accepted, rejected FROM feed_health").fetchall()
    return {r["feed"]: {"received": r["received"], "accepted": r["accepted"], "rejected": r["rejected"]} for r in rows}


def _last(conn) -> dict | None:
    return conn.execute("SELECT at, actor, detail FROM audit_log WHERE action = %s ORDER BY id DESC LIMIT 1", (ACTION,)).fetchone()


def status(pool, settings: Settings, user: User) -> dict:
    faults = _faults(settings)
    wild = (faults or {}).get("*") or {}
    active = bool(wild.get("error_rate"))
    with pool.connection() as conn:
        last = _last(conn)
        feeds = _feeds(conn)
    out: dict = {"active": active, "simulator_up": faults is not None, "error_rate": wild.get("error_rate", 0.0),
                 "can_toggle": may_toggle(user), "since": None, "by": None, "erp_file": None, "erp_rows": 0,
                 "carrier_polls": 0, "carrier_failed_polls": 0, "erp_rejected": 0}
    if active and last and last["detail"].get("on"):
        then = last["detail"].get("feeds", {})

        def delta(feed: str, key: str) -> int:
            return max(0, feeds.get(feed, {}).get(key, 0) - then.get(feed, {}).get(key, 0))

        carriers = [f for f in feeds if f.startswith("carrier:")]
        out.update(since=last["at"].isoformat(), by=last["actor"], erp_file=last["detail"].get("erp_file"),
                   erp_rows=last["detail"].get("erp_rows", 0),
                   carrier_polls=sum(delta(f, "received") for f in carriers),
                   carrier_failed_polls=sum(delta(f, "rejected") for f in carriers),
                   erp_rejected=delta("erp", "rejected"))
    return out


def switch(pool, settings: Settings, clock: Clock, user: User, on: bool) -> dict:
    """Start or stop the outage simulation. Starting while it runs does nothing (no second ERP file)."""
    if not may_toggle(user):
        raise PermissionError("only VP Operations can simulate an outage")
    faults = _faults(settings)
    if faults is None:
        raise SimUnavailable("the simulator (mock carrier API) is not answering")
    if on == bool(((faults or {}).get("*") or {}).get("error_rate")):
        return status(pool, settings, user)
    now: datetime = clock.now()
    with pool.connection() as conn:   # counters before the faults land: the ERP worker picks the file up within a second
        detail: dict = {"on": on, "feeds": _feeds(conn)}
    try:
        with sim_client(settings) as c:
            if on:
                c.put("/admin/faults", json={"carrier": "*", "error_rate": ERROR_RATE}).raise_for_status()
                made = c.post("/admin/erp-malformed", json={"rows": MALFORMED_ROWS})
                made.raise_for_status()
                detail.update(error_rate=ERROR_RATE, erp_file=made.json()["file"], erp_rows=made.json()["rows"])
            else:
                c.delete("/admin/faults").raise_for_status()
    except httpx.HTTPError as e:
        raise SimUnavailable(f"the simulator refused the change: {type(e).__name__}") from None
    with pool.connection() as conn:
        audit(conn, user.id, ACTION, "simulator", detail, at=now)
    return status(pool, settings, user)
