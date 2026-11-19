"""Mock carrier status API + alerts feed on port 9100, with runtime fault injection.

GET  /v2/consignments?carrier_id=C1   consignment status per carrier (X-Api-Key)
GET  /alerts                          IMD-style alerts issued so far (X-Api-Key)
GET  /admin/faults                    current faults (X-Admin-Key)
PUT  /admin/faults                    {"carrier": "C2" | "*", "error_rate": 0.3, "latency_ms": 800, "outage": true, "timeout": false}
DELETE /admin/faults                  clear all faults
POST /admin/erp-malformed             {"rows": 40}: drop one broken ERP export into the tower's inbox (outage simulation)
POST /hooks/chat                      mock chat webhook: records a message from the tower's outbox (X-Api-Key)
GET  /hooks/chat                      the recorded messages (X-Api-Key); DELETE clears them
Time follows the scenario clock file, so statuses and alerts move as the simulation runs.
"""

from __future__ import annotations

import asyncio
import hmac
import random
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sim.alerts_data import scenario_alerts
from sim.erp_writer import write_malformed_export
from sim.world import World
from tower.clock import Clock, FileClock
from tower.config import Settings, load_settings
from tower.logging_ import configure_logging

TIMEOUT_SLEEP_S = 30.0


class MalformedIn(BaseModel):
    rows: int = Field(40, ge=1, le=500)


class FaultIn(BaseModel):
    carrier: str = "*"
    error_rate: float = Field(0.0, ge=0, le=1)
    latency_ms: int = Field(0, ge=0, le=60000)
    outage: bool = False
    timeout: bool = False


def create_app(settings: Settings | None = None, clock: Clock | None = None, world: World | None = None) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or FileClock(settings.clock_file)
    world = world or World(settings.network_dir, settings.scenario_seed)
    prakash = next(s["id"] for s in world.net.suppliers if "prakash" in s["name"].lower())
    alerts = scenario_alerts(prakash)
    faults: dict[str, FaultIn] = {}
    chat_messages: list[dict] = []
    rng = random.Random(7)
    app = FastAPI(title="Mock carrier API")

    def check(key: str | None, expected: str) -> None:
        if not expected or not key or not hmac.compare_digest(key, expected):
            raise HTTPException(401, "bad key")

    @app.get("/health")
    def health():
        return {"ok": True, "now": clock.now().isoformat(), "faults": {k: v.model_dump() for k, v in faults.items()}}

    @app.get("/v2/consignments")
    async def consignments(carrier_id: str, x_api_key: str | None = Header(None)):
        check(x_api_key, settings.carrier_api_key)
        if carrier_id not in {"C1", "C2", "C3"}:
            raise HTTPException(404, "unknown carrier")
        f = faults.get(carrier_id) or faults.get("*")
        if f:
            if f.timeout:
                await asyncio.sleep(TIMEOUT_SLEEP_S)
            if f.latency_ms:
                await asyncio.sleep(f.latency_ms / 1000)
            if f.outage:
                return JSONResponse({"error": "service unavailable"}, status_code=503)
            if f.error_rate and rng.random() < f.error_rate:
                return JSONResponse({"error": "internal error"}, status_code=500)
        now = clock.now()
        items = []
        for s in world.shipments:
            if s.carrier == carrier_id:
                v = world.carrier_view(s, now)
                if v:
                    items.append(v)
        return {"carrier": carrier_id, "as_of": now.isoformat(), "consignments": items}

    @app.get("/alerts")
    def get_alerts(x_api_key: str | None = Header(None)):
        check(x_api_key, settings.carrier_api_key)
        now = clock.now()
        return {"as_of": now.isoformat(), "alerts": [a for a in alerts if datetime.fromisoformat(a["issued_at"]) <= now]}

    @app.post("/hooks/chat")
    def chat_post(body: dict, x_api_key: str | None = Header(None)):
        check(x_api_key, settings.carrier_api_key)
        chat_messages.append({**body, "received_at": clock.now().isoformat()})
        return {"ok": True, "count": len(chat_messages)}

    @app.get("/hooks/chat")
    def chat_get(x_api_key: str | None = Header(None)):
        check(x_api_key, settings.carrier_api_key)
        return {"count": len(chat_messages), "messages": chat_messages[-200:]}

    @app.delete("/hooks/chat")
    def chat_clear(x_api_key: str | None = Header(None)):
        check(x_api_key, settings.carrier_api_key)
        chat_messages.clear()
        return {"ok": True}

    @app.get("/admin/faults")
    def get_faults(x_admin_key: str | None = Header(None)):
        check(x_admin_key, settings.carrier_admin_key)
        return {k: v.model_dump() for k, v in faults.items()}

    @app.put("/admin/faults")
    def set_fault(body: FaultIn, x_admin_key: str | None = Header(None)):
        check(x_admin_key, settings.carrier_admin_key)
        if body.error_rate == 0 and not (body.latency_ms or body.outage or body.timeout):
            faults.pop(body.carrier, None)
        else:
            faults[body.carrier] = body
        return {k: v.model_dump() for k, v in faults.items()}

    @app.delete("/admin/faults")
    def clear_faults(x_admin_key: str | None = Header(None)):
        check(x_admin_key, settings.carrier_admin_key)
        faults.clear()
        return {}

    @app.post("/admin/erp-malformed")
    def erp_malformed(body: MalformedIn | None = None, x_admin_key: str | None = Header(None)):
        check(x_admin_key, settings.carrier_admin_key)
        path, rows = write_malformed_export(world, clock.now(), settings.inbox, rows=(body or MalformedIn()).rows)
        return {"file": path.name, "rows": rows}

    return app


def main() -> None:
    settings = load_settings()
    configure_logging()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.carrier_api_port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
