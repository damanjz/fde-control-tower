"""Contract PDFs: parse (pypdf), split into clause chunks, embed, store, and verify the money parameters.

`python -m tower.contracts [dir]` ingests every *.pdf in data/contracts/. Safe to rerun: a file with the same
hash and complete embeddings is not re-embedded, but the verification always runs again.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pypdf
from psycopg import Connection
from psycopg.types.json import Jsonb

from tower import llm
from tower.clock import Clock, RealClock
from tower.config import Settings, load_settings
from tower.logging_ import log
from tower.parsing import norm
from tower.store import RowError, add_dead_letter, audit

logger = logging.getLogger("tower.contracts")
FEED = "contracts"

MONEY_CLAUSES = {"MSA-7.2": "late_delivery", "MSA-9.1": "fill_rate", "MSA-14.3": "force_majeure"}
_START = [
    re.compile(r"^MSA-(\d{1,2}\.\d{1,2})\s+(.+)$"),                          # layout A: "MSA-7.2  Late delivery"
    re.compile(r"^Clause\s+(\d{1,2}\.\d{1,2})\s*[-–—]\s*(.+)$", re.I),  # layout B: "Clause 7.2 - LATE DELIVERY"
]
_NOISE = [re.compile(p) for p in (r"^Page \d+$", r"^TD-MSA-[A-Z0-9-]+$", r"^Tarangini Distribution - MSA - .+$", r"^ARTICLE \d+$")]
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", re.I)
_CHAIN = [re.compile(r"^Between Tarangini Distribution Pvt Ltd and (.+)$"), re.compile(r"^Customer:\s*(.+)$")]


@dataclass
class Chunk:
    clause_id: str
    heading: str
    page: int
    text: str
    kind: str | None = None
    params: dict = field(default_factory=dict)


@dataclass
class ParsedMsa:
    chain: str
    template: str
    pages: int
    chunks: list[Chunk]


class ContractError(Exception):
    def __init__(self, code: str, reason: str, fix: str):
        super().__init__(reason)
        self.code, self.reason, self.fix = code, reason, fix


def parse_params(clause_id: str, text: str) -> dict:
    """Numbers for a money clause, read from the clause text. Raises ContractError when they cannot be read."""
    kind = MONEY_CLAUSES[clause_id]
    pcts = [float(x) for x in _PCT.findall(text)]
    if kind == "late_delivery":
        if len(pcts) < 2:
            raise ContractError("unreadable_clause", f"{clause_id}: cannot read the daily rate and the cap",
                                "Check the PDF text of the clause by hand")
        out = {"rate_per_day": round(pcts[0] / 100, 6), "cap": round(pcts[-1] / 100, 6)}
        m = re.search(r"(\d+)\s*hours", text)
        if m:
            out["grace_hours"] = int(m.group(1))
        return out
    if kind == "fill_rate":
        if len(pcts) < 2:
            raise ContractError("unreadable_clause", f"{clause_id}: cannot read the fill-rate floor and the penalty",
                                "Check the PDF text of the clause by hand")
        return {"min_fill_rate": round(pcts[0] / 100, 6), "penalty": round(pcts[-1] / 100, 6)}
    m = re.search(r"within\s+(\d+)\s*hours", text)
    if not m:
        raise ContractError("unreadable_clause", f"{clause_id}: cannot read the notification window",
                            "Check the PDF text of the clause by hand")
    return {"notify_within_hours": int(m.group(1))}


def parse_pdf(path: Path) -> ParsedMsa:
    reader = pypdf.PdfReader(str(path))
    chunks: list[Chunk] = []
    chain, template = None, "?"
    cur: Chunk | None = None
    for pno, page in enumerate(reader.pages, start=1):
        for raw in (page.extract_text() or "").splitlines():
            line = raw.strip()
            if not line or any(n.match(line) for n in _NOISE):
                continue
            for pat in _CHAIN:
                m = pat.match(line)
                if m and chain is None:
                    chain = m.group(1).strip()
            start = next((m for m in (p.match(line) for p in _START) if m), None)
            if start:
                template = "A" if line.startswith("MSA-") else "B"
                heading = start.group(2).strip()
                if template == "B":
                    heading = heading.capitalize()
                cur = Chunk(f"MSA-{start.group(1)}", heading, pno, "")
                chunks.append(cur)
            elif cur is not None:
                cur.text = f"{cur.text} {line}".strip()
    if not chunks:
        raise ContractError("no_clauses", f"{path.name}: no numbered clauses found", "Send the PDF to a person to check")
    chain = chain or path.stem.removeprefix("MSA_").replace("-", " ").title()
    for ch in chunks:
        if ch.clause_id in MONEY_CLAUSES:
            ch.kind = MONEY_CLAUSES[ch.clause_id]
            ch.params = parse_params(ch.clause_id, ch.text)
    missing = [c for c in MONEY_CLAUSES if c not in {x.clause_id for x in chunks}]
    if missing:
        raise ContractError("missing_clause", f"{path.name}: clauses not found: {', '.join(missing)}",
                            "Ask the customer for a complete copy of the agreement")
    return ParsedMsa(chain, template, len(reader.pages), chunks)


def _same(parsed: dict, seeded: dict) -> bool:
    return all(k in parsed and abs(float(parsed[k]) - float(v)) < 1e-9 for k, v in seeded.items())


def verify_msa(conn: Connection, msa_id: int, chain: str, at: datetime) -> list[dict]:
    """Compare each money clause's parsed params with the seeded contract_clause params of the chain's contracts.
    Mismatches are flagged on the chunk and dead-lettered (feed 'contracts'). Returns the mismatches."""
    bad: list[dict] = []
    for ch in conn.execute("SELECT id, clause_id, params FROM contract_chunk WHERE msa_id = %s AND kind IS NOT NULL",
                           (msa_id,)).fetchall():
        seeds = conn.execute(
            "SELECT DISTINCT cl.params FROM contract_clause cl JOIN contract k ON k.id = cl.contract_id "
            "JOIN customer c ON c.id = k.customer_id WHERE c.chain = %s AND cl.clause_id = %s", (chain, ch["clause_id"])).fetchall()
        ok = bool(seeds) and all(_same(ch["params"], s["params"]) for s in seeds)
        conn.execute("UPDATE contract_chunk SET verified = %s WHERE id = %s", (ok, ch["id"]))
        if not ok:
            seeded = seeds[0]["params"] if seeds else {}
            mm = {"chain": chain, "clause_id": ch["clause_id"], "pdf_params": ch["params"], "seeded_params": seeded}
            bad.append(mm)
            log(logger, logging.WARNING, "contract param mismatch", chain=chain, clause=ch["clause_id"])
            add_dead_letter(conn, FEED, at, mm, RowError(
                "param_mismatch",
                f"{chain} {ch['clause_id']}: the PDF says {ch['params']}, the contract master says {seeded}",
                "Check the signed copy. If the PDF is right, update the contract master; if the master is right, ask the "
                "customer for a corrected PDF. Until then the PDF values are used and the exposure is flagged unverified."))
    return bad


def ingest_pdf(conn: Connection, settings: Settings, path: Path, at: datetime, *, embed: bool = True) -> dict:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        parsed = parse_pdf(path)
    except ContractError as e:
        add_dead_letter(conn, FEED, at, {"_file": path.name, "file": path.name}, RowError(e.code, e.reason, e.fix))
        return {"file": path.name, "ok": False, "reason": e.code}
    cust = conn.execute("SELECT 1 FROM customer WHERE chain = %s LIMIT 1", (parsed.chain,)).fetchone()
    if not cust:
        by_norm = {norm(r["chain"]): r["chain"] for r in conn.execute("SELECT DISTINCT chain FROM customer").fetchall()}
        if norm(parsed.chain) not in by_norm:
            add_dead_letter(conn, FEED, at, {"file": path.name, "chain": parsed.chain}, RowError(
                "unknown_chain", f"{path.name}: no customers for chain {parsed.chain!r}", "Check the chain name on the cover page"))
            return {"file": path.name, "ok": False, "reason": "unknown_chain"}
        parsed.chain = by_norm[norm(parsed.chain)]
    existing = conn.execute("SELECT id, sha256 FROM msa_document WHERE chain = %s", (parsed.chain,)).fetchone()
    reuse = bool(existing and existing["sha256"] == sha and not conn.execute(
        "SELECT 1 FROM contract_chunk WHERE msa_id = %s AND embedding IS NULL LIMIT 1", (existing["id"],)).fetchone()
        and conn.execute("SELECT count(*) AS n FROM contract_chunk WHERE msa_id = %s", (existing["id"],)).fetchone()["n"] == len(parsed.chunks))
    embedded = 0
    if reuse:
        msa_id = existing["id"]
    else:
        vectors: list[list[float]] | None = None
        if embed:
            try:
                vectors = llm.embed(settings, [f"{parsed.chain} {c.clause_id} {c.heading}. {c.text}" for c in parsed.chunks])
            except llm.LLMError as e:
                log(logger, logging.WARNING, "embedding unavailable, storing chunks without vectors", error=str(e))
        msa_id = conn.execute(
            """INSERT INTO msa_document (chain, file, sha256, template, pages) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (chain) DO UPDATE SET file = EXCLUDED.file, sha256 = EXCLUDED.sha256,
                   template = EXCLUDED.template, pages = EXCLUDED.pages, ingested_at = now() RETURNING id""",
            (parsed.chain, path.name, sha, parsed.template, parsed.pages)).fetchone()["id"]
        conn.execute("DELETE FROM contract_chunk WHERE msa_id = %s", (msa_id,))
        for i, c in enumerate(parsed.chunks):
            vec = llm.vector_literal(vectors[i]) if vectors else None
            embedded += bool(vec)
            conn.execute(
                "INSERT INTO contract_chunk (msa_id, clause_id, heading, page, text, kind, params, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                (msa_id, c.clause_id, c.heading, c.page, c.text, c.kind, Jsonb(c.params), vec))
    linked = conn.execute("UPDATE contract SET msa_id = %s WHERE customer_id IN (SELECT id FROM customer WHERE chain = %s)",
                          (msa_id, parsed.chain)).rowcount
    mismatches = verify_msa(conn, msa_id, parsed.chain, at)
    return {"file": path.name, "ok": True, "chain": parsed.chain, "template": parsed.template, "pages": parsed.pages,
            "chunks": len(parsed.chunks), "embedded": embedded, "reused": reuse, "contracts_linked": linked,
            "mismatches": len(mismatches)}


def ingest_all(pool, settings: Settings, clock: Clock | None = None, directory: Path | None = None, *,
               embed: bool = True) -> list[dict]:
    clock = clock or RealClock()
    directory = directory or settings.contracts_dir
    out = []
    for path in sorted(directory.glob("*.pdf")):
        with pool.connection() as conn, conn.transaction():
            out.append(ingest_pdf(conn, settings, path, clock.now(), embed=embed))
    with pool.connection() as conn:
        audit(conn, "system", "contracts.ingested", str(directory.name),
              {"files": len(out), "ok": sum(1 for o in out if o["ok"]), "mismatches": sum(o.get("mismatches", 0) for o in out)},
              at=clock.now())
    return out


def main() -> None:
    import sys

    from tower.db import open_pool
    from tower.logging_ import configure_logging

    configure_logging()
    settings = load_settings()
    pool = open_pool(settings.db_url, size=4)
    try:
        for r in ingest_all(pool, settings, directory=Path(sys.argv[1]) if len(sys.argv) > 1 else None):
            print(r)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
