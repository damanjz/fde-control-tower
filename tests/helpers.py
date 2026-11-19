"""Builders for small ERP exports used by the DB tests."""

from __future__ import annotations

from pathlib import Path

HEADER = "Shipment_No,Order_No,DC,City_Code,Carrier,Customer,Cases,Amount,Status,Dispatch_Dt,ETA,Promise_Dt,Items"


def row(ship="SH-1001", city="Hyderabad", amount="4,20,000", promised="14/11/2026 06:00",
        eta="2026-11-14T00:00:00+05:30", items="PE-001:10|SN-004:20", carrier="Ganga Freight",
        status="In Transit", customer="Kirana network (Hyderabad)"):
    amount_cell = f'"{amount}"' if "," in amount else amount
    return (f"{ship},SO-9001,DC-HYD,{city},{carrier},{customer},30,{amount_cell},{status},"
            f"13/11/2026 06:00,{eta},{promised},{items}")


def write_export(directory: Path, name: str, rows: list[str], header: str = HEADER, bom: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = "\n".join([header, *rows]) + "\n"
    path.write_bytes((("﻿" if bom else "") + text).encode("utf-8"))
    return path


def snapshot(conn) -> dict:
    """Business-table state used to prove idempotency."""
    out = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
           for t in ("shipment", "shipment_line", "dead_letter")}
    r = conn.execute(
        "SELECT COALESCE(sum(version),0) AS v, "
        "md5(COALESCE(string_agg(id || content_hash, ',' ORDER BY id), '')) AS h FROM shipment"
    ).fetchone()
    out["versions"], out["state"] = int(r["v"]), r["h"]
    return out
