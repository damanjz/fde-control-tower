"""The scoped data-access layer. Every read path (API endpoints and agent tools) goes through ScopedStore.

It applies the row filter (which DCs the user may see) in SQL and the field masking before a row leaves this
module. Nothing outside this file (and the write-side engine in tower.actions / tower.risk, which only receives
ids this layer already approved) runs a query on behalf of a user.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from tower import llm
from tower.access import User, mask_carrier, mask_shipment
from tower.config import Settings

SHIPMENT_COLS = """s.id, s.order_no, s.dc_id, s.zone_id, z.city AS zone_city, z.state AS zone_state, s.carrier_id,
    s.customer_id, cu.name AS customer, cu.chain, s.cases, s.value_inr, s.status, s.departed_at, s.eta, s.promised_at,
    s.last_lat, s.last_lon, s.last_position_at, s.carrier_status, s.carrier_status_at, s.source, s.version,
    s.bol_no, s.vehicle_no, s.driver_name, s.driver_phone, s.bol_received_at"""
SHIPMENT_FROM = "shipment s JOIN zone z ON z.id = s.zone_id JOIN customer cu ON cu.id = s.customer_id"


def rrf(rankings: list[list[dict]], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion of ranked chunk lists: score = sum of 1 / (k + rank)."""
    score: dict[int, float] = {}
    by_id: dict[int, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            score[row["id"]] = score.get(row["id"], 0.0) + 1.0 / (k + rank)
            by_id[row["id"]] = row
    return [{**by_id[i], "score": round(score[i], 5)} for i in sorted(score, key=lambda i: (-score[i], i))]


class ScopedStore:
    def __init__(self, pool, user: User, now: datetime, settings: Settings | None = None):
        self.pool, self.user, self.now, self.settings = pool, user, now, settings

    # ------------------------------------------------------------ plumbing
    @property
    def _p(self) -> dict[str, Any]:
        return {"dcs": list(self.user.dcs or []), "uid": self.user.id, "now": self.now}

    def _ship(self, alias: str = "s") -> str:
        """SQL predicate: the shipment rows this user may see."""
        return f"{alias}.dc_id = ANY(%(dcs)s)" if self.user.scoped else "TRUE"

    def _q(self, sql: str, params: dict | None = None) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute(sql, {**self._p, **(params or {})}).fetchall()

    def _one(self, sql: str, params: dict | None = None) -> dict | None:
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _chain_scope(self) -> str:
        if self.user.contracts == "all":
            return "TRUE"
        if self.user.contracts == "scoped":
            # a dispatcher reads the contracts of the chains he actually ships to
            return ("d.chain IN (SELECT cu.chain FROM customer cu JOIN shipment s ON s.customer_id = cu.id "
                    "WHERE s.dc_id = ANY(%(dcs)s))")
        return "FALSE"

    # ------------------------------------------------------------ shipments
    def shipments(self, *, dc=None, zone=None, carrier=None, status=None, customer_id=None, min_version=None,
                  limit: int = 100, offset: int = 0) -> dict:
        where, params = [self._ship()], {"limit": limit, "offset": offset}
        for col, key, val in (("s.dc_id", "dc", dc), ("s.zone_id", "zone", zone), ("s.carrier_id", "carrier", carrier),
                              ("s.status", "status", status), ("s.customer_id", "cust", customer_id)):
            if val is not None:
                where.append(f"{col} = %({key})s")
                params[key] = val
        if min_version is not None:
            where.append("s.version >= %(minv)s")
            params["minv"] = min_version
        clause = " AND ".join(where)
        tot = self._one(f"SELECT count(*) AS n, COALESCE(sum(s.value_inr), 0) AS v FROM {SHIPMENT_FROM} WHERE {clause}", params)  # noqa: S608
        rows = self._q(f"SELECT {SHIPMENT_COLS} FROM {SHIPMENT_FROM} WHERE {clause} ORDER BY s.id LIMIT %(limit)s OFFSET %(offset)s", params)  # noqa: S608
        return {"total": tot["n"], "total_value_inr": tot["v"], "items": [mask_shipment(self.user, r) for r in rows]}

    def shipment(self, shipment_id: str) -> dict | None:
        row = self._one(f"SELECT {SHIPMENT_COLS} FROM {SHIPMENT_FROM} WHERE s.id = %(id)s AND {self._ship()}", {"id": shipment_id})  # noqa: S608
        if row is None:
            return None
        row["lines"] = self._q("SELECT sku, cases, value_inr FROM shipment_line WHERE shipment_id = %(id)s ORDER BY sku", {"id": shipment_id})
        return mask_shipment(self.user, row)

    def visible_shipment_ids(self, ids: list[str]) -> set[str]:
        rows = self._q(f"SELECT s.id FROM shipment s WHERE s.id = ANY(%(ids)s) AND {self._ship()}", {"ids": list(ids)})  # noqa: S608
        return {r["id"] for r in rows}

    def shipment_lines(self, shipment_ids: list[str]) -> list[dict]:
        return self._q(
            f"SELECT l.shipment_id, l.sku, l.cases, l.value_inr, p.supplier_id FROM shipment_line l "
            f"JOIN product p ON p.sku = l.sku JOIN shipment s ON s.id = l.shipment_id "
            f"WHERE l.shipment_id = ANY(%(ids)s) AND {self._ship()} ORDER BY l.shipment_id, l.sku",  # noqa: S608
            {"ids": list(shipment_ids)})

    # ------------------------------------------------------------ incidents
    _INCIDENT_SQL = """
        WITH scoped AS (
            SELECT i.*, s.value_inr AS ship_value FROM incident_impact i JOIN shipment s ON s.id = i.shipment_id
            WHERE {ship}
        )
        SELECT e.id, e.source, e.external_id, e.kind, e.title, e.detail, e.lat, e.lon, e.radius_km, e.dc_id, e.carrier_id,
               e.supplier_id, e.states, e.severity, e.started_at, e.status,
               count(sc.shipment_id)::int AS shipments,
               COALESCE(sum(sc.ship_value), 0) AS value_inr,
               COALESCE(sum(sc.gross_penalty_inr), 0) AS gross_inr,
               COALESCE(sum(sc.net_penalty_inr), 0) AS net_inr,
               COALESCE(sum(sc.gross_penalty_inr) FILTER (WHERE NOT sc.waived AND x.notify_deadline >= %(now)s), 0) AS waivable_inr,
               COALESCE(sum(sc.gross_penalty_inr - sc.net_penalty_inr), 0) AS waived_inr,
               x.notify_deadline,
               count(*) FILTER (WHERE sc.params_unverified)::int AS unverified_rows
        FROM disruption_event e
        LEFT JOIN scoped sc ON sc.incident_id = e.id
        LEFT JOIN incident_exposure x ON x.incident_id = e.id
        {where}
        GROUP BY e.id, x.notify_deadline
        ORDER BY net_inr DESC, value_inr DESC, e.started_at DESC
        """

    def incidents(self, status: str | None = None, kind: str | None = None) -> list[dict]:
        where, params = [], {}
        if status:
            where.append("e.status = %(status)s")
            params["status"] = status
        if kind:
            where.append("e.kind = %(kind)s")
            params["kind"] = kind
        sql = self._INCIDENT_SQL.format(ship=self._ship(), where=("WHERE " + " AND ".join(where)) if where else "")
        rows = self._q(sql, params)
        if self.user.scoped:   # hide incidents with no shipment this user may see
            rows = [r for r in rows if r["shipments"] > 0]
        for rank, r in enumerate(rows, start=1):
            r["rank"] = rank
        return rows

    def incident(self, incident_id: int) -> dict | None:
        return next((r for r in self.incidents() if r["id"] == incident_id), None)

    def impact_rows(self, incident_id: int, limit: int | None = None, offset: int = 0) -> list[dict]:
        sql = f"""SELECT i.shipment_id, i.reason, i.est_delay_days, i.affected_value_inr, i.est_fill_rate, i.arrival_estimate,
                         i.days_late, i.late_penalty_inr, i.fill_penalty_inr, i.gross_penalty_inr, i.waived,
                         i.net_penalty_inr, i.clauses, i.params_unverified,
                         {SHIPMENT_COLS}
                  FROM incident_impact i JOIN {SHIPMENT_FROM} ON s.id = i.shipment_id
                  WHERE i.incident_id = %(inc)s AND {self._ship()}
                  ORDER BY i.net_penalty_inr DESC, s.value_inr DESC, s.id"""  # noqa: S608
        params: dict = {"inc": incident_id}
        if limit is not None:
            sql += " LIMIT %(limit)s OFFSET %(offset)s"
            params.update(limit=limit, offset=offset)
        return [mask_shipment(self.user, r) for r in self._q(sql, params)]

    def impact_count(self, incident_id: int) -> int:
        return self._one(f"SELECT count(*)::int AS n FROM incident_impact i JOIN shipment s ON s.id = i.shipment_id "  # noqa: S608
                         f"WHERE i.incident_id = %(inc)s AND {self._ship()}", {"inc": incident_id})["n"]

    def chain_counts(self, incident_id: int) -> dict:
        """Ontology chain: source -> warehouses -> carriers -> cities -> customers, with scoped counts and value."""
        base = (f"FROM incident_impact i JOIN {SHIPMENT_FROM} ON s.id = i.shipment_id "
                f"WHERE i.incident_id = %(inc)s AND {self._ship()}")
        out = {}
        for key, label, ident in (("warehouses", "s.dc_id", "s.dc_id"), ("carriers", "s.carrier_id", "s.carrier_id"),
                                  ("cities", "z.city", "s.zone_id"), ("customers", "cu.name", "s.customer_id::text")):
            out[key] = self._q(f"SELECT {ident} AS id, {label} AS label, count(*)::int AS shipments, "  # noqa: S608
                               f"sum(s.value_inr) AS value_inr {base} GROUP BY 1, 2 ORDER BY 3 DESC, 1", {"inc": incident_id})
        return out

    def exposure_breakdown(self, incident_id: int) -> dict:
        r = self._one(f"""SELECT count(*)::int AS shipments, COALESCE(sum(s.value_inr), 0) AS value_inr,
                   COALESCE(sum(i.late_penalty_inr), 0) AS late_inr, COALESCE(sum(i.fill_penalty_inr), 0) AS fill_inr,
                   COALESCE(sum(i.gross_penalty_inr), 0) AS gross_inr, COALESCE(sum(i.net_penalty_inr), 0) AS net_inr,
                   COALESCE(sum(i.gross_penalty_inr - i.net_penalty_inr), 0) AS waived_inr,
                   COALESCE(sum(i.gross_penalty_inr) FILTER (WHERE NOT i.waived AND x.notify_deadline >= %(now)s), 0) AS waivable_inr,
                   x.notify_deadline, max(i.days_late) AS max_days_late,
                   count(*) FILTER (WHERE i.params_unverified)::int AS unverified_rows
            FROM incident_impact i JOIN shipment s ON s.id = i.shipment_id
            LEFT JOIN incident_exposure x ON x.incident_id = i.incident_id
            WHERE i.incident_id = %(inc)s AND {self._ship()} GROUP BY x.notify_deadline""", {"inc": incident_id})  # noqa: S608
        if r is None:
            r = {"shipments": 0, "value_inr": 0, "late_inr": 0, "fill_inr": 0, "gross_inr": 0, "net_inr": 0,
                 "waived_inr": 0, "waivable_inr": 0, "notify_deadline": None, "max_days_late": 0, "unverified_rows": 0}
        r["top_customers"] = self._q(
            f"""SELECT cu.name AS customer, cu.chain, count(*)::int AS shipments, sum(i.net_penalty_inr) AS net_inr,
                       sum(i.gross_penalty_inr) AS gross_inr
                FROM incident_impact i JOIN {SHIPMENT_FROM} ON s.id = i.shipment_id
                WHERE i.incident_id = %(inc)s AND {self._ship()} GROUP BY cu.name, cu.chain
                ORDER BY sum(i.gross_penalty_inr) DESC, cu.name LIMIT 5""", {"inc": incident_id})  # noqa: S608
        r["by_chain"] = self._q(
            f"""SELECT cu.chain, count(*)::int AS shipments, sum(s.value_inr) AS value_inr, sum(i.gross_penalty_inr) AS gross_inr
                FROM incident_impact i JOIN {SHIPMENT_FROM} ON s.id = i.shipment_id
                WHERE i.incident_id = %(inc)s AND {self._ship()} GROUP BY cu.chain ORDER BY sum(i.gross_penalty_inr) DESC, cu.chain""",  # noqa: S608
            {"inc": incident_id})
        r["notifications"] = self._q(
            f"""SELECT n.customer_id, cu.name AS customer, n.notified_at FROM customer_notification n
                JOIN customer cu ON cu.id = n.customer_id
                WHERE n.incident_id = %(inc)s AND EXISTS (SELECT 1 FROM incident_impact i JOIN shipment s ON s.id = i.shipment_id
                    WHERE i.incident_id = n.incident_id AND s.customer_id = n.customer_id AND {self._ship()})
                ORDER BY n.notified_at""", {"inc": incident_id})  # noqa: S608
        return r

    # ------------------------------------------------------------ contracts (hybrid retrieval)
    def search_clauses(self, query: str, *, chain: str | None = None, k: int = 3) -> list[dict]:
        """Hybrid retrieval over contract_chunk: Postgres full-text and pgvector cosine, fused with RRF.
        Returns [{chain, clause_id, heading, page, text, score}]. Only chains this user may read."""
        scope = self._chain_scope()
        chain_sql = "AND d.chain = %(chain)s" if chain else ""
        words = re.findall(r"[a-z0-9]+", query.lower())
        cols = "ch.id, d.chain, ch.clause_id, ch.heading, ch.page, ch.text"
        rankings: list[list[dict]] = []
        if words:
            rankings.append(self._q(
                f"SELECT {cols}, ts_rank_cd(ch.tsv, q) AS score FROM contract_chunk ch JOIN msa_document d ON d.id = ch.msa_id, "  # noqa: S608
                f"websearch_to_tsquery('english', %(q)s) q WHERE ch.tsv @@ q AND {scope} {chain_sql} "
                f"ORDER BY score DESC, ch.id LIMIT 20", {"q": " or ".join(words), "chain": chain}))
        if self.settings is not None:
            try:
                vec = llm.vector_literal(llm.embed(self.settings, [query], kind="query")[0])
                rankings.append(self._q(
                    f"SELECT {cols}, 1 - (ch.embedding <=> %(v)s::vector) AS score FROM contract_chunk ch "  # noqa: S608
                    f"JOIN msa_document d ON d.id = ch.msa_id WHERE ch.embedding IS NOT NULL AND {scope} {chain_sql} "
                    f"ORDER BY ch.embedding <=> %(v)s::vector, ch.id LIMIT 20", {"v": vec, "chain": chain}))
            except llm.LLMError:
                pass   # full-text alone still answers
        fused = rrf(rankings)[:k]
        return [{k2: r[k2] for k2 in ("chain", "clause_id", "heading", "page", "text", "score")} for r in fused]

    def clause(self, chain: str, clause_id: str) -> dict | None:
        return self._one(
            f"SELECT d.chain, ch.clause_id, ch.heading, ch.page, ch.text, ch.params, ch.verified FROM contract_chunk ch "  # noqa: S608
            f"JOIN msa_document d ON d.id = ch.msa_id WHERE d.chain = %(chain)s AND ch.clause_id = %(cid)s AND {self._chain_scope()}",
            {"chain": chain, "cid": clause_id})

    def contract_file(self, chain: str) -> tuple[str | None, bool]:
        """The stored PDF file name of a chain's contract, if this user may read that chain's clauses.
        Returns (file, exists): exists is False for a chain with no contract at all (so the API can say 404, not 403)."""
        exists = self._one("SELECT d.file FROM msa_document d WHERE d.chain = %(chain)s", {"chain": chain})
        if exists is None:
            return None, False
        row = self._one(f"SELECT d.file FROM msa_document d WHERE d.chain = %(chain)s AND {self._chain_scope()}", {"chain": chain})  # noqa: S608
        return (row["file"] if row else None), True

    def unverified_sources(self, incident_id: int) -> list[dict]:
        """The contract clauses behind this incident's unverified rows: chain, clause and the waiting dead letter that
        explains it. `dead_letter_id` is None for a role that may not open the dead-letter queue."""
        rows = self._q(
            f"""SELECT DISTINCT cu.chain, c AS clause_id
                FROM incident_impact i JOIN {SHIPMENT_FROM} ON s.id = i.shipment_id, unnest(i.clauses) AS c
                WHERE i.incident_id = %(inc)s AND i.params_unverified AND {self._ship()}
                ORDER BY 1, 2""", {"inc": incident_id})  # noqa: S608
        out = []
        for r in rows:
            dl = self._one("""SELECT id FROM dead_letter WHERE feed = 'contracts' AND status = 'waiting'
                              AND payload->>'chain' = %(chain)s AND payload->>'clause_id' = %(cid)s ORDER BY id DESC LIMIT 1""",
                           {"chain": r["chain"], "cid": r["clause_id"]})
            if dl is None:
                continue
            out.append({"chain": r["chain"], "clause_id": r["clause_id"], "dead_letter_id": dl["id"] if self.user.dead_letters else None})
        return out

    # ------------------------------------------------------------ playbooks, agent runs, audit
    _PB_VISIBLE = "(NOT %(scoped)s OR pb.dc_scope <@ %(dcs)s::text[])"

    def playbooks(self, incident_id: int, *, role_only: bool = False) -> list[dict]:
        rows = self._q(
            f"""SELECT pb.* FROM playbook pb WHERE pb.incident_id = %(inc)s AND {self._PB_VISIBLE}
                {"AND pb.role = %(role)s" if role_only else ""} ORDER BY pb.id""",
            {"inc": incident_id, "scoped": self.user.scoped, "role": self.user.role})
        return rows

    def playbook(self, playbook_id: int) -> dict | None:
        return self._one(f"SELECT pb.* FROM playbook pb WHERE pb.id = %(id)s AND {self._PB_VISIBLE}",
                         {"id": playbook_id, "scoped": self.user.scoped})

    def latest_run(self, incident_id: int) -> dict | None:
        """Latest triage for this incident and this user's role (the cache)."""
        run = self._one("SELECT * FROM agent_run WHERE incident_id = %(inc)s AND role = %(role)s AND outcome <> 'running' "
                        "ORDER BY id DESC LIMIT 1", {"inc": incident_id, "role": self.user.role})
        if run:
            run["steps"] = self._q("SELECT seq, tool, args, result_summary, ms FROM agent_step WHERE run_id = %(r)s ORDER BY seq",
                                   {"r": run["id"]})
        return run

    # The audit log is append-only and keeps every run of the scenario morning (each restage starts the clock at
    # 05:00 again). The current morning starts at the latest `scenario.start` marker (sim/demo.py writes it after the
    # reset), or, for a log written before the marker existed, at the latest fresh read of the 05:00 ERP export.
    _MORNING_SQL = """SELECT GREATEST(
        COALESCE((SELECT max(id) FROM audit_log WHERE action = 'scenario.start'), 0),
        COALESCE((SELECT max(id) FROM audit_log WHERE action = 'erp.file_processed'
                  AND target LIKE 'erp\\_export\\_%%\\_0500%%' AND COALESCE((detail->>'new')::int, 0) > 0), 0))::bigint AS id"""

    def morning_start(self) -> int:
        """The audit id where the current scenario morning starts (0 when the log has no run yet)."""
        return int(self._one(self._MORNING_SQL)["id"])

    def audit_rows(self, *, limit: int = 100, offset: int = 0, action: str | None = None, morning: bool = False) -> dict:
        """The scoped audit trail. `morning=True` keeps only the current scenario morning and nothing stamped after the
        scenario clock, newest first by time (ties by id)."""
        where = ["(NOT %(scoped)s OR a.dc_ids && %(dcs)s::text[] OR a.actor = %(uid)s)"]
        params: dict = {"scoped": self.user.scoped, "limit": limit, "offset": offset}
        order = "a.id DESC"
        if action:
            where.append("a.action = %(action)s")
            params["action"] = action
        if morning:
            where.append("a.id >= %(since)s AND a.at <= %(now)s")
            params["since"] = self.morning_start()
            order = "a.at DESC, a.id DESC"
        clause = " AND ".join(where)
        total = self._one(f"SELECT count(*)::int AS n FROM audit_log a WHERE {clause}", params)["n"]  # noqa: S608
        rows = self._q(f"SELECT a.id, a.at, a.actor, a.action, a.target, a.detail, a.dc_ids FROM audit_log a WHERE {clause} "  # noqa: S608
                       f"ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s", params)
        return {"total": total, "items": rows, **({"morning_from": params["since"]} if morning else {})}

    # ------------------------------------------------------------ network, capacity, feeds
    def network(self) -> dict:
        return {
            "warehouses": self._q("SELECT id, city, lat, lon FROM warehouse ORDER BY id"),
            "zones": self._q("SELECT id, city, state, lat, lon, serving_dc, road_km, std_transit_days FROM zone ORDER BY id"),
            "carriers": [mask_carrier(self.user, r) for r in
                         self._q("SELECT id, name, dispatch_phone, bank_account FROM carrier ORDER BY id")],
            "suppliers": self._q("SELECT id, name, category, hub, state, lat, lon, quoted_lead_days FROM supplier ORDER BY id"),
        }

    def fleet_and_stock(self, skus: list[str] | None = None) -> dict:
        """Spare trucks per DC and carrier, and stock of the given SKUs per DC (capacity data, no PII)."""
        out = {"trucks": self._q("SELECT dc_id, carrier_id, trucks_free FROM fleet_capacity ORDER BY dc_id, carrier_id")}
        out["stock"] = self._q("SELECT dc_id, sku, cases FROM dc_stock WHERE sku = ANY(%(s)s) ORDER BY sku, dc_id",
                               {"s": list(skus or [])}) if skus else []
        return out

    _FEED_AUDIT = {"erp": "erp.file_processed", "bol": "bol.files_processed", "contracts": "contracts.ingested"}

    def feed_health(self) -> list[dict]:
        """Feed counters. A `last_at` after the scenario clock (left by an earlier run of the morning) is replaced by the
        feed's latest audited read in the current morning at or before the clock, or None: never a time from the future."""
        rows = self._q("SELECT * FROM feed_health ORDER BY feed")
        stale = [r for r in rows if r["last_at"] is not None and r["last_at"] > self.now]
        if stale:
            since = self.morning_start()
            for r in stale:
                action = self._FEED_AUDIT.get(r["feed"])
                hit = self._one("SELECT max(at) AS at FROM audit_log WHERE action = %(a)s AND id >= %(since)s AND at <= %(now)s",
                                {"a": action, "since": since}) if action else None
                r["last_at"] = hit["at"] if hit else None
        return rows

    # ------------------------------------------------------------ the live map (milestone 3 UI)
    def live_shipments(self) -> list[dict]:
        """Shipments still on the road, with what the active incidents say about each: the incident ids that touch it,
        the worst days late and the largest net penalty. No PII columns are selected."""
        rows = self._q(f"""
            SELECT s.id, s.dc_id, s.zone_id, s.carrier_id, cu.name AS customer, s.cases, s.value_inr, s.status,
                   s.departed_at, s.eta, s.promised_at, s.last_lat, s.last_lon, s.last_position_at,
                   s.carrier_status, s.carrier_status_at,
                   COALESCE(array_agg(i.incident_id ORDER BY i.incident_id) FILTER (WHERE i.incident_id IS NOT NULL),
                            '{{}}') AS incidents,
                   COALESCE(max(i.days_late), 0)::int AS days_late,
                   COALESCE(max(i.net_penalty_inr), 0) AS net_penalty_inr
            FROM {SHIPMENT_FROM}
            LEFT JOIN (SELECT ii.* FROM incident_impact ii JOIN disruption_event e ON e.id = ii.incident_id
                       WHERE e.status = 'active') i ON i.shipment_id = s.id
            WHERE s.status <> 'delivered' AND {self._ship()}
            GROUP BY s.id, cu.name ORDER BY s.id""")  # noqa: S608
        return [mask_shipment(self.user, r) for r in rows]

    def executed_plans(self) -> list[dict]:
        """The latest executed playbook per incident, among the playbooks this user may see."""
        return self._q(
            f"""SELECT DISTINCT ON (pb.incident_id) pb.incident_id, pb.id, pb.title, pb.action_type, pb.executed_by,
                       pb.approved_by, pb.executed_at
                FROM playbook pb WHERE pb.status = 'executed' AND {self._PB_VISIBLE}
                ORDER BY pb.incident_id, pb.executed_at DESC, pb.id DESC""",  # noqa: S608
            {"scoped": self.user.scoped})

    def dead_letters_waiting(self) -> int | None:
        """How many dead letters wait for a person; None for a role that may not read the queue."""
        if not self.user.dead_letters:
            return None
        return self._one("SELECT count(*)::int AS n FROM dead_letter WHERE status = 'waiting'")["n"]

    def dead_letters(self, *, status=None, feed=None, reason_code=None, limit: int = 100, offset: int = 0) -> dict | None:
        if not self.user.dead_letters:
            return None
        where, params = [], {"limit": limit, "offset": offset}
        for col, val in (("status", status), ("feed", feed), ("reason_code", reason_code)):
            if val:
                where.append(f"{col} = %({col})s")
                params[col] = val
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return {
            "total": self._one(f"SELECT count(*)::int AS n FROM dead_letter {clause}", params)["n"],  # noqa: S608
            "by_reason": self._q(f"SELECT reason_code, count(*)::int AS n FROM dead_letter {clause} GROUP BY 1 ORDER BY 2 DESC", params),  # noqa: S608
            "items": self._q(f"SELECT * FROM dead_letter {clause} ORDER BY id DESC LIMIT %(limit)s OFFSET %(offset)s", params),  # noqa: S608
        }
