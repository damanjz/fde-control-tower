"""ERP export ingestion: header mapping, canonicalisation, idempotent upsert, file worker."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from psycopg import Connection
from pydantic import BaseModel, ValidationError, field_validator

from tower.clock import IST, Clock
from tower.config import Settings
from tower.logging_ import log
from tower.parsing import ParseError, norm, parse_amount, parse_datetime, parse_int, to_utc_iso
from tower.store import RowError, add_dead_letter, audit, bump

logger = logging.getLogger("tower.erp")

FEED = "erp"

# Canonical field -> normalised header spellings seen in the wild.
HEADER_ALIASES: dict[str, set[str]] = {
    "shipment_no": {"shipmentno", "shipmentid", "shipref", "shipmentref", "shipno", "shipid"},
    "order_no": {"orderno", "sonumber", "soref", "salesorder", "orderref", "sono"},
    "dc": {"dc", "dccode", "origindc", "warehouse", "whse", "plant"},
    "city": {"citycode", "city", "destcity", "dest", "destination", "deliverycity", "zone", "town"},
    "carrier": {"carrier", "carriername", "carriernm", "transporter", "trucker"},
    "customer": {"customer", "customername", "cust", "custname", "buyer"},
    "cases": {"cases", "totalcases", "qtycases", "noofcases"},
    "value": {"amount", "invoicevalueinr", "invoicevalue", "invamt", "value", "invoiceamount", "totalamount"},
    "status": {"status", "shipstatus", "sts"},
    "departed_at": {"dispatchdt", "departed", "dispatchdate", "shipdate", "depdt", "departuretime"},
    "eta": {"eta", "expectedarrival", "etadt", "expecteddate"},
    "promised_at": {"promisedt", "promisedby", "promisedate", "promisedeta"},
    "items": {"items", "lineitems", "skulines", "lines"},
}
_ALIAS_TO_FIELD = {a: f for f, aliases in HEADER_ALIASES.items() for a in aliases}
REQUIRED_FIELDS = ("shipment_no",)

STATUS_ALIASES = {
    "intransit": "in_transit", "dispatched": "in_transit", "shipped": "in_transit", "enroute": "in_transit",
    "planned": "planned", "created": "planned", "open": "planned",
    "held": "held", "onhold": "held", "hold": "held",
    "delivered": "delivered", "pod": "delivered", "complete": "delivered", "completed": "delivered",
}
ZONE_EXTRA_CODES = {"BLR": "Z05", "BOM": "Z11", "MAA": "Z06", "CCU": "Z29", "VTZ": "Z03", "COK": "Z08",
                    "GOI": "Z17", "VNS": "Z36", "DEL": "Z20", "HYD": "Z01"}
SKU_RE = re.compile(r"([A-Za-z]{2}-\d{3})\s*[:xX*]\s*(\d+)")


# ------------------------------------------------------------------ reference data
@dataclass
class RefData:
    zones: dict[str, dict] = field(default_factory=dict)          # zone id -> row
    zone_alias: dict[str, str] = field(default_factory=dict)      # normalised alias -> zone id
    dc_alias: dict[str, str] = field(default_factory=dict)
    carrier_alias: dict[str, str] = field(default_factory=dict)
    customers: dict[tuple[str, str], int] = field(default_factory=dict)   # (norm chain, zone id) -> customer id
    products: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: Connection) -> RefData:
        ref = cls()
        for z in conn.execute("SELECT id, city, state, serving_dc, std_transit_days FROM zone").fetchall():
            ref.zones[z["id"]] = z
        codes: dict[str, list[str]] = {}
        for z in ref.zones.values():
            ref.zone_alias[norm(z["id"])] = z["id"]
            ref.zone_alias[norm(z["city"])] = z["id"]
            codes.setdefault(norm(z["city"])[:3], []).append(z["id"])
        for code, ids in codes.items():
            if len(ids) == 1:
                ref.zone_alias.setdefault(code, ids[0])
        for code, zid in ZONE_EXTRA_CODES.items():
            ref.zone_alias.setdefault(code.lower(), zid)
        for d in conn.execute("SELECT id, city FROM warehouse").fetchall():
            ref.dc_alias[norm(d["id"])] = d["id"]
            ref.dc_alias[norm(d["id"].removeprefix("DC-"))] = d["id"]
            ref.dc_alias[norm(d["city"].split()[0])] = d["id"]
        for c in conn.execute("SELECT id, name FROM carrier").fetchall():
            ref.carrier_alias[norm(c["id"])] = c["id"]
            ref.carrier_alias[norm(c["name"])] = c["id"]
            ref.carrier_alias[norm(c["name"].split()[0])] = c["id"]
        for c in conn.execute("SELECT id, chain, zone_id FROM customer").fetchall():
            ref.customers[(norm(c["chain"]), c["zone_id"])] = c["id"]
        for p in conn.execute("SELECT sku, unit_price, case_qty FROM product").fetchall():
            ref.products[p["sku"].upper()] = p
        return ref

    def resolve_zone(self, raw: str) -> str | None:
        return self.zone_alias.get(norm(raw))


# ------------------------------------------------------------------ canonical record
class ShipmentIn(BaseModel):
    shipment_no: str
    order_no: str
    dc_id: str
    zone_id: str
    carrier_id: str
    customer_id: int
    cases: int
    value_inr: Decimal
    status: str
    departed_at: datetime
    eta: datetime
    promised_at: datetime
    lines: list[tuple[str, int, Decimal]]   # sku, cases, value

    @field_validator("cases")
    @classmethod
    def _cases_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("cases must be positive")
        return v

    def content_hash(self) -> str:
        data = {
            "n": self.shipment_no, "o": self.order_no, "dc": self.dc_id, "z": self.zone_id, "c": self.carrier_id,
            "cu": self.customer_id, "cs": self.cases, "v": str(self.value_inr), "st": self.status,
            "d": to_utc_iso(self.departed_at), "e": to_utc_iso(self.eta), "p": to_utc_iso(self.promised_at),
            "l": sorted((s, c, str(v)) for s, c, v in self.lines),
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def canonicalize(raw: dict[str, str], ref: RefData) -> ShipmentIn:
    """Raw strings keyed by canonical field -> validated ShipmentIn, or RowError with a suggested fix."""
    g = lambda k: (raw.get(k) or "").strip()  # noqa: E731

    shipment_no = g("shipment_no").upper()
    if not shipment_no:
        raise RowError("missing_shipment_no", "Shipment number is blank", "Discard, or key the shipment number by hand")
    order_no = g("order_no") or shipment_no

    zone_raw = g("city")
    zone_id = ref.resolve_zone(zone_raw) if zone_raw else None
    if zone_id is None:
        patch = None
        fix = "Add the city to the zone table or correct the code"
        m = re.match(r"^([A-Za-z]{3,})\W*\d+$", zone_raw)
        base = ref.resolve_zone(m.group(1)) if m else None
        if base:
            c = ref.zones[base]
            patch = {"city": base}
            fix = f"Map '{zone_raw}' to {base} {c['city']}"
        raise RowError("unknown_zone", f"City code not in the zone table: {zone_raw!r}", fix, patch)
    zone = ref.zones[zone_id]

    dc_raw = g("dc")
    if dc_raw:
        dc_id = ref.dc_alias.get(norm(dc_raw))
        if dc_id is None:
            raise RowError("unknown_dc", f"Warehouse not recognised: {dc_raw!r}",
                           f"Use the lane's serving DC: {zone['serving_dc']}", {"dc": zone["serving_dc"]})
    else:
        dc_id = zone["serving_dc"]

    carrier_raw = g("carrier")
    carrier_id = ref.carrier_alias.get(norm(carrier_raw)) or ref.carrier_alias.get(norm(carrier_raw.split(" ")[0]))
    if carrier_id is None:
        raise RowError("unknown_carrier", f"Carrier not recognised: {carrier_raw!r}", "Pick the carrier from the master list")

    m = re.match(r"^(.*?)\s*\(.*\)$", g("customer"))
    chain = m.group(1) if m else g("customer")
    customer_id = ref.customers.get((norm(chain), zone_id))
    if customer_id is None:
        raise RowError("unknown_customer", f"No customer {g('customer')!r} in {zone['city']}",
                       "Check the customer master for this city")

    status = STATUS_ALIASES.get(norm(g("status")))
    if status is None:
        raise RowError("bad_status", f"Status not recognised: {g('status')!r}", "Map the status in the ERP export")

    # lines first: a bad amount can be recomputed from them
    lines: dict[str, list] = {}
    for sku, qty in SKU_RE.findall(g("items")):
        sku = sku.upper()
        if sku not in ref.products:
            raise RowError("unknown_sku", f"SKU not in the product master: {sku}", "Check the SKU against the product master")
        p = ref.products[sku]
        cur = lines.setdefault(sku, [0, Decimal(0)])
        cur[0] += int(qty)
        cur[1] += Decimal(int(qty)) * p["case_qty"] * p["unit_price"]
    if not lines:
        raise RowError("no_lines", "Shipment has no readable line items", "Re-export the shipment with its lines")
    line_list = [(s, c, v.quantize(Decimal("0.01"))) for s, (c, v) in lines.items()]
    computed = sum((v for _, _, v in line_list), Decimal(0))

    try:
        value = parse_amount(g("value"))
    except ParseError as e:
        raise RowError("bad_amount", f"Invoice amount unreadable: {e.message}",
                       f"Use the value computed from the lines: {computed:,.2f}", {"value": str(computed)}) from None
    try:
        cases = parse_int(g("cases"), "cases") if g("cases") else sum(c for _, c, _ in line_list)
        departed = parse_datetime(g("departed_at"))
        eta = parse_datetime(g("eta"))
        promised = parse_datetime(g("promised_at"))
    except ParseError as e:
        raise RowError(e.code, e.message, "Correct the value in the payload and replay") from None
    if departed is None:
        raise RowError("missing_departed_at", "Dispatch date is blank", "Discard, or key the dispatch date by hand")
    std = timedelta(days=zone["std_transit_days"])
    if promised is None:
        fix_dt = (departed + std).astimezone(IST)
        raise RowError("missing_promised_at", "Promised date is blank",
                       f"Use the lane's standard transit: {zone['std_transit_days']} day(s), "
                       f"i.e. {fix_dt:%d/%m/%Y %H:%M} IST", {"promised_at": fix_dt.isoformat()})
    if eta is None:
        eta = promised
    if eta < departed:
        raise RowError("eta_before_departure", "ETA is earlier than the dispatch time", "Correct the ETA and replay")

    try:
        return ShipmentIn(
            shipment_no=shipment_no, order_no=order_no, dc_id=dc_id, zone_id=zone_id, carrier_id=carrier_id,
            customer_id=customer_id, cases=cases, value_inr=value, status=status, departed_at=departed,
            eta=eta, promised_at=promised, lines=line_list,
        )
    except ValidationError as e:
        err = e.errors()[0]
        raise RowError("schema_invalid", f"{'.'.join(map(str, err['loc']))}: {err['msg']}", "Correct the field and replay") from None


# ------------------------------------------------------------------ upsert
def upsert_shipment(conn: Connection, s: ShipmentIn, exported_at: datetime, source: str = "erp") -> str:
    """Return 'new', 'updated', 'stale' (older export than what we hold) or 'duplicate' (same content, not older).

    `source_exported_at` is the newest export that carried the stored content. A duplicate from a newer export moves
    it forward; otherwise a replayed row from an export in between would pass the staleness check."""
    h = s.content_hash()
    cur = conn.execute(
        "SELECT version, content_hash, source_exported_at FROM shipment WHERE id = %s FOR UPDATE", (s.shipment_no,)
    ).fetchone()
    params = {
        "id": s.shipment_no, "order_no": s.order_no, "dc": s.dc_id, "zone": s.zone_id, "carrier": s.carrier_id,
        "cust": s.customer_id, "cases": s.cases, "value": s.value_inr, "status": s.status, "dep": s.departed_at,
        "eta": s.eta, "prom": s.promised_at, "src": source, "hash": h, "exp": exported_at,
    }
    if cur is None:
        conn.execute(
            """
            INSERT INTO shipment (id, order_no, dc_id, zone_id, carrier_id, customer_id, cases, value_inr, status,
                                  departed_at, eta, promised_at, source, content_hash, source_exported_at)
            VALUES (%(id)s, %(order_no)s, %(dc)s, %(zone)s, %(carrier)s, %(cust)s, %(cases)s, %(value)s, %(status)s,
                    %(dep)s, %(eta)s, %(prom)s, %(src)s, %(hash)s, %(exp)s)
            """,
            params,
        )
        outcome = "new"
    elif exported_at < cur["source_exported_at"]:
        return "stale"
    elif cur["content_hash"] == h:
        if exported_at > cur["source_exported_at"]:   # a newer export confirmed the content: move the watermark
            conn.execute("UPDATE shipment SET source_exported_at = %s WHERE id = %s", (exported_at, s.shipment_no))
        return "duplicate"
    else:
        conn.execute(
            """
            UPDATE shipment SET order_no=%(order_no)s, dc_id=%(dc)s, zone_id=%(zone)s, carrier_id=%(carrier)s,
                customer_id=%(cust)s, cases=%(cases)s, value_inr=%(value)s, status=%(status)s, departed_at=%(dep)s,
                eta=%(eta)s, promised_at=%(prom)s, source=%(src)s, content_hash=%(hash)s,
                source_exported_at=%(exp)s, version = version + 1, updated_at = now()
            WHERE id = %(id)s
            """,
            params,
        )
        conn.execute("DELETE FROM shipment_line WHERE shipment_id = %s", (s.shipment_no,))
        outcome = "updated"
    with conn.cursor() as c:
        c.executemany(
            "INSERT INTO shipment_line (shipment_id, sku, cases, value_inr) VALUES (%s, %s, %s, %s)",
            [(s.shipment_no, sku, cs, v) for sku, cs, v in s.lines],
        )
    if outcome == "new":  # a GPS ping may have arrived before the ERP row: pick up the latest position
        conn.execute(
            """
            UPDATE shipment SET last_lat = p.lat, last_lon = p.lon, last_position_at = p.event_ts
            FROM (SELECT lat, lon, event_ts FROM gps_ping WHERE shipment_id = %s ORDER BY event_ts DESC LIMIT 1) p
            WHERE shipment.id = %s
            """,
            (s.shipment_no, s.shipment_no),
        )
    return outcome


def process_row(conn: Connection, ref: RefData, raw: dict[str, str], exported_at: datetime, source: str = "erp") -> str:
    return upsert_shipment(conn, canonicalize(raw, ref), exported_at, source)


# ------------------------------------------------------------------ file reading
def _delimiter(header_line: str) -> str:
    best, best_n = ",", -1
    for d in (",", ";", "\t", "|"):
        try:
            cells = next(csv.reader([header_line], delimiter=d))
        except csv.Error:
            continue
        n = sum(1 for c in cells if norm(c) in _ALIAS_TO_FIELD)
        if n > best_n:
            best, best_n = d, n
    return best


def parse_export(text: str) -> tuple[list[str | None], list[tuple[int, dict[str, str] | None, str]]]:
    """Split an export into (mapped header fields, rows).

    Each row is (line_no, canonical dict or None when malformed, raw line). BOM, blank lines and stray
    whitespace are tolerated; a row with the wrong column count or broken quoting is malformed.
    """
    text = text.lstrip("﻿")
    lines = [(i, ln) for i, ln in enumerate(text.splitlines(), start=1) if ln.strip()]
    if not lines:
        return [], []
    delim = _delimiter(lines[0][1])
    header = next(csv.reader([lines[0][1]], delimiter=delim))
    fields = [_ALIAS_TO_FIELD.get(norm(h)) for h in header]
    rows: list[tuple[int, dict[str, str] | None, str]] = []
    for no, ln in lines[1:]:
        try:
            cells = next(csv.reader(io.StringIO(ln), delimiter=delim, strict=True))
        except (csv.Error, StopIteration):
            rows.append((no, None, ln))
            continue
        if len(cells) != len(fields):
            rows.append((no, None, ln))
            continue
        rows.append((no, {f: c.strip() for f, c in zip(fields, cells, strict=True) if f}, ln))
    return fields, rows


def export_time(path: Path, clock: Clock) -> datetime:
    m = re.search(r"(\d{8})[_-](\d{4})", path.name)
    if m:
        d, t = m.groups()
        return datetime(int(d[:4]), int(d[4:6]), int(d[6:]), int(t[:2]), int(t[2:]), tzinfo=IST)
    return clock.now()


# ------------------------------------------------------------------ file processing
def process_file(pool, settings: Settings, clock: Clock, path: Path, *, skip_seen_check: bool = False) -> dict:
    """Process one export file. Atomic: all rows commit together or the file stays in the inbox."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    at = clock.now()
    exported_at = export_time(path, clock)
    stats = {"file": path.name, "rows": 0, "new": 0, "updated": 0, "duplicate": 0, "stale": 0,
             "dead_letter": 0, "dead_letter_repeat": 0, "file_duplicate": False}
    with pool.connection() as conn:
        seen = conn.execute("SELECT 1 FROM ingest_log WHERE feed = %s AND idem_key = %s", (FEED, sha)).fetchone()
        if seen and not skip_seen_check:
            stats["file_duplicate"] = True
            bump(conn, FEED, at, counters={"files_duplicate": 1})
        else:
            fields, rows = parse_export(raw.decode("utf-8", errors="replace"))
            ref = RefData.load(conn)
            if not any(fields) or not all(f in fields for f in REQUIRED_FIELDS):
                payload = {"_file": path.name, "headers": [str(f) for f in fields]}
                add_dead_letter(conn, FEED, at, payload, RowError(
                    "unmapped_headers", f"Cannot map the header of {path.name} to the canonical schema",
                    "Send the file back to the ERP team, or add the header spellings to the alias table"))
                bump(conn, FEED, at, received=1, rejected=1, error=f"{path.name}: unmapped headers",
                     counters={"files_rejected": 1})
                stats["rejected_file"] = True
            else:
                _process_rows(conn, ref, rows, path.name, exported_at, at, stats)
                conn.execute(
                    "INSERT INTO ingest_log (feed, idem_key, meta) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (feed, idem_key) DO NOTHING",
                    (FEED, sha, json.dumps({"file": path.name, "rows": stats["rows"]})),
                )
                bump(conn, FEED, at, received=stats["rows"],
                     accepted=stats["new"] + stats["updated"] + stats["duplicate"] + stats["stale"],
                     rejected=stats["dead_letter"] + stats["dead_letter_repeat"], clear_error=True,
                     error=f"{stats['dead_letter']} rows to dead letters in {path.name}" if stats["dead_letter"] else None,
                     counters={"files": 1, "new": stats["new"], "updated": stats["updated"],
                               "duplicates": stats["duplicate"], "stale": stats["stale"],
                               "dead_letters": stats["dead_letter"]})
                audit(conn, "system", "erp.file_processed", path.name,
                      {k: v for k, v in stats.items() if k != "file"}, at=at)
    return stats


