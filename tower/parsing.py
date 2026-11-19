"""Parsers for the messy values legacy systems emit: dates, amounts, integers, headers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tower.clock import IST


class ParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_EXCEL_EPOCH = datetime(1899, 12, 30)
_DMY = re.compile(
    r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
_DMON = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ -](\d{4})(?:[ T,]+(\d{1,2}):(\d{2}))?$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_datetime(value: str | None) -> datetime | None:
    """Return an aware datetime, or None for blank. Naive values are India time (IST).

    Accepted: DD/MM/YYYY [HH:MM[:SS]], ISO 8601 (with or without offset), 13-Nov-2026, Excel serial numbers.
    DD/MM is always day-first; this distributor is in India.
    """
    if value is None:
        return None
    s = value.strip().strip("'\"")
    if not s or s.lower() in {"na", "n/a", "null", "none", "-", "tbd"}:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", s):  # Excel serial day count (dates 1954 to 2173)
        return (_EXCEL_EPOCH + timedelta(seconds=round(float(s) * 86400))).replace(tzinfo=IST)
    m = _DMY.match(s)
    if m:
        d, mo, y, hh, mi, ss = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=IST)
        except ValueError:
            raise ParseError("bad_date", f"not a real date: {value!r}") from None
    m = _DMON.match(s)
    if m and m.group(2).lower() in _MONTHS:
        d, mon, y, hh, mi = m.groups()
        return datetime(int(y), _MONTHS[mon.lower()], int(d), int(hh or 0), int(mi or 0), tzinfo=IST)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ParseError("bad_date", f"unrecognised date format: {value!r}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


_AMOUNT_SUFFIX = re.compile(r"(crores?|cr|lakhs?|lacs?|lk|l|k)\.?$", re.IGNORECASE)
_MULT = {"cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000, "lakh": 100_000, "lakhs": 100_000,
         "lac": 100_000, "lacs": 100_000, "lk": 100_000, "l": 100_000, "k": 1_000}


def parse_amount(value: str | None) -> Decimal:
    """Rupees as Decimal from '420000', '4,20,000', 'Rs. 4,20,000.50', '₹4.2 L', '4.2 lakh', '1.1 Cr'."""
    if value is None or not value.strip():
        raise ParseError("bad_amount", "amount is blank")
    s = value.strip()
    s = re.sub(r"(₹|rs\.?|inr)", "", s, flags=re.IGNORECASE).strip()
    mult = 1
    m = _AMOUNT_SUFFIX.search(s)
    if m:
        mult = _MULT[m.group(1).lower().rstrip(".")]
        s = s[: m.start()].strip()
    s = s.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise ParseError("bad_amount", f"cannot read an amount from {value!r}")
    try:
        return (Decimal(s) * mult).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ParseError("bad_amount", f"cannot read an amount from {value!r}") from None


def parse_int(value: str | None, what: str = "number") -> int:
    if value is None or not value.strip():
        raise ParseError(f"missing_{what}", f"{what} is blank")
    s = re.sub(r"(?i)\s*(cases?|cs|nos?)\.?$", "", value.strip()).replace(",", "")
    if not re.fullmatch(r"\d+(\.0+)?", s):
        raise ParseError(f"bad_{what}", f"{what} is not a whole number: {value!r}")
    return int(float(s))


def norm(s: str) -> str:
    """Lowercase alphanumerics only: 'Shipment No.' -> 'shipmentno'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()
