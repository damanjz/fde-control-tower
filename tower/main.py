"""FastAPI app: ingestion endpoints, dead-letter operations and read APIs. Workers run inside the process."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from tower import alerts, gps, replay, routes
from tower.access import authenticate
from tower.bol import BolWorker
from tower.carrier import CarrierPoller
from tower.clock import Clock, FileClock
from tower.config import Settings, load_settings
from tower.db import open_pool
from tower.erp import ErpWorker
from tower.logging_ import log
from tower.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, admin_ok, verify
from tower.workers import ImpactWorker, OutboxWorker

logger = logging.getLogger("tower.api")
MAX_BATCH = 500


def create_app(settings: Settings | None = None, *, clock: Clock | None = None, start_workers: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or FileClock(settings.clock_file)
    run_workers = settings.workers if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = open_pool(settings.db_url)
        app.state.pool = pool
        stop = threading.Event()
        threads: list[threading.Thread] = []
        if run_workers:
            threads.append(ErpWorker(pool, settings, clock, stop))
            for cid in ("C1", "C2", "C3"):
                threads.append(CarrierPoller(pool, settings, clock, cid, stop))
            threads.append(alerts.AlertsPoller(pool, settings, clock, stop))
            threads += [BolWorker(pool, settings, clock, stop), ImpactWorker(pool, settings, clock, stop),
                        OutboxWorker(pool, settings, stop)]
            for t in threads:
                t.start()
        log(logger, logging.INFO, "tower started", port=settings.port, workers=run_workers)
        yield
        stop.set()
        for t in threads:
            t.join(timeout=5)
        pool.close()

    app = FastAPI(title="Tarangini control tower: ingestion", lifespan=lifespan)

    def pool(request: Request):
        return request.app.state.pool

    def require_admin(x_admin_key: str | None) -> None:
        if not admin_ok(settings.admin_key, x_admin_key):
            raise HTTPException(401, "admin key required")

    def dead_letter_actor(x_admin_key: str | None, x_actor: str | None, authorization: str | None) -> str:
        """Replay and discard: the admin key (scripts), or the bearer token of a user who may read the queue (the UI)."""
        if admin_ok(settings.admin_key, x_admin_key):
            return x_actor or "admin"
        user = authenticate(settings, authorization)
        if user is None:
            raise HTTPException(401, "admin key or bearer token required")
        if not user.dead_letters:
            raise HTTPException(403, "dead letters are for VP Operations only")
        return user.id

    def q(request: Request, sql: str, params=()) -> list[dict]:
        with pool(request).connection() as conn:
            return conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- health
    @app.get("/health")
    def health():
        return {"ok": True, "now": clock.now().isoformat()}

    # ---------------------------------------------------------------- ingest
    @app.post("/ingest/gps")
    async def ingest_gps(request: Request):
        body = await request.body()
        now = clock.now()
        try:
            verify(settings.gps_secret, body, request.headers.get(SIGNATURE_HEADER), request.headers.get(TIMESTAMP_HEADER),
                   now.timestamp(), settings.gps_window_s)
        except SignatureError as e:
            gps.reject_signature(pool(request), now, e.code)
            log(logger, logging.WARNING, "gps webhook rejected", code=e.code)
            return JSONResponse({"error": e.code}, status_code=401)
        try:
            data = json.loads(body)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        pings = data.get("pings") if isinstance(data, dict) and "pings" in data else [data]
        if not isinstance(pings, list) or not all(isinstance(p, dict) for p in pings):
            raise HTTPException(400, "expected a ping object or {'pings': [...]}")
        if len(pings) > MAX_BATCH:
            raise HTTPException(413, f"at most {MAX_BATCH} pings per request")
        return gps.process_batch(pool(request), pings, now, timedelta(seconds=settings.gps_max_future_s))

    # ---------------------------------------------------------------- dead letters
    @app.post("/dead-letters/{dl_id}/replay")
    def dead_letter_replay(request: Request, dl_id: int, body: dict | None = Body(default=None),
                           x_admin_key: str | None = Header(None), x_actor: str | None = Header(None),
                           authorization: str | None = Header(None)):
        actor = dead_letter_actor(x_admin_key, x_actor, authorization)
        corrected = (body or {}).get("payload")
        if corrected is not None and not isinstance(corrected, dict):
            raise HTTPException(400, "payload must be an object")
        try:
            res = replay.replay(pool(request), dl_id, corrected, actor, clock.now(),
                                timedelta(seconds=settings.gps_max_future_s))
        except replay.NotFound:
            raise HTTPException(404, "no such dead letter") from None
        except replay.NotWaiting as e:
            raise HTTPException(409, f"dead letter is already {e}") from None
        return JSONResponse(res, status_code=200 if res["ok"] else 422)

    @app.post("/dead-letters/{dl_id}/discard")
    def dead_letter_discard(request: Request, dl_id: int, body: dict | None = Body(default=None),
                            x_admin_key: str | None = Header(None), x_actor: str | None = Header(None),
                            authorization: str | None = Header(None)):
        actor = dead_letter_actor(x_admin_key, x_actor, authorization)
        try:
            return replay.discard(pool(request), dl_id, actor, clock.now(), (body or {}).get("note"))
        except replay.NotFound:
            raise HTTPException(404, "no such dead letter") from None
        except replay.NotWaiting as e:
            raise HTTPException(409, f"dead letter is already {e}") from None

    @app.get("/audit-log")
    def audit_log(request: Request, x_admin_key: str | None = Header(None), action: str | None = None,
                  limit: int = Query(100, ge=1, le=1000)):
        require_admin(x_admin_key)
        if action:
            return q(request, "SELECT * FROM audit_log WHERE action = %s ORDER BY id DESC LIMIT %s", (action, limit))
        return q(request, "SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))

    # ---------------------------------------------------------------- authenticated, scoped read and action API (milestone 2)
    routes.register(app, settings, clock)

    return app