def _process_rows(conn, ref, rows, filename, exported_at, at, stats) -> None:
    seen_in_file: dict[str, str] = {}
    for line_no, row, raw_line in rows:
        stats["rows"] += 1
        meta = {"_file": filename, "_line_no": line_no, "_exported_at": exported_at.isoformat()}
        try:
            if row is None:
                raise RowError("malformed_row", "Row does not match the header (wrong column count or broken quoting)",
                               "Discard, or re-key the row by hand and replay")
            payload = {**row, **meta}
            with conn.transaction():
                s = canonicalize(row, ref)
                h = s.content_hash()
                prev = seen_in_file.get(s.shipment_no)
                if prev is not None and prev != h:
                    raise RowError("duplicate_conflict",
                                   f"{s.shipment_no} appears twice in this export with different values",
                                   "Keep the later row (replay it) or discard it")
                seen_in_file.setdefault(s.shipment_no, h)
                outcome = upsert_shipment(conn, s, exported_at)
            stats[outcome] += 1
        except RowError as e:
            payload = {"raw_line": raw_line, **meta} if row is None else {**row, **meta}
            if add_dead_letter(conn, FEED, at, payload, e):
                stats["dead_letter"] += 1
            else:
                stats["dead_letter_repeat"] += 1


# ------------------------------------------------------------------ worker
class ErpWorker(threading.Thread):
    """Watches the inbox; files are processed oldest first and moved to data/processed/ (or rejected/)."""

    def __init__(self, pool, settings: Settings, clock: Clock, stop: threading.Event):
        super().__init__(name="erp-worker", daemon=True)
        self.pool, self.settings, self.clock, self.stop_event = pool, settings, clock, stop

    def run(self) -> None:
        self.settings.inbox.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception as e:  # keep the worker alive; the file stays for the next pass
                log(logger, logging.ERROR, "erp scan failed", error=type(e).__name__)
            self.stop_event.wait(self.settings.erp_poll_s)

    def scan_once(self) -> int:
        files = sorted(self.settings.inbox.glob("*.csv"), key=lambda p: (export_time(p, self.clock), p.name))
        for path in files:
            stats = process_file(self.pool, self.settings, self.clock, path)
            dest_dir = self.settings.rejected if stats.get("rejected_file") else self.settings.processed
            move_unique(path, dest_dir)
            log(logger, logging.INFO, "erp file processed", **stats)
        return len(files)


def move_unique(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{path.stem}.{n}{path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest
