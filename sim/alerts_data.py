"""The six scenario disruptions as IMD-style alert JSON, published at their scenario times (IST)."""

from __future__ import annotations

from datetime import datetime

from tower.clock import IST


def _t(h: int, m: int) -> str:
    return datetime(2026, 11, 13, h, m, tzinfo=IST).isoformat()


def scenario_alerts(prakash_supplier_id: str) -> list[dict]:
    return [
        {"alert_id": "IMD-TS-2026-1113-0412", "source": "imd", "kind": "weather", "severity": "orange",
         "title": "NH44 closed near Adilabad",
         "detail": "Heavy rain; Telangana police closed NH44 between Adilabad and Nirmal. Reopening estimate 26 hours.",
         "lat": 19.67, "lon": 78.53, "radius_km": 70, "issued_at": _t(5, 10)},
        {"alert_id": "DCOPS-KOL-0540", "source": "dc_ops", "kind": "facility", "severity": 3,
         "title": "Kolkata DC dock 2 conveyor down",
         "detail": "Sorter fault at dock 2; outbound loading at 40% capacity until the part arrives (ETA 18:00).",
         "dc_id": "DC-KOL", "issued_at": _t(5, 40)},
        {"alert_id": "CARRIER-GF-0605", "source": "carrier_notice", "kind": "carrier", "severity": "RED",
         "title": "Ganga Freight drivers on strike in UP",
         "detail": "Union strike across Lucknow and Kanpur depots; Ganga Freight trucks into UP are holding at the border.",
         "carrier_id": "C2", "states": ["Uttar Pradesh"], "issued_at": _t(6, 5)},
        {"alert_id": "IMD-CY-2026-1113-0730", "source": "imd", "kind": "weather", "severity": "red",
         "title": "Cyclone watch on the Odisha coast",
         "detail": "IMD cyclone watch; landfall possible near Gopalpur in 48 to 60 hours. Coastal lanes at risk from tomorrow night.",
         "lat": 19.8, "lon": 85.5, "radius_km": 160, "issued_at": _t(7, 30)},
        {"alert_id": "EDI-S-PRAKASH-0847", "source": "supplier_edi", "kind": "supplier", "severity": "yellow",
         "title": "Prakash Consumer Products inbound 5 days late",
         "detail": "Snacks supplier missed its dispatch window again. Snack SKUs at Mumbai and Delhi run out before the late stock lands.",
         "supplier_id": prakash_supplier_id, "issued_at": _t(8, 47)},
        {"alert_id": "IMD-FG-2026-1113-0912", "source": "imd", "kind": "weather", "severity": "Orange",
         "title": "Dense fog on the Yamuna Expressway",
         "detail": "Visibility under 50 m until about 11:30; trucks are holding at toll plazas.",
         "lat": 27.6, "lon": 77.6, "radius_km": 55, "issued_at": _t(9, 12)},
    ]
