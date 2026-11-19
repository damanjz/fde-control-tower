"""Simulated unstructured inputs: one MSA contract PDF per chain, and bills of lading dropped in the BOL inbox.

PDFs are drawn by sim/make_pdfs.py, run with the reportlab interpreter (the tower never imports reportlab).
Everything is deterministic for a given seed.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from sim.erp_writer import indian_group
from tower.clock import IST
from tower.config import ROOT, Settings, load_settings
from tower.network import CHAINS, KIRANA


def doc_python() -> str:
    """The interpreter with reportlab (the tower itself never imports it): CT_PDF_PYTHON in the environment or .env."""
    return os.environ.get("CT_PDF_PYTHON") or load_settings().pdf_python or sys.executable


def has_reportlab() -> bool:
    """Whether doc_python() can draw PDFs (tests skip the PDF cases when it cannot)."""
    try:
        return subprocess.run([doc_python(), "-c", "import reportlab"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
GENERATOR = Path(__file__).resolve().parent / "make_pdfs.py"

# One chain's MSA deliberately disagrees with the seeded clause params, so the verification path has something to catch.
DRIFT = {"Spar": {"fillpen": "2.5"}}

FIRST = ["Ravi", "Suresh", "Imran", "Mahesh", "Gurpreet", "Anil", "Santosh", "Prakash", "Rajesh", "Venkat", "Manoj",
         "Dinesh", "Arjun", "Sanjay", "Naveen", "Kiran", "Satish", "Bablu", "Ramesh", "Yusuf"]
LAST = ["Kumar", "Reddy", "Khan", "Patil", "Singh", "Yadav", "Das", "Naik", "Sharma", "Rao", "Shaikh", "Ghosh"]
STATE_CODE = {"DC-HYD": "TS09", "DC-MUM": "MH04", "DC-DEL": "DL1C", "DC-KOL": "WB02"}


def slug(chain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", chain.lower()).strip("-")


def contract_specs() -> list[dict]:
    chains = [KIRANA, *CHAINS]
    return [{"chain": ch, "template": "A" if i % 2 == 0 else "B", "ref": f"TD-MSA-{slug(ch).upper()}",
             "file": f"MSA_{slug(ch)}.pdf", "drift": DRIFT.get(ch)} for i, ch in enumerate(chains)]


def run_generator(spec: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        res = subprocess.run([doc_python(), str(GENERATOR), str(spec_path)], capture_output=True, text=True, cwd=ROOT)
    if res.returncode:
        raise RuntimeError(f"PDF generator failed: {res.stderr.strip()[-400:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def write_contracts(settings: Settings) -> list[dict]:
    specs = contract_specs()
    run_generator({"out_dir": str(settings.contracts_dir), "contracts": specs})
    return specs


def write_bols(settings: Settings, seed: int | None = None, count: int = 60) -> list[dict]:
    """Drop `count` bills of lading for shipments already in the database, 5 of them deliberately bad.
    Returns the manifest [{file, shipment_id, mode, note}]."""
    seed = settings.scenario_seed if seed is None else seed
    rng = random.Random(f"bol-{seed}")
    with psycopg.connect(settings.db_url, row_factory=dict_row) as conn:
        ships = conn.execute(
            """SELECT s.id, s.dc_id, s.cases, s.value_inr, s.departed_at, c.name AS consignee, ca.name AS carrier
               FROM shipment s JOIN customer c ON c.id = s.customer_id JOIN carrier ca ON ca.id = s.carrier_id
               ORDER BY s.id""").fetchall()
    chosen = rng.sample(ships, min(count, len(ships)))
    bad_kinds = ["garbled", "blank", "mismatch_high", "mismatch_low", "wrong_id"]
    manifest, bols = [], []
    for i, s in enumerate(chosen):
        value = float(s["value_inr"])
        mode, note, shipment_id = "ok", "clean", s["id"]
        kind = bad_kinds[i] if i < len(bad_kinds) else ("minor_drift" if i == len(bad_kinds) else "clean")
        if kind == "garbled":
            mode, note = "garbled", "garbled text"
        elif kind == "blank":
            mode, note = "blank", "scan with no text layer"
        elif kind == "mismatch_high":
            value, note = value * 1.09, "declared value 9% above the ERP"
        elif kind == "mismatch_low":
            value, note = value * 0.94, "declared value 6% below the ERP"
        elif kind == "wrong_id":
            shipment_id, note = f"SH-{9100 + i}", "shipment id not in the ERP"
        elif kind == "minor_drift":
            value, note = value * 1.012, "declared value 1.2% above the ERP (inside the 2% tolerance)"
        num = shipment_id.removeprefix("SH-")
        bol_no = f"BOL-{num}-{rng.randrange(100, 999)}"
        file = f"BOL_{bol_no}.pdf"
        vehicle = f"{STATE_CODE[s['dc_id']]}{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.randrange(1000, 9999)}"
        bols.append({
            "file": file, "mode": mode, "bol_no": bol_no, "shipment_id": shipment_id, "carrier": s["carrier"],
            "vehicle": vehicle, "driver": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "phone": f"+91 9{rng.randrange(10000, 99999)} {rng.randrange(10000, 99999)}", "consignee": s["consignee"],
            "cases": s["cases"], "value": f"Rs {indian_group(round(value))}",
            "date": s["departed_at"].astimezone(IST).strftime("%d-%b-%Y"),
        })
        manifest.append({"file": file, "shipment_id": shipment_id, "real_shipment": s["id"], "mode": kind, "note": note})
    inbox = settings.bol_inbox
    inbox.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=inbox.parent) as tmp:
        run_generator({"out_dir": tmp, "bols": bols})
        for b in bols:   # move in only when complete, so the worker never reads a half-written file
            shutil.move(str(Path(tmp) / b["file"]), str(inbox / b["file"]))
    return manifest
