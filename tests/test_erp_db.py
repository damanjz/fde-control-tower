"""ERP pipeline against the real schema: idempotency, versions, dead letters."""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.helpers import row, snapshot, write_export
from tower.clock import IST
from tower.erp import ErpWorker, process_file

pytestmark = pytest.mark.db

ROWS = [
    row("SH-1001"),
    row("SH-1002", amount="₹4.2 L"),
    row("SH-1003", city="HYD-2"),                       # unknown city code
    row("SH-1004", promised=""),                        # blank promised date
    row("SH-1005", amount="TBD"),                       # unreadable amount
    "garbage ;;; ###",                                  # malformed line
    row("SH-1006", carrier="Nobody Freight"),           # unknown carrier
    row("SH-1007", items="XX-999:5"),                   # unknown SKU
]


def run(pool, settings, clock, path, **kw):
    return process_file(pool, settings, clock, path, **kw)


def test_good_rows_land_and_bad_rows_go_to_dead_letters(pool, settings, clock, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", ROWS, bom=True)
    stats = run(pool, settings, clock, p)
    assert stats["new"] == 2 and stats["dead_letter"] == 6
    codes = {r["reason_code"] for r in conn.execute("SELECT reason_code FROM dead_letter").fetchall()}
    assert codes == {"unknown_zone", "missing_promised_at", "bad_amount", "malformed_row", "unknown_carrier", "unknown_sku"}
    s = conn.execute("SELECT * FROM shipment WHERE id = 'SH-1002'").fetchone()
    assert float(s["value_inr"]) == 420000.0 and s["version"] == 1 and s["cases"] == 30
    assert conn.execute("SELECT count(*) AS n FROM shipment_line WHERE shipment_id = 'SH-1001'").fetchone()["n"] == 2
    dl = conn.execute("SELECT * FROM dead_letter WHERE reason_code = 'unknown_zone'").fetchone()
    assert dl["suggested_patch"] == {"city": "Z01"} and "Hyderabad" in dl["suggested_fix"]
    assert conn.execute("SELECT suggested_patch FROM dead_letter WHERE reason_code = 'missing_promised_at'").fetchone()["suggested_patch"]["promised_at"]
    health = conn.execute("SELECT * FROM feed_health WHERE feed = 'erp'").fetchone()
    assert health["received"] == 8 and health["accepted"] == 2 and health["rejected"] == 6


def test_same_file_twice_leaves_identical_state(pool, settings, clock, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", ROWS)
    run(pool, settings, clock, p)
    first = snapshot(conn)
    again = run(pool, settings, clock, p)
    assert again["file_duplicate"] is True
    assert snapshot(conn) == first


def test_row_level_idempotency_when_file_memory_is_gone(pool, settings, clock, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", ROWS)
    run(pool, settings, clock, p)
    first = snapshot(conn)
    again = run(pool, settings, clock, p, skip_seen_check=True)
    assert again["new"] == 0 and again["updated"] == 0 and again["duplicate"] == 2
    assert again["dead_letter"] == 0 and again["dead_letter_repeat"] == 6
    assert snapshot(conn) == first


def test_newer_export_updates_with_version_plus_one(pool, settings, clock, conn, tmp_path):
    d = tmp_path / "in"
    run(pool, settings, clock, write_export(d, "erp_export_20261113_0500.csv", [row("SH-1001")]))
    run(pool, settings, clock, write_export(d, "erp_export_20261113_0600.csv",
                                            [row("SH-1001", eta="2026-11-14T06:00:00+05:30", promised="14/11/2026 18:00")]))
    s = conn.execute("SELECT * FROM shipment WHERE id = 'SH-1001'").fetchone()
    assert s["version"] == 2 and s["eta"] == datetime(2026, 11, 14, 6, 0, tzinfo=IST) and s["source_exported_at"] == datetime(2026, 11, 13, 6, 0, tzinfo=IST)
    assert conn.execute("SELECT count(*) AS n FROM shipment_line").fetchone()["n"] == 2   # lines replaced, not doubled


def test_older_export_never_overwrites_newer(pool, settings, clock, conn, tmp_path):
    d = tmp_path / "in"
    run(pool, settings, clock, write_export(d, "erp_export_20261113_0700.csv", [row("SH-1001", eta="2026-11-14T06:00:00+05:30")]))
    stats = run(pool, settings, clock, write_export(d, "erp_export_20261113_0500.csv", [row("SH-1001")]))
    assert stats["stale"] == 1
    assert conn.execute("SELECT version FROM shipment").fetchone()["version"] == 1


def test_conflicting_duplicate_in_one_file_goes_to_dead_letters(pool, settings, clock, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv",
                     [row("SH-1001"), row("SH-1001"), row("SH-1001", amount="4,29,000")])
    stats = run(pool, settings, clock, p)
    assert stats["new"] == 1 and stats["duplicate"] == 1 and stats["dead_letter"] == 1
    assert conn.execute("SELECT reason_code FROM dead_letter").fetchone()["reason_code"] == "duplicate_conflict"


def test_unmappable_header_rejects_the_file(pool, settings, clock, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", ["a,b,c"], header="foo,bar,baz")
    stats = run(pool, settings, clock, p)
    assert stats.get("rejected_file") is True
    assert conn.execute("SELECT reason_code FROM dead_letter").fetchone()["reason_code"] == "unmapped_headers"


def test_worker_moves_files_to_processed(pool, settings, clock, conn):
    write_export(settings.inbox, "erp_export_20261113_0500.csv", [row("SH-1001")])
    ErpWorker(pool, settings, clock, None).scan_once()
    assert not list(settings.inbox.glob("*.csv"))
    assert (settings.processed / "erp_export_20261113_0500.csv").exists()
    assert conn.execute("SELECT count(*) AS n FROM shipment").fetchone()["n"] == 1
