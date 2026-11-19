"""PDF inputs: contract clause parsing and parameter verification, bill of lading parsing and validation."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from sim.pdf_sim import contract_specs, has_reportlab, run_generator, write_bols
from tests.world import NOW, ship
from tower import bol, contracts
from tower.bol import BolWorker
from tower.clock import FixedClock
from tower.contracts import MONEY_CLAUSES, ContractError, parse_params, parse_pdf
from tower.scoped import ScopedStore
from tower.store import RowError

needs_pdf = pytest.mark.skipif(not has_reportlab(), reason="no interpreter with reportlab (set CT_PDF_PYTHON)")


@pytest.fixture(scope="module")
def pdf_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("msa")
    run_generator({"out_dir": str(d), "contracts": contract_specs()})
    return d


# ------------------------------------------------------------------ contracts
@needs_pdf
@pytest.mark.parametrize("name,template,pages", [("MSA_kirana-network.pdf", "A", 2), ("MSA_ratnadeep.pdf", "B", 4)])
def test_contract_clauses_parse_in_both_layouts(pdf_dir, name, template, pages):
    m = parse_pdf(Path(pdf_dir) / name)
    assert m.template == template and m.pages == pages and len(m.chunks) == 20
    ids = [c.clause_id for c in m.chunks]
    assert {"MSA-7.2", "MSA-9.1", "MSA-14.3", "MSA-6.1", "MSA-10.1", "MSA-11.1"} <= set(ids)
    by = {c.clause_id: c for c in m.chunks}
    assert by["MSA-7.2"].params == {"rate_per_day": 0.015, "cap": 0.1, "grace_hours": 12}
    assert by["MSA-9.1"].params == {"min_fill_rate": 0.95, "penalty": 0.02}
    assert by["MSA-14.3"].params == {"notify_within_hours": 6}
    assert 1 <= min(c.page for c in m.chunks) and max(c.page for c in m.chunks) == pages
    assert "Page " not in by["MSA-7.2"].text and "ARTICLE" not in by["MSA-6.1"].text   # headers and footers stripped


@needs_pdf
def test_spar_pdf_carries_the_deliberate_drift(pdf_dir):
    m = parse_pdf(Path(pdf_dir) / "MSA_spar.pdf")
    assert {c.clause_id: c.params for c in m.chunks if c.params}["MSA-9.1"]["penalty"] == 0.025


def test_param_parsing_rejects_unreadable_text():
    with pytest.raises(ContractError):
        parse_params("MSA-7.2", "Late delivery is penalised as agreed.")
    assert set(MONEY_CLAUSES) == {"MSA-7.2", "MSA-9.1", "MSA-14.3"}


@pytest.mark.db
def test_ingested_chunks_are_verified_and_the_mismatch_is_dead_lettered(msa_loaded, conn, pool, settings, pdf_dir):
    assert all(r["ok"] for r in msa_loaded) and len(msa_loaded) == 12
    again = contracts.ingest_all(pool, settings, directory=pdf_dir, embed=False)     # the pool fixture cleared the dead letters
    assert sum(r["mismatches"] for r in again) == 1
    assert conn.execute("SELECT count(*) AS n FROM contract_chunk").fetchone()["n"] == 240
    assert conn.execute("SELECT count(*) AS n FROM contract WHERE msa_id IS NULL").fetchone()["n"] == 0
    bad = conn.execute("SELECT d.chain, ch.clause_id FROM contract_chunk ch JOIN msa_document d ON d.id = ch.msa_id "
                       "WHERE ch.verified IS FALSE").fetchall()
    assert [(b["chain"], b["clause_id"]) for b in bad] == [("Spar", "MSA-9.1")]
    dl = conn.execute("SELECT * FROM dead_letter WHERE feed = 'contracts'").fetchall()
    assert len(dl) == 1 and dl[0]["reason_code"] == "param_mismatch" and "Spar" in dl[0]["reason"]


@pytest.mark.db
def test_clause_retrieval_returns_quoted_text_with_chain_and_page(msa_loaded, pool):
    from tower.access import USERS
    st = ScopedStore(pool, USERS["meera"], NOW, None)          # full-text only (no settings, no embeddings)
    hits = st.search_clauses("late delivery penalty per day", chain="Ratnadeep", k=3)
    assert hits[0]["clause_id"] == "MSA-7.2" and hits[0]["chain"] == "Ratnadeep" and hits[0]["page"] >= 1
    assert "1.5 percent" in hits[0]["text"]
    fm = st.search_clauses("force majeure weather notified within hours", chain="Spar", k=2)
    assert fm[0]["clause_id"] == "MSA-14.3"


# ------------------------------------------------------------------ bills of lading
@pytest.mark.db
@needs_pdf
def test_bol_parse_validate_and_dead_letter(pool, settings, test_db_url, conn):
    with pool.connection() as c:
        for i in range(70):
            ship(c, f"SH-{5000 + i}", city="Nagpur", value=1_000_000 + i * 1000, cases=100 + i)
    manifest = write_bols(settings, seed=7, count=60)
    assert len(list(settings.bol_inbox.glob("*.pdf"))) == 60
    worker = BolWorker(pool, settings, FixedClock(NOW), None)
    counts = worker.scan_once()
    assert counts["dead_letter"] == 5 and counts["applied"] == 55
    codes = sorted(r["reason_code"] for r in conn.execute("SELECT reason_code FROM dead_letter WHERE feed = 'bol'").fetchall())
    assert codes == ["unknown_shipment", "unreadable_pdf", "unreadable_pdf", "value_mismatch", "value_mismatch"]
    unreadable = conn.execute("SELECT suggested_fix FROM dead_letter WHERE reason_code = 'unreadable_pdf'").fetchall()
    assert all(u["suggested_fix"] == "Send it to a person to type in" for u in unreadable)
    wrong = conn.execute("SELECT * FROM dead_letter WHERE reason_code = 'unknown_shipment'").fetchone()
    real = next(m["real_shipment"] for m in manifest if m["mode"] == "wrong_id")
    assert wrong["suggested_patch"] == {"shipment_id": real}      # same consignee and cases: the likely intended shipment
    # driver and vehicle landed on the shipment, the 1.2% drift was accepted
    drift = next(m for m in manifest if m["mode"] == "minor_drift")
    s = conn.execute("SELECT * FROM shipment WHERE id = %s", (drift["shipment_id"],)).fetchone()
    assert s["bol_no"].startswith("BOL-") and s["driver_phone"].startswith("+91") and s["vehicle_no"]
    assert conn.execute("SELECT count(*) AS n FROM shipment WHERE bol_no IS NOT NULL").fetchone()["n"] == 55
    # replaying the wrong-id dead letter with its suggested fix applies the BOL
    from tower import replay
    res = replay.replay(pool, wrong["id"], None, "asha", NOW, timedelta(minutes=5))
    assert res["ok"] and conn.execute("SELECT bol_no FROM shipment WHERE id = %s", (real,)).fetchone()["bol_no"]
    # the same files again change nothing (the processed copies are keyed by hash)
    for f in list(settings.bol_processed.glob("*.pdf"))[:3]:
        shutil.copy(f, settings.bol_inbox / f.name)
    assert worker.scan_once()["duplicate"] == 3


def test_corrupt_pdf_is_unreadable_with_the_fix(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"not a pdf")
    with pytest.raises(RowError) as e:
        bol.parse_bol(p)
    assert e.value.code == "unreadable_pdf" and e.value.fix == "Send it to a person to type in"
