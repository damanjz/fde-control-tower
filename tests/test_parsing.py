from datetime import datetime
from decimal import Decimal

import pytest

from tower.clock import IST
from tower.erp import parse_export
from tower.parsing import ParseError, norm, parse_amount, parse_datetime, parse_int


@pytest.mark.parametrize("text, expected", [
    ("4,20,000", "420000.00"),
    ("420000", "420000.00"),
    ("420000.50", "420000.50"),
    ("₹4.2 L", "420000.00"),
    ("₹ 4,20,000", "420000.00"),
    ("Rs. 4.2 lakh", "420000.00"),
    ("4.2L", "420000.00"),
    ("1.1 Cr", "11000000.00"),
    ("INR 12,34,567.89", "1234567.89"),
    ("  2.5 lacs ", "250000.00"),
])
def test_amounts(text, expected):
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "TBD", "N/A", "see invoice", "4.2.1 L", "-5"])
def test_bad_amounts(text):
    with pytest.raises(ParseError) as e:
        parse_amount(text)
    assert e.value.code == "bad_amount"


def test_dates_all_formats_agree():
    want = datetime(2026, 11, 13, 4, 30, tzinfo=IST)
    serial = (datetime(2026, 11, 13, 4, 30) - datetime(1899, 12, 30)).total_seconds() / 86400
    for text in ["13/11/2026 04:30", "2026-11-13T04:30:00+05:30", "2026-11-13 04:30:00", "13-Nov-2026 04:30",
                 f"{serial:.8f}", "2026-11-12T23:00:00+00:00"]:
        assert parse_datetime(text) == want, text


def test_excel_serial_known_value():
    # 46339 is 2026-11-13 (serial 1 = 1900-01-01 in Excel's 1900 system, with its leap-year quirk)
    assert parse_datetime("46339") == datetime(2026, 11, 13, 0, 0, tzinfo=IST)
    assert parse_datetime("46339.5") == datetime(2026, 11, 13, 12, 0, tzinfo=IST)


def test_date_only_and_blank_and_bad():
    assert parse_datetime("14/11/2026") == datetime(2026, 11, 14, 0, 0, tzinfo=IST)
    assert parse_datetime("") is None and parse_datetime("  ") is None and parse_datetime(None) is None
    with pytest.raises(ParseError):
        parse_datetime("31/02/2026")
    with pytest.raises(ParseError):
        parse_datetime("next tuesday")


def test_day_first_not_month_first():
    assert parse_datetime("03/04/2026").month == 4


def test_ints():
    assert parse_int("24") == 24 and parse_int("24.0") == 24 and parse_int("1,200 cases") == 1200
    with pytest.raises(ParseError):
        parse_int("two")


def test_norm():
    assert norm("Shipment No.") == "shipmentno" and norm("invoice value (INR)") == "invoicevalueinr"


def test_header_variants_map_to_same_fields():
    a = "Shipment_No,Order_No,DC,City_Code,Carrier,Customer,Cases,Amount,Status,Dispatch_Dt,ETA,Promise_Dt,Items"
    b = ("shipment id;SO number;origin dc;dest city;transporter;customer name;total cases;invoice value (INR);"
         "ship status;departed;expected arrival;promised by;line items")
    c = "SHIP_REF\tSO_REF\tDC_CODE\tDEST\tCARRIER_NM\tCUST\tQTY_CASES\tINV_AMT\tSTS\tDEP_DT\tETA_DT\tPROMISE_DT\tSKU_LINES"
    fa, _ = parse_export(a + "\n")
    fb, _ = parse_export(b + "\n")
    fc, _ = parse_export(c + "\n")
    assert fa == fb == fc
    assert fa[0] == "shipment_no" and "promised_at" in fa and None not in fa


def test_parse_export_bom_whitespace_and_malformed():
    text = ("﻿Shipment_No,Order_No,City_Code,Items\r\n"
            "  SH-1 , SO-1 ,HYD,PE-001:2\r\n"
            "\r\n"
            "SH-2,SO-2,HYD\n"                      # too few columns
            '"SH-3,SO-3,HYD,x\n'                   # broken quoting
            "*** ERP EXPORT INTERRUPTED ***\n")
    fields, rows = parse_export(text)
    assert fields == ["shipment_no", "order_no", "city", "items"]
    good = [r for r in rows if r[1] is not None]
    bad = [r for r in rows if r[1] is None]
    assert len(good) == 1 and good[0][1]["shipment_no"] == "SH-1" and good[0][1]["order_no"] == "SO-1"
    assert len(bad) == 3
