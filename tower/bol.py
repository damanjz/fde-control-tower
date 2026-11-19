"""Bills of lading (PDF): parse with pypdf, validate against the ERP shipment, store driver and vehicle on it.

Unreadable files, unknown shipment ids and value or case mismatches go to the dead-letter queue (feed 'bol') with a
reason and a suggested fix. Driver name and phone are PII: they are stored on the shipment, masked by role on read,
and never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pypdf
from psycopg import Connection

from tower.clock import Clock
from tower.config import Settings
from tower.erp import move_unique
from tower.logging_ import log
from tower.parsing import ParseError, parse_amount
from tower.store import RowError, add_dead_letter, audit, bump

logger = logging.getLogger("tower.bol")
FEED = "bol"
VALUE_TOLERANCE = Decimal("0.02")

_FIELDS = {
    "bol_no": r"BOL\s*No", "shipment_id": r"Shipment", "carrier": r"Carrier", "vehicle": r"Vehicle", "driver": r"Driver",
    "phone": r"Driver\s*phone", "consignee": r"Consignee", "cases": r"Cases", "value": r"Declared\s*value", "date": r"Date",
}
_ORDER = ["phone", "bol_no", "shipment_id", "carrier", "vehicle", "consignee", "cases", "value", "date", "driver"]  # longer labels first


@dataclass
class Bol:
    bol_no: str
    shipment_id: str
    carrier: str
    vehicle: str
    driver: str
    phone: str
    consignee: str
    cases: int
    value: Decimal
    date: str

    def as_payload(self) -> dict:
        return {"bol_no": self.bol_no, "shipment_id": self.shipment_id, "carrier": self.carrier, "vehicle": self.vehicle,
                "driver": self.driver, "phone": self.phone, "consignee": self.consignee, "cases": self.cases,
                "value": str(self.value), "date": self.date}


def read_text(path: Path) -> str:
    try:
        return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(path)).pages)
    except Exception as e:   # corrupt PDFs raise many different errors
        raise RowError("unreadable_pdf", f"{path.name} cannot be opened as a PDF ({type(e).__name__})",
                       "Send it to a person to type in") from None


def parse_bol(path: Path) -> Bol:
    text = read_text(path)
    found: dict[str, str] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        for key in _ORDER:
            m = re.match(rf"^{_FIELDS[key]}\s*:\s*(.*)$", line, re.I)
            if m:
                # PDF text extraction often puts the value on the next line when label and value use different fonts
                value = m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                found.setdefault(key, value)
                break
    need = ("bol_no", "shipment_id", "value", "cases", "consignee")
    if any(k not in found for k in need) or not re.match(r"^BOL-", found["bol_no"]):
        raise RowError("unreadable_pdf", f"{path.name} has no readable text for the key fields (garbled or a plain scan)",
                       "Send it to a person to type in")
    try:
        value = parse_amount(found["value"])
        cases = int(found["cases"])
    except (ParseError, ValueError):
        raise RowError("unreadable_pdf", f"{path.name}: declared value or cases cannot be read",
                       "Send it to a person to type in") from None
    return Bol(found["bol_no"], found["shipment_id"].upper(), found.get("carrier", ""), found.get("vehicle", ""),
               found.get("driver", ""), found.get("phone", ""), found["consignee"], cases, value, found.get("date", ""))


def apply_bol(conn: Connection, b: Bol, at: datetime) -> str:
    """Validate against the ERP shipment and store driver and vehicle. Raises RowError; returns 'applied' or 'unchanged'."""
    s = conn.execute("SELECT id, cases, value_inr, bol_no, vehicle_no, driver_name, driver_phone FROM shipment WHERE id = %s FOR UPDATE",
                     (b.shipment_id,)).fetchone()
    if s is None:
        cands = conn.execute(
            "SELECT s.id FROM shipment s JOIN customer c ON c.id = s.customer_id WHERE c.name = %s AND s.cases = %s ORDER BY s.id",
            (b.consignee, b.cases)).fetchall()
        if len(cands) == 1:
            sid = cands[0]["id"]
            raise RowError("unknown_shipment", f"Shipment {b.shipment_id} is not in the ERP",
                           f"Same consignee and {b.cases} cases match {sid}: use it", {"shipment_id": sid})
        raise RowError("unknown_shipment", f"Shipment {b.shipment_id} is not in the ERP",
                       "Check the shipment number on the document with the dispatcher")
    erp = Decimal(s["value_inr"])
    diff = abs(b.value - erp) / erp if erp else Decimal(1)
    if diff > VALUE_TOLERANCE:
        raise RowError("value_mismatch", f"Declared value differs from the ERP by {diff:.1%} (limit {VALUE_TOLERANCE:.0%})",
                       "Ask the shipper to confirm the invoice value; amend the ERP or request a corrected BOL")
    if b.cases != s["cases"]:
        raise RowError("cases_mismatch", f"BOL says {b.cases} cases, the ERP has {s['cases']}",
                       "Count the load at the dock and correct whichever is wrong")
    same = (s["bol_no"], s["vehicle_no"], s["driver_name"], s["driver_phone"]) == (b.bol_no, b.vehicle, b.driver, b.phone)
    if same:
        return "unchanged"
    conn.execute("UPDATE shipment SET bol_no = %s, vehicle_no = %s, driver_name = %s, driver_phone = %s, bol_received_at = %s "
                 "WHERE id = %s", (b.bol_no, b.vehicle, b.driver, b.phone, at, b.shipment_id))
    return "applied"


def payload_to_bol(p: dict) -> Bol:
    try:
        return Bol(str(p["bol_no"]), str(p["shipment_id"]).upper(), str(p.get("carrier", "")), str(p.get("vehicle", "")),
                   str(p.get("driver", "")), str(p.get("phone", "")), str(p["consignee"]), int(p["cases"]),
                   parse_amount(str(p["value"])), str(p.get("date", "")))
    except (KeyError, ValueError, ParseError):
        raise RowError("cannot_replay", "The payload lacks the BOL fields", "Supply bol_no, shipment_id, consignee, cases and value") from None


def process_file(pool, path: Path, at: datetime) -> dict:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    stats = {"file": path.name, "outcome": None, "reason": None}
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM ingest_log WHERE feed = %s AND idem_key = %s", (FEED, sha)).fetchone():
            bump(conn, FEED, at, counters={"files_duplicate": 1})
            stats["outcome"] = "duplicate"
            return stats
        bol = None
        try:
            bol = parse_bol(path)
            with conn.transaction():
                stats["outcome"] = apply_bol(conn, bol, at)
            bump(conn, FEED, at, received=1, accepted=1, clear_error=True, counters={stats["outcome"]: 1})
        except RowError as e:
            payload = {"_file": path.name, "file": path.name, **(bol.as_payload() if bol else {})}
            add_dead_letter(conn, FEED, at, payload, e)
            bump(conn, FEED, at, received=1, rejected=1, error=f"{path.name}: {e.code}", counters={"dead_letters": 1, e.code: 1})
            stats["outcome"], stats["reason"] = "dead_letter", e.code
        conn.execute("INSERT INTO ingest_log (feed, idem_key, meta) VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                     (FEED, sha, json.dumps({"file": path.name, "outcome": stats["outcome"]})))
    return stats


class BolWorker(threading.Thread):
    """Watches the BOL inbox; good files move to processed/bol, dead-lettered ones to rejected/bol."""

    def __init__(self, pool, settings: Settings, clock: Clock, stop: threading.Event):
        super().__init__(name="bol-worker", daemon=True)
        self.pool, self.settings, self.clock, self.stop_event = pool, settings, clock, stop

    def run(self) -> None:
        self.settings.bol_inbox.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception as e:
                log(logger, logging.ERROR, "bol scan failed", error=type(e).__name__)
            self.stop_event.wait(self.settings.erp_poll_s)

    def scan_once(self) -> dict:
        counts = {"applied": 0, "unchanged": 0, "duplicate": 0, "dead_letter": 0}
        for path in sorted(self.settings.bol_inbox.glob("*.pdf")):
            st = process_file(self.pool, path, self.clock.now())
            counts[st["outcome"]] += 1
            move_unique(path, self.settings.bol_rejected if st["outcome"] == "dead_letter" else self.settings.bol_processed)
        if sum(counts.values()):
            with self.pool.connection() as conn:
                audit(conn, "system", "bol.files_processed", "bol", counts, at=self.clock.now())
            log(logger, logging.INFO, "bol files processed", **counts)
        return counts
