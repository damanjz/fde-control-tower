"""Authenticated read and action API. Every read goes through tower.scoped.ScopedStore; nothing here queries the
database directly except via it (tests/test_rbac_db.py checks that).

GET  /me                         who am I, what may I see and do
GET  /incidents                  ranked by exposure, scoped
GET  /incidents/{id}/brief       everything the UI brief needs in one call
POST /incidents/{id}/triage      run the triage agent (body {"use_cache": true} returns the latest run for the role)
POST /playbooks/{id}/run         execute (<= threshold) or park for approval
POST /playbooks/{id}/approve     execute a parked playbook (VP Operations)
GET  /contracts/{chain}/pdf       the chain's contract PDF (same rule as clause reads)
GET  /audit                      scoped audit trail (?morning=true: the current scenario morning only)
GET  /network, /shipments, /shipments/{id}, /disruptions, /dead-letters, /health/feeds
GET  /live                       milestone 3: what the live map needs in one poll (trucks with risk, plans, circuits)
GET  /outage, POST /outage       milestone 3: the outage simulation switch (VP Operations may switch it)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from tower import actions, agent, outage, risk
from tower.access import USERS, User, authenticate
from tower.clock import Clock
from tower.config import Settings
from tower.scoped import ScopedStore


def register(app: FastAPI, settings: Settings, clock: Clock) -> None:
    def current_user(authorization: str | None = Header(None)) -> User:
        user = authenticate(settings, authorization)
        if user is None:
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        return user

    def store(request: Request, user: User = Depends(current_user)) -> ScopedStore:
        return ScopedStore(request.app.state.pool, user, clock.now(), settings)

    # ---------------------------------------------------------------- identity
    @app.get("/me")
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "name": user.name, "role": user.role,
                "scope": "all India" if not user.scoped else {"dcs": list(user.dcs)},
                "can_execute": user.can_execute, "can_approve": user.can_approve,
                "approval_threshold_inr": settings.approval_threshold_inr,
                "field_access": {"driver_contact": user.driver_contact, "carrier_contact": user.carrier_contact,
                                 "carrier_bank": user.bank, "contracts": user.contracts}}

    # ---------------------------------------------------------------- incidents
    @app.get("/incidents")
    def incidents(status: str | None = Query(None, pattern="^(active|resolved)$"), kind: str | None = None,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), st: ScopedStore = Depends(store)):
        rows = st.incidents(status, kind)
        return {"total": len(rows), "items": rows[offset:offset + limit]}

    @app.get("/incidents/{incident_id}/brief")
    def brief(incident_id: int, limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
              st: ScopedStore = Depends(store)):
        inc = st.incident(incident_id)
        if inc is None:
            raise HTTPException(404, "no such incident")
        money = st.exposure_breakdown(incident_id)
        deadline = money["notify_deadline"]
        money["hours_left_to_notify"] = (round((deadline - clock.now()).total_seconds() / 3600, 2)
                                         if deadline and inc["kind"] == "weather" else None)
        chain = st.chain_counts(incident_id)
        run = st.latest_run(incident_id)
        playbooks = [p for p in st.playbooks(incident_id) if p["status"] != "rejected"]
        clauses, seen = [], set()
        for p in playbooks:
            key = (p["clause_chain"], p["clause_id"])
            if p["clause_id"] and p["clause_chain"] and key not in seen:
                seen.add(key)
                c = st.clause(p["clause_chain"], p["clause_id"])
                if c:
                    clauses.append({"chain": c["chain"], "clause_id": c["clause_id"], "heading": c["heading"], "page": c["page"],
                                    "text": c["text"], "verified": c["verified"], "quoted_by_playbook": p["id"]})
        return {
            "incident": inc,
            "money": {**money, "unverified_sources": st.unverified_sources(incident_id) if money.get("unverified_rows") else []},
            "chain": {"source": {"kind": inc["kind"], "title": inc["title"], "severity": inc["severity"]}, **chain},
            "agent": run,
            "clauses": clauses,
            "playbooks": playbooks,
            "shipments": {"total": st.impact_count(incident_id), "limit": limit, "offset": offset,
                          "items": st.impact_rows(incident_id, limit=limit, offset=offset)},
        }

    @app.get("/contracts/{chain}/pdf")
    def contract_pdf(chain: str, st: ScopedStore = Depends(store)):
        """The chain's signed contract PDF, for the UI's clause citations. Only for a role that may read that chain's
        clauses (the same rule as clause reads); only files the ingest recorded, inside data/contracts/."""
        name, exists = st.contract_file(chain)
        if not exists:
            raise HTTPException(404, "no such contract")
        if name is None:
            raise HTTPException(403, "this role may not read that chain's contract")
        base = settings.contracts_dir.resolve()
        path = (base / Path(name).name).resolve()
        if path.parent != base or path.suffix.lower() != ".pdf" or not path.is_file():
            raise HTTPException(404, "no such contract")
        return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{path.name}"'})

    @app.post("/incidents/{incident_id}/triage")
    def triage(incident_id: int, request: Request, body: dict | None = Body(default=None), user: User = Depends(current_user),
               st: ScopedStore = Depends(store)):
        try:
            return agent.triage(request.app.state.pool, settings, clock, user, st, incident_id,
                                use_cache=bool((body or {}).get("use_cache")))
        except LookupError:
            raise HTTPException(404, "no such incident") from None

    @app.post("/incidents/recompute")
    def recompute(request: Request, user: User = Depends(current_user)):
        if user.id != "asha":
            raise HTTPException(403, "only VP Operations may force a recompute")
        return {"incidents": risk.recompute_all(request.app.state.pool, clock.now())}

    # ---------------------------------------------------------------- playbooks
    @app.get("/playbooks/{playbook_id}")
    def playbook(playbook_id: int, st: ScopedStore = Depends(store)):
        pb = st.playbook(playbook_id)
        if pb is None:
            raise HTTPException(404, "no such playbook")
        return pb

    @app.post("/playbooks/{playbook_id}/run")
    def run_playbook(playbook_id: int, request: Request, user: User = Depends(current_user), st: ScopedStore = Depends(store)):
        try:
            return actions.run_playbook(request.app.state.pool, settings, clock, user, st, playbook_id)
        except actions.ActionError as e:
            raise HTTPException(e.status, e.message) from None

    @app.post("/playbooks/{playbook_id}/approve")
    def approve_playbook(playbook_id: int, request: Request, user: User = Depends(current_user), st: ScopedStore = Depends(store)):
        try:
            return actions.approve_playbook(request.app.state.pool, settings, clock, user, st, playbook_id)
        except actions.ActionError as e:
            raise HTTPException(e.status, e.message) from None

    # ---------------------------------------------------------------- audit
    @app.get("/audit")
    def audit_trail(action: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    morning: bool = False, st: ScopedStore = Depends(store)):
        return st.audit_rows(limit=limit, offset=offset, action=action, morning=morning)

    # ---------------------------------------------------------------- M1 read endpoints, now authenticated and scoped
    @app.get("/network")
    def network(st: ScopedStore = Depends(store)):
        return st.network()

    @app.get("/shipments")
    def shipments(dc: str | None = None, zone: str | None = None, carrier: str | None = None, status: str | None = None,
                  customer_id: int | None = None, min_version: int | None = None, limit: int = Query(100, ge=1, le=1000),
                  offset: int = Query(0, ge=0), st: ScopedStore = Depends(store)):
        return st.shipments(dc=dc, zone=zone, carrier=carrier, status=status, customer_id=customer_id,
                            min_version=min_version, limit=limit, offset=offset)

    @app.get("/shipments/{shipment_id}")
    def shipment_detail(shipment_id: str, st: ScopedStore = Depends(store)):
        row = st.shipment(shipment_id)
        if row is None:
            raise HTTPException(404, "no such shipment")
        return row

    @app.get("/disruptions")
    def disruptions(status: str | None = Query(None, pattern="^(active|resolved)$"), kind: str | None = None,
                    st: ScopedStore = Depends(store)):
        return st.incidents(status, kind)

    @app.get("/dead-letters")
    def dead_letters(status: str | None = Query(None, pattern="^(waiting|replayed|discarded)$"), feed: str | None = None,
                     reason_code: str | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                     st: ScopedStore = Depends(store)):
        out = st.dead_letters(status=status, feed=feed, reason_code=reason_code, limit=limit, offset=offset)
        if out is None:
            raise HTTPException(403, "dead letters are visible to VP Operations only")
        return out

    @app.get("/health/feeds")
    def health_feeds(st: ScopedStore = Depends(store)):
        now = clock.now()
        out = []
        for r in st.feed_health():
            rate = (r["accepted"] / r["received"]) if r["received"] else None
            age = (now - r["last_at"]).total_seconds() if r["last_at"] else None
            status = "degraded" if r["circuit_state"] != "closed" else ("ok" if rate is None or rate >= 0.95 else "warning")
            out.append({**r, "accept_rate": rate, "seconds_since_last": age, "status": status})
        return {"now": now.isoformat(), "feeds": out}

    # ---------------------------------------------------------------- milestone 3: the UI
    @app.get("/live")
    def live(st: ScopedStore = Depends(store)):
        """Trucks still on the road with a risk status (at risk: an active incident makes it late; watch: touched by an
        incident or held by the carrier; on time otherwise), the executed plan per incident, carrier circuits, and the
        dead-letter count for a role that may read the queue. `exposure_inr` counts each shipment once, at its largest
        net penalty, because incident exposures overlap."""
        ships = st.live_shipments()
        for s in ships:
            s["risk"] = ("at risk" if s["days_late"] > 0 else
                         "watch" if s["incidents"] or s["carrier_status"] == "held" else "on time")
        return {
            "now": clock.now().isoformat(),
            "shipments": ships,
            "exposure_inr": round(float(sum(s["net_penalty_inr"] for s in ships)), 2),
            "plans": {str(p["incident_id"]): p for p in st.executed_plans()},
            "circuits": {r["feed"].split(":", 1)[1]: r["circuit_state"] for r in st.feed_health() if r["feed"].startswith("carrier:")},
            "dead_letters_waiting": st.dead_letters_waiting(),
        }

    @app.get("/outage")
    def outage_status(request: Request, user: User = Depends(current_user)):
        return outage.status(request.app.state.pool, settings, user)

    @app.post("/outage")
    def outage_switch(request: Request, body: dict = Body(...), user: User = Depends(current_user)):
        if not isinstance(body.get("on"), bool):
            raise HTTPException(400, 'body must be {"on": true} or {"on": false}')
        try:
            return outage.switch(request.app.state.pool, settings, clock, user, body["on"])
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except outage.SimUnavailable as e:
            raise HTTPException(503, str(e)) from None


__all__ = ["register", "USERS"]
