"""Users, roles, bearer-token auth and field masking. The one place that says who may see and do what.

Row scope (which shipments) is applied in SQL by tower.scoped.ScopedStore; field masking is applied here,
before a row leaves the data-access layer.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from tower.config import Settings


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    dcs: tuple[str, ...] | None      # None: all India. Otherwise only shipments leaving these DCs.
    driver_contact: str              # full | masked | hidden   (driver name stays; phone follows this level)
    carrier_contact: str             # carrier dispatch phone: full | masked | hidden
    bank: str                        # carrier bank account: full | masked | hidden
    contracts: str                   # all | scoped | none
    can_execute: bool                # may run operational playbooks inside his scope
    can_approve: bool                # may approve playbooks above the threshold
    dead_letters: bool = False       # may read the dead-letter queue (payloads hold shipment data from every DC)

    @property
    def scoped(self) -> bool:
        return self.dcs is not None

    def sees_dc(self, dc_id: str) -> bool:
        return self.dcs is None or dc_id in self.dcs

    def covers(self, dc_ids) -> bool:
        return self.dcs is None or all(d in self.dcs for d in dc_ids)


USERS: dict[str, User] = {
    "asha": User("asha", "Asha (VP Operations)", "vp_operations", None, "full", "full", "masked", "all", True, True, True),
    "kiran": User("kiran", "Kiran (Dispatcher South)", "dispatcher_south", ("DC-HYD",), "masked", "masked", "masked",
                  "scoped", True, False),
    "meera": User("meera", "Meera (Finance)", "finance", None, "hidden", "hidden", "full", "all", False, False),
}


def authenticate(settings: Settings, authorization: str | None) -> User | None:
    """Bearer token -> User, or None. Every configured token is compared (constant time per comparison)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    found: User | None = None
    for uid, expected in (("asha", settings.token_asha), ("kiran", settings.token_kiran), ("meera", settings.token_meera)):
        if expected and token and hmac.compare_digest(expected.encode(), token.encode()):
            found = USERS[uid]
    return found


# ------------------------------------------------------------------ masking
def mask_phone(v: str | None) -> str | None:
    """'+91 98490 22713' -> '+91 ***** **713' (last three digits only)."""
    if not v:
        return v
    digits = [c for c in v if c.isdigit()]
    return "+91 ***** **" + "".join(digits[-3:])


def mask_bank(v: str | None) -> str | None:
    """'HDFC A/c 5010 0033 4471' -> 'HDFC A/c **** **** 4471'."""
    if not v:
        return v
    parts = v.split()
    return " ".join(["****" if p.isdigit() and i < len(parts) - 1 else p for i, p in enumerate(parts)])


def apply_level(value: str | None, level: str, masker) -> str | None:
    if value is None:
        return None
    if level == "full":
        return value
    if level == "masked":
        return masker(value)
    return None


def mask_shipment(user: User, row: dict) -> dict:
    """Driver and vehicle PII on a shipment row. Hidden fields are removed from the output entirely."""
    if "driver_phone" in row:
        row["driver_phone"] = apply_level(row["driver_phone"], user.driver_contact, mask_phone)
        if user.driver_contact == "hidden":
            row.pop("driver_phone", None)
            row.pop("driver_name", None)
    return row


def mask_carrier(user: User, row: dict) -> dict:
    if "dispatch_phone" in row:
        row["dispatch_phone"] = apply_level(row["dispatch_phone"], user.carrier_contact, mask_phone)
        if user.carrier_contact == "hidden":
            row.pop("dispatch_phone")
    if "bank_account" in row:
        row["bank_account"] = apply_level(row["bank_account"], user.bank, mask_bank)
        if user.bank == "hidden":
            row.pop("bank_account")
    return row
