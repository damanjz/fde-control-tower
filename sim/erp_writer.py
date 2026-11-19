"""Legacy-ERP export simulator: hourly CSV files with inconsistent headers, mixed formats and injected mess.

Three dialects rotate by file number (header spellings, delimiter, item syntax, date and amount styles).
Mess injected per row, at fixed rates, from a seeded RNG: the same seed gives the same files.
"""

from __future__ import annotations

import copy
import csv
import io
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from sim.world import CARRIER_NAME, SimShipment, World
from tower.clock import IST

DIALECTS = [
    {  # 0: comma, ISO / DD/MM / Excel dates, plain and grouped amounts
        "delim": ",", "bom": True,
        "headers": ["Shipment_No", "Order_No", "DC", "City_Code", "Carrier", "Customer", "Cases", "Amount", "Status",
                    "Dispatch_Dt", "ETA", "Promise_Dt", "Items"],
        "items": lambda ls: "|".join(f"{s}:{c}" for s, c, _ in ls),
        "dates": ["dmy", "iso", "excel"], "amounts": ["plain", "indian", "rupee_indian"],
    },
    {  # 1: semicolon, lakh notation, long headers
        "delim": ";", "bom": False,
        "headers": ["shipment id", "SO number", "origin dc", "dest city", "transporter", "customer name", "total cases",
                    "invoice value (INR)", "ship status", "departed", "expected arrival", "promised by", "line items"],
        "items": lambda ls: ", ".join(f"{s} x {c}" for s, c, _ in ls),
        "dates": ["dmy", "dmon", "iso_naive"], "amounts": ["lakh", "rs_lakh", "rupee_indian", "plain"],
    },
    {  # 2: tab, upper-case headers, Excel serials
        "delim": "\t", "bom": True,
        "headers": ["SHIP_REF", "SO_REF", "DC_CODE", "DEST", "CARRIER_NM", "CUST", "QTY_CASES", "INV_AMT", "STS",
                    "DEP_DT", "ETA_DT", "PROMISE_DT", "SKU_LINES"],
        "items": lambda ls: "|".join(f"{s}*{c}" for s, c, _ in ls),
        "dates": ["excel", "dmy"], "amounts": ["plain", "indian", "crore_lakh"],
    },
]
STATUS_TEXT = {"in_transit": ["In Transit", "IN-TRANSIT", "Dispatched", "in transit"], "held": ["Held", "ON HOLD"]}
ZONE_FORMS = ["id", "city", "code"]
CARRIER_FORMS = ["name", "upper", "short", "id"]

# Per-row injection rates
RATES = {"blank_promised": 0.025, "unknown_city": 0.02, "bad_amount": 0.012, "conflict_dup": 0.02,
         "exact_dup": 0.015, "padded": 0.10, "lower_id": 0.03}


