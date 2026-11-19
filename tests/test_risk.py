"""Impact rules per incident kind, exposure math (cap, grace, fill rate, force-majeure waiver timing), idempotency."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.world import NOW, T0, customer, incident, notify, ship
from tower import risk
from tower.risk import Terms, days_late, delay_model, price_shipment

KIRANA = "Kirana network"


# ------------------------------------------------------------------ pure math
def test_days_late_grace_and_rounding_up():
    p = T0
    assert days_late(p + timedelta(hours=-5), p) == 0
    assert days_late(p + timedelta(hours=12), p) == 0            # exactly the 12 hour grace: not late
    assert days_late(p + timedelta(hours=12, minutes=1), p) == 1  # a part day counts as a day
    assert days_late(p + timedelta(days=2, hours=5), p) == 2      # 2.21 - 0.5 = 1.71 -> 2
    assert days_late(p + timedelta(days=3, hours=12), p) == 3


def test_late_penalty_rate_and_cap():
    t = Terms()
    three = price_shipment(value=Decimal(1_000_000), promised=T0, arrival=T0 + timedelta(days=3, hours=6), terms=t)
    assert three.days_late == 3 and three.late_inr == Decimal("45000.00") and three.clauses == ["MSA-7.2"]
    ten = price_shipment(value=Decimal(1_000_000), promised=T0, arrival=T0 + timedelta(days=10, hours=6), terms=t)
    assert ten.days_late == 10 and ten.late_inr == Decimal("100000.00")      # 15% capped at 10%


def test_fill_rate_penalty_applies_below_the_floor_only():
    t = Terms()
    low = price_shipment(value=Decimal(500_000), promised=T0, arrival=T0, terms=t, affected_value=Decimal(200_000), fill_rate=0.6)
    assert low.fill_inr == Decimal("4000.00") and low.clauses == ["MSA-9.1"] and low.late_inr == 0
    ok = price_shipment(value=Decimal(500_000), promised=T0, arrival=T0, terms=t, affected_value=Decimal(200_000), fill_rate=0.96)
    assert ok.gross_inr == 0


def test_force_majeure_waives_weather_penalties_only_when_notified():
    t = Terms()
    kw = dict(value=Decimal(1_000_000), promised=T0, arrival=T0 + timedelta(days=2), terms=t)
    plain = price_shipment(**kw, weather=True, notified=False)
    assert plain.net_inr == plain.gross_inr > 0 and not plain.waived
    waived = price_shipment(**kw, weather=True, notified=True)
    assert waived.waived and waived.net_inr == 0 and waived.gross_inr == plain.gross_inr and "MSA-14.3" in waived.clauses
    not_weather = price_shipment(**kw, weather=False, notified=True)
    assert not not_weather.waived and not_weather.net_inr == plain.gross_inr


def test_delay_model_numbers():
    def ev(kind, sev, title="t", detail="d"):
        return {"kind": kind, "severity": sev, "title": title, "detail": detail}
    assert delay_model(ev("weather", "high", "NH44 closed", "Reopening estimate 26 hours.")) == ("closure", 2.08)
    assert delay_model(ev("weather", "high", "Dense fog on the Yamuna Expressway")) == ("fog", 0.75)
    assert delay_model(ev("weather", "critical", "Cyclone watch")) == ("cyclone", 3.0)
    assert delay_model(ev("weather", "medium", "Heavy rain")) == ("weather", 1.0)
    assert delay_model(ev("facility", "high")) == ("facility", 1.5)
    assert delay_model(ev("carrier", "critical")) == ("carrier", 3.0)
    assert delay_model(ev("supplier", "medium")) == ("supplier", 0.5)


def test_lane_distance():
    # the DC to Nagpur lane passes about 19 km from a point near Adilabad's latitude band
    d = risk.lane_distance_km(17.485, 78.487, 21.146, 79.088, 19.5, 79.0)
    assert 10 < d < 30
    assert risk.lane_distance_km(17.485, 78.487, 13.083, 80.271, 19.5, 79.0) > 200


# ------------------------------------------------------------------ impact rules against the database
def run(pool, iid):
    with pool.connection() as conn:
        ev = conn.execute("SELECT * FROM disruption_event WHERE id = %s", (iid,)).fetchone()
        risk.recompute_incident(conn, ev, NOW)
        return {r["shipment_id"]: r for r in conn.execute("SELECT * FROM incident_impact WHERE incident_id = %s", (iid,)).fetchall()}


@pytest.mark.db
def test_geo_event_selects_lane_segments_inside_the_radius(pool, msa_loaded):
    with pool.connection() as c:
        ship(c, "T-IN", city="Nagpur")
        ship(c, "T-OUT", city="Chennai")
        ship(c, "T-DONE", city="Nagpur", status="delivered")
        ship(c, "T-EARLY", city="Nagpur", eta=T0 - timedelta(hours=1))
        iid = incident(c, "g1", "weather", "NH44 closed", "Road closed. Reopening estimate 26 hours.", lat=19.5, lon=79.0, radius_km=60)
    rows = run(pool, iid)
    assert set(rows) == {"T-IN"}
    assert float(rows["T-IN"]["est_delay_days"]) == 2.08


@pytest.mark.db
def test_facility_event_selects_departures_inside_the_window(pool, msa_loaded):
    with pool.connection() as c:
        ship(c, "T-IN", dc="DC-KOL", city="Patna", departed=T0 + timedelta(hours=1))
        ship(c, "T-BEFORE", dc="DC-KOL", city="Patna", departed=T0 - timedelta(hours=1))
        ship(c, "T-AFTER", dc="DC-KOL", city="Patna", departed=T0 + timedelta(hours=14))
        ship(c, "T-OTHER", dc="DC-HYD", city="Nagpur", departed=T0 + timedelta(hours=1))
        iid = incident(c, "f1", "facility", "Dock down", "Outbound at 40% until the part arrives (ETA 18:00).",
                       started=T0 + timedelta(minutes=40), dc_id="DC-KOL")
    rows = run(pool, iid)
    assert set(rows) == {"T-IN"}
    # eta 10 h after NOW, promised +12 h, delay 1.5 d (high): arrival is 1.5 d after eta -> ceil(1.5 - 0.5 - 0.5) = 1 day late
    assert rows["T-IN"]["days_late"] == 1 and rows["T-IN"]["late_penalty_inr"] == Decimal("15000.00")


@pytest.mark.db
def test_carrier_strike_selects_carrier_into_named_states(pool, msa_loaded):
    with pool.connection() as c:
        ship(c, "T-IN", dc="DC-DEL", city="Lucknow", carrier="C2")
        ship(c, "T-OTHERSTATE", dc="DC-HYD", city="Nagpur", carrier="C2")
        ship(c, "T-OTHERCARRIER", dc="DC-DEL", city="Lucknow", carrier="C1")
        iid = incident(c, "c1", "carrier", "Strike", "Drivers on strike", severity="critical", carrier_id="C2",
                       states=["Uttar Pradesh"])
    rows = run(pool, iid)
    assert set(rows) == {"T-IN"} and float(rows["T-IN"]["est_delay_days"]) == 3.0


@pytest.mark.db
def test_supplier_delay_selects_lines_of_the_supplier_at_low_stock_dcs(pool, msa_loaded):
    with pool.connection() as c:
        sku = c.execute("SELECT sku FROM product WHERE supplier_id = 'S036' ORDER BY sku LIMIT 1").fetchone()["sku"]
        other = c.execute("SELECT sku FROM product WHERE supplier_id <> 'S036' ORDER BY sku LIMIT 1").fetchone()["sku"]
        assert c.execute("SELECT cases FROM dc_stock WHERE dc_id = 'DC-MUM' AND sku = %s", (sku,)).fetchone()["cases"] < 40
        ship(c, "T-IN", dc="DC-MUM", city="Pune", lines=[(sku, 20, 200_000), (other, 30, 300_000)], value=500_000)
        ship(c, "T-HIGHSTOCK", dc="DC-HYD", city="Nagpur", lines=[(sku, 20, 200_000)], value=200_000)
        ship(c, "T-NOSKU", dc="DC-MUM", city="Pune", lines=[(other, 30, 300_000)], value=300_000)
        iid = incident(c, "s1", "supplier", "Supplier late", "Snacks supplier 5 days late.", supplier_id="S036")
    rows = run(pool, iid)
    assert set(rows) == {"T-IN"}
    r = rows["T-IN"]
    assert r["affected_value_inr"] == Decimal("200000.00") and float(r["est_fill_rate"]) == 0.6     # 5 days x 8% = 40% short
    assert r["fill_penalty_inr"] == Decimal("4000.00") and "MSA-9.1" in r["clauses"]


@pytest.mark.db
def test_force_majeure_waiver_deadline_is_six_hours_from_event_start(pool, msa_loaded):
    with pool.connection() as c:
        s1 = ship(c, "T-ONTIME", city="Nagpur", eta=T0 + timedelta(days=1))
        s2 = ship(c, "T-LATE", city="Pune", dc="DC-MUM", eta=T0 + timedelta(days=1))
        # promised is only 1 hour after eta so a 2.08 day delay is 2 days late
        c.execute("UPDATE shipment SET promised_at = eta + interval '1 hour'")
        iid = incident(c, "w1", "weather", "NH44 closed", "Closed. Reopening estimate 26 hours.", lat=19.5, lon=79.0, radius_km=700)
        notify(c, iid, customer(c, KIRANA, "Nagpur"), T0 + timedelta(hours=5, minutes=59))
        notify(c, iid, customer(c, KIRANA, "Pune"), T0 + timedelta(hours=6, minutes=1))
    rows = run(pool, iid)
    assert {s1, s2} <= set(rows)
    assert rows[s1]["waived"] is True and rows[s1]["net_penalty_inr"] == 0 and rows[s1]["gross_penalty_inr"] > 0
    assert rows[s2]["waived"] is False and rows[s2]["net_penalty_inr"] == rows[s2]["gross_penalty_inr"] > 0
    with pool.connection() as c:
        x = c.execute("SELECT * FROM incident_exposure WHERE incident_id = %s", (iid,)).fetchone()
    assert x["notify_deadline"] == T0 + timedelta(hours=6)


@pytest.mark.db
def test_recompute_is_idempotent(pool, msa_loaded):
    with pool.connection() as c:
        ship(c, "T-A", city="Nagpur")
        ship(c, "T-B", dc="DC-DEL", city="Lucknow", carrier="C2")
        i1 = incident(c, "i1", "weather", "NH44 closed", "Closed. 26 hours.", lat=19.5, lon=79.0, radius_km=60)
        i2 = incident(c, "i2", "carrier", "Strike", "Strike", carrier_id="C2", states=["Uttar Pradesh"], severity="critical")

    def snap():
        with pool.connection() as c:
            return [dict(r) for r in c.execute(
                "SELECT incident_id, shipment_id, est_delay_days, days_late, gross_penalty_inr, net_penalty_inr, clauses "
                "FROM incident_impact ORDER BY 1, 2").fetchall()]
    risk.recompute_all(pool, NOW)
    first = snap()
    risk.recompute_all(pool, NOW)
    assert snap() == first and {r["incident_id"] for r in first} == {i1, i2}


@pytest.mark.db
def test_unverified_contract_params_flag_the_exposure(pool, msa_loaded):
    """Spar's PDF says 2.5% where the master says 2%: the PDF value is used and the row is flagged."""
    with pool.connection() as c:
        sku = c.execute("SELECT sku FROM product WHERE supplier_id = 'S036' ORDER BY sku LIMIT 1").fetchone()["sku"]
        city = c.execute("SELECT z.city FROM customer cu JOIN zone z ON z.id = cu.zone_id WHERE cu.chain = 'Spar' "
                         "ORDER BY z.id LIMIT 1").fetchone()["city"]
        ship(c, "T-SPAR", dc="DC-MUM", city=city, chain="Spar", lines=[(sku, 20, 200_000)], value=200_000)
        iid = incident(c, "s2", "supplier", "Supplier late", "5 days late", supplier_id="S036")
    r = run(pool, iid)["T-SPAR"]
    assert r["params_unverified"] is True and r["fill_penalty_inr"] == Decimal("5000.00")    # 2.5% of 200,000
