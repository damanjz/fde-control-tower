"""GPS webhook simulator: signed batches of pings with out-of-order, duplicate, skewed and forged traffic."""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import timedelta

import httpx

from sim.world import World
from tower.clock import Clock
from tower.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

PING_EVERY = timedelta(minutes=20)       # scenario time between pings for one truck
BATCH = 100
RATES = {"delay": 0.08, "dup": 0.03, "skew": 0.012}


class GpsSender:
    def __init__(self, world: World, clock: Clock, url: str, secret: str, seed: int = 3, client: httpx.Client | None = None):
        self.world, self.clock, self.url, self.secret = world, clock, url.rstrip("/"), secret
        self.rng = random.Random(seed)
        self.client = client or httpx.Client(timeout=10)
        self.last: dict[str, object] = {}
        self.delayed: list[tuple[int, dict]] = []
        self.tick_no = 0
        self.stats: Counter[str] = Counter()

    def _ping(self, s, t) -> dict:
        lat, lon, speed = self.world.position(s, t)
        return {"ping_id": f"{s.id}-{int(t.timestamp())}", "shipment_id": s.id, "vehicle": f"{s.dc[3:]}-{int(s.id[3:]) % 90 + 10:02d}-TR-{int(s.id[3:]) % 9000 + 1000}",
                "lat": lat, "lon": lon, "speed_kmph": speed, "event_ts": t.isoformat()}

    def _post(self, pings: list[dict], secret: str) -> int:
        body = json.dumps({"pings": pings}).encode()
        ts = int(self.clock.now().timestamp())
        r = self.client.post(self.url + "/ingest/gps", content=body, headers={
            "Content-Type": "application/json", TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign(secret, ts, body)})
        return r.status_code

    def tick(self) -> None:
        now = self.clock.now()
        self.tick_no += 1
        out: list[dict] = []
        for s in self.world.in_transit(now):
            last = self.last.get(s.id)
            if last is None:  # stagger first pings so trucks do not all report at once
                last = now - PING_EVERY * self.rng.random()
                self.last[s.id] = last
            if now - last < PING_EVERY:
                continue
            self.last[s.id] = now
            ping = self._ping(s, now)
            r = self.rng.random()
            if r < RATES["delay"]:
                self.delayed.append((self.tick_no + self.rng.randrange(2, 5), ping))
                self.stats["delayed"] += 1
                continue
            if r < RATES["delay"] + RATES["skew"]:
                ping = {**ping, "event_ts": (now + timedelta(minutes=self.rng.randrange(20, 46))).isoformat()}
                self.stats["skewed"] += 1
            out.append(ping)
            if self.rng.random() < RATES["dup"]:
                out.append(dict(ping))
                self.stats["duplicates"] += 1
        due = [p for at, p in self.delayed if at <= self.tick_no]
        self.delayed = [(at, p) for at, p in self.delayed if at > self.tick_no]
        out += due
        for i in range(0, len(out), BATCH):
            chunk = out[i:i + BATCH]
            try:
                code = self._post(chunk, self.secret)
            except httpx.HTTPError:
                self.stats["transport_errors"] += 1
                continue
            self.stats["sent"] += len(chunk)
            if code != 200:
                self.stats["http_errors"] += 1
        if self.tick_no % 5 == 0 and out:  # someone with the wrong secret
            try:
                code = self._post(out[:1], "not-the-secret")
                self.stats["forged"] += 1
                self.stats["forged_rejected"] += int(code == 401)
            except httpx.HTTPError:
                self.stats["transport_errors"] += 1