def fmt_date(dt: datetime, style: str, rng: random.Random) -> str:
    dt = dt.astimezone(IST)
    if style == "dmy":
        return dt.strftime("%d/%m/%Y %H:%M")
    if style == "iso":
        return dt.isoformat()
    if style == "iso_naive":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if style == "dmon":
        return dt.strftime("%d-%b-%Y %H:%M")
    serial = (dt.replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400
    return f"{serial:.8f}"


def indian_group(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def fmt_amount(v: int, style: str) -> str:
    lakh = f"{v / 1e5:.5f}".rstrip("0").rstrip(".")
    return {
        "plain": f"{v}.00",
        "indian": indian_group(v),
        "rupee_indian": f"₹{indian_group(v)}",
        "lakh": f"{lakh} L",
        "rs_lakh": f"Rs. {lakh} lakh",
        "crore_lakh": (f"{v / 1e7:.7f}".rstrip("0").rstrip(".") + " Cr") if v >= 1_000_000 else f"{lakh} L",
    }[style]


class ErpSim:
    def __init__(self, world: World, seed: int = 1):
        self.world = world
        self.seed = seed
        self.state: dict[str, SimShipment] = {}     # what the ERP currently believes
        self.file_no = 0
        self.last_export: datetime | None = None
        self.injected: Counter[str] = Counter()

    # ---- one export
    def write_export(self, at: datetime, out_dir: Path) -> tuple[Path, Counter]:
        rng = random.Random(f"{self.seed}-{self.file_no}")
        dialect = DIALECTS[self.file_no % len(DIALECTS)]
        stats: Counter[str] = Counter()
        rows: list[list[str]] = []

        new = [s for s in self.world.shipments
               if s.departed <= at and (self.last_export is None or s.departed > self.last_export) and s.id not in self.state]
        updates, same = [], []
        for s in list(self.state.values()):
            r = rng.random()
            if r < 0.12:
                s.eta += timedelta(hours=rng.randrange(1, 7))
                s.promised = max(s.promised, s.eta)
                updates.append(s)
            elif r < 0.15:
                s.lines = [(k, c, v) for k, c, v in s.lines]
                k, c, v = s.lines[0]
                s.lines[0] = (k, c, v + rng.randrange(500, 5000))   # invoice amended
                updates.append(s)
            elif r < 0.30:
                same.append(s)
        for s in new:
            self.state[s.id] = copy.deepcopy(s)
        batch = [self.state[s.id] for s in new] + updates + same
        stats["new_shipments"], stats["updated_shipments"], stats["unchanged_reexports"] = len(new), len(updates), len(same)

        for s in batch:
            row, faults = self._row(s, dialect, rng)
            rows.append(row)
            self._count(faults, stats)
            if rng.random() < RATES["exact_dup"]:
                rows.append(list(row))
                stats["exact_dup"] += 1
            if rng.random() < RATES["conflict_dup"]:
                alt = list(row)
                alt[7] = fmt_amount(int(s.value * 1.02) + 137, "plain")
                rows.insert(rng.randrange(len(rows) + 1), alt)
                if not faults - {"padded_rows"}:   # a copy of an already faulty row is dead-lettered for that fault instead
                    stats["conflict_dup"] += 1
                # The conflicting copy is a copy of the row: it keeps the city and promised-date faults but has a
                # valid amount. Its amount differs, so it is a different payload with its own dead letter.
                self._count(faults - {"bad_amount", "padded_rows"}, stats)

        text = self._render(dialect, rows, rng, stats)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"erp_export_{at.astimezone(IST):%Y%m%d_%H%M}.csv"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        tmp.replace(path)
        self.file_no += 1
        self.last_export = at
        stats["rows_written"] = len(rows)
        self.injected.update(stats)
        return path, stats

    # ---- one row
    def _row(self, s: SimShipment, d: dict, rng: random.Random) -> tuple[list[str], set[str]]:
        z = self.world.zone[s.zone]
        faults: set[str] = set()
        form = rng.choice(ZONE_FORMS)
        city = {"id": z["id"], "city": z["city"], "code": z["city"][:3].upper()}[form]
        if rng.random() < RATES["unknown_city"]:
            city = z["city"][:3].upper() + "-2"
            faults.add("unknown_city")
        carrier = CARRIER_NAME[s.carrier]
        carrier = {"name": carrier, "upper": carrier.upper(), "short": carrier.split()[0], "id": s.carrier}[rng.choice(CARRIER_FORMS)]
        dstyle = lambda: rng.choice(d["dates"])  # noqa: E731
        promised = fmt_date(s.promised, dstyle(), rng)
        if rng.random() < RATES["blank_promised"]:
            promised = ""
            faults.add("blank_promised")
        amount = fmt_amount(s.value, rng.choice(d["amounts"]))
        if rng.random() < RATES["bad_amount"]:
            amount = rng.choice(["TBD", "N/A", "see invoice"])
            faults.add("bad_amount")
        ship = s.id.lower() if rng.random() < RATES["lower_id"] else s.id
        row = [ship, s.order_no, s.dc if rng.random() < 0.7 else s.dc.removeprefix("DC-"), city, carrier, s.customer,
               str(s.cases), amount, rng.choice(STATUS_TEXT[s.status]), fmt_date(s.departed, dstyle(), rng),
               fmt_date(s.eta, dstyle(), rng), promised, d["items"](s.lines)]
        if rng.random() < RATES["padded"]:
            row = [f"  {c} " if rng.random() < 0.5 else c for c in row]
            faults.add("padded_rows")
        return row, faults

    @staticmethod
    def _count(faults: set[str], stats: Counter) -> None:
        """A row is dead-lettered for the first check it fails, so a row with two faults counts once, as the first
        in the pipeline's order (city, amount, promised date). Counting each fault would over-report."""
        if "padded_rows" in faults:
            stats["padded_rows"] += 1
        for kind in ("unknown_city", "bad_amount", "blank_promised"):
            if kind in faults:
                stats[kind] += 1
                break

    def _render(self, d: dict, rows: list[list[str]], rng: random.Random, stats: Counter) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=d["delim"], lineterminator="\n")
        w.writerow(d["headers"])
        for r in rows:
            w.writerow(r)
        lines = buf.getvalue().split("\n")[:-1]
        # malformed lines: truncated row, junk footer text, unbalanced quote
        for _ in range(rng.randrange(2, 5)):
            kind = rng.choice(["truncated", "junk", "quote"])
            pos = rng.randrange(1, len(lines) + 1)
            if kind == "truncated" and len(lines) > 2:
                src = lines[rng.randrange(1, len(lines))]
                lines.insert(pos, src[: len(src) // 2])
            elif kind == "junk":
                lines.insert(pos, rng.choice(["*** ERP EXPORT INTERRUPTED ***", "##,,,,;;;;,,", "SYSTEM MESSAGE: lock timeout on SO table"]))
            else:
                lines.insert(pos, f'"{rng.choice(["SH-9", "SH-48"])}{rng.randrange(10, 99)}{d["delim"]}"broken{d["delim"]}x')
            stats["malformed_lines"] += 1
        if rng.random() < 0.3:
            lines.insert(rng.randrange(1, len(lines) + 1), "")
        # mixed line endings
        text = "".join(ln + rng.choice(["\r\n", "\n"]) for ln in lines)
        return ("﻿" + text) if d["bom"] else text


def write_malformed_export(world: World, at: datetime, out_dir: Path, rows: int = 40, seed: int | None = None) -> tuple[Path, int]:
    """The outage simulation's broken ERP file: a mappable header, then `rows` lines cut off part-way (the export job died
    while writing) and one interruption banner. Every data line is malformed, so nothing in it can change a shipment;
    each becomes a `malformed_row` dead letter. A fresh seed picks other shipments and cut points, so a second outage is
    not collapsed into the first one's dead letters."""
    rng = random.Random(seed if seed is not None else random.SystemRandom().randrange(1 << 62))
    d = DIALECTS[0]
    pool = [s for s in world.shipments if s.departed <= at] or world.shipments
    picks = rng.sample(pool, k=min(rows, len(pool)))
    sim = ErpSim(world, seed=rng.randrange(1 << 30))
    buf = io.StringIO()
    csv.writer(buf, delimiter=d["delim"], lineterminator="\n").writerow(d["headers"])
    lines = [buf.getvalue().rstrip("\n")]
    for s in picks:
        row, _ = sim._row(s, d, rng)
        b = io.StringIO()   # cut between fields: always too few columns, never an open quote that swallows the next line
        csv.writer(b, delimiter=d["delim"], lineterminator="\n").writerow(row[:rng.randrange(3, len(row) - 2)])
        lines.append(b.getvalue().rstrip("\n"))
    lines.insert(rng.randrange(2, len(lines) + 1), "*** ERP EXPORT INTERRUPTED ***")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"erp_export_{at.astimezone(IST):%Y%m%d_%H%M}_outage.csv"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(("﻿" + "\n".join(lines) + "\n").encode("utf-8"))
    tmp.replace(path)
    return path, len(lines) - 1
