"""PDF generator for the simulator: MSA contracts and bills of lading.

Run with an interpreter that has reportlab (the simulator uses CT_PDF_PYTHON), never from the tower:

    python sim/make_pdfs.py spec.json

The spec is written by sim/pdf_sim.py. Two layouts for contracts; three BOL modes (ok, garbled, blank).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

# (clause number, heading, article, text template). {chain} is the customer, {rate} {cap} {fillpen} {fm} fill the money clauses.
FILLER = {
    "1.1": ("Definitions", "Definitions and interpretation",
            "In this Agreement, Supplier means Tarangini Distribution Pvt Ltd, Customer means {chain} and its stores, "
            "Consignment means a delivery of goods under one sales order, and Invoice Value means the invoiced price of the goods "
            "before tax."),
    "2.1": ("Term", "Term and renewal",
            "This Agreement starts on 1 April 2026 and continues until 31 March 2027. It renews for successive periods of twelve "
            "months unless either party gives ninety days written notice."),
    "3.1": ("Orders", "Orders and forecasts",
            "The Customer places orders through the Supplier's ordering portal. The Supplier confirms each order within one working "
            "day. Rolling four-week forecasts are indicative and do not bind either party."),
    "4.1": ("Delivery windows", "Delivery points and windows",
            "Goods are delivered to the receiving dock of each store or distribution point named in the order, within the delivery "
            "window of the order. Receiving hours are 06:00 to 20:00 unless the order says otherwise."),
    "5.1": ("Pricing", "Prices and price changes",
            "Prices are as per the price list annexed to the order. The Supplier gives thirty days notice of any price increase. "
            "Promotional prices apply only to the period stated."),
    "6.1": ("Payment terms", "Payment terms",
            "The Customer pays each undisputed invoice within forty-five days of the invoice date by bank transfer. Late payment "
            "bears interest at 1% per month. Disputed amounts are notified within ten days of receipt of the invoice."),
    "7.1": ("Delivery standard", "Delivery standard",
            "The Supplier shall deliver each Consignment complete, undamaged and with a valid invoice and proof of delivery. "
            "Delivery is complete when the Customer's receiving staff sign the proof of delivery."),
    "8.1": ("Returns", "Returns and damaged goods",
            "The Customer may return goods that are damaged, expired or not as ordered within seven days of delivery. The Supplier "
            "collects them at its cost and issues a credit note within fourteen days."),
    "9.2": ("Substitutions", "Substitutions",
            "If an ordered SKU is unavailable, the Supplier may offer a substitute of equal or better specification at the same "
            "price. The Customer may refuse a substitute without penalty."),
    "10.1": ("Liability cap", "Limitation of liability",
             "Each party's total liability under this Agreement in a contract year is limited to the fees invoiced under it in the "
             "preceding twelve months. Nothing limits liability for fraud or wilful default."),
    "11.1": ("Data protection", "Data protection",
             "Each party processes personal data, including delivery staff and driver details, only to perform this Agreement, "
             "keeps it secure, and deletes it when no longer needed. Driver contact details are shared only with the Customer's "
             "receiving staff for a delivery."),
    "12.1": ("Confidentiality", "Confidentiality",
             "Each party keeps the other's prices, volumes and business plans confidential during the term and for three years "
             "after it, except as required by law."),
    "13.1": ("Termination", "Termination",
             "Either party may terminate for material breach that is not remedied within thirty days of written notice. Accrued "
             "rights and payments survive termination."),
    "14.1": ("Force majeure events", "Force majeure: events",
             "Force majeure means an event beyond a party's reasonable control, including flood, cyclone, storm, extreme fog, "
             "earthquake, epidemic, war, riot, or government action closing a road, port or facility."),
    "14.2": ("Force majeure relief", "Force majeure: relief",
             "A party prevented from performing by force majeure is excused for as long as the event lasts, if it takes reasonable "
             "steps to limit the effect and resumes performance as soon as practicable."),
    "15.1": ("Governing law", "Governing law and courts",
             "This Agreement is governed by Indian law. The courts at Hyderabad have exclusive jurisdiction."),
    "16.1": ("Disputes", "Dispute resolution",
             "The parties first try to settle a dispute by senior management talks within thirty days. Failing that, the dispute is "
             "referred to arbitration in Hyderabad under the Arbitration and Conciliation Act, 1996."),
}
MONEY = {
    "A": {
        "7.2": ("Late delivery", "If a Consignment reaches the delivery point later than its promised delivery time, allowing a grace "
                "period of 12 hours, the Supplier shall credit the Customer with {rate}% of the Invoice Value of that Consignment for each "
                "day of delay, capped at {cap}% of the Invoice Value."),
        "9.1": ("Fill rate", "Where the fill rate for a listed SKU is below {minfill}% in a calendar week, the Supplier shall credit "
                "the Customer {fillpen}% of that week's invoice value of that SKU. Fill rate is cases delivered divided by cases ordered."),
        "14.3": ("Force majeure: notification", "For declared weather events, the penalties under clauses 7.2 and 9.1 are waived if the "
                 "Supplier notifies the Customer within {fm} hours of the event."),
    },
    "B": {
        "7.2": ("Late delivery (service credit)", "Where delivery is made after the promised time and the grace period of 12 hours, the "
                "Supplier shall pay a service credit of {rate} percent of the Invoice Value per day late. The aggregate credit for any one "
                "Consignment shall not exceed {cap} percent of its Invoice Value."),
        "9.1": ("Fill rate (service credit)", "If the weekly fill rate for any listed SKU falls less than {minfill} percent, the Supplier "
                "shall allow a credit of {fillpen} percent of the invoice value for that SKU for that week."),
        "14.3": ("Weather events", "Where a weather event is declared by the India Meteorological Department or a state authority, the "
                 "credits in clauses 7.2 and 9.1 do not apply if the Customer has been told of the event within {fm} hours of its start."),
    },
}
ORDER = ["1.1", "2.1", "3.1", "4.1", "5.1", "6.1", "7.1", "7.2", "8.1", "9.1", "9.2", "10.1", "11.1", "12.1", "13.1",
         "14.1", "14.2", "14.3", "15.1", "16.1"]


def money_values(drift: dict | None) -> dict:
    v = {"rate": "1.5", "cap": "10", "minfill": "95", "fillpen": "2", "fm": "6"}
    v.update({k: str(x) for k, x in (drift or {}).items()})
    return v


def make_contract(c: dict, out: Path) -> None:
    tpl = c["template"]
    c = {**c, "chain": escape(c["chain"])}
    vals = money_values(c.get("drift"))
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=10, leading=14)
    head = ParagraphStyle("head", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=11, spaceBefore=8, spaceAfter=3)
    title = ParagraphStyle("title", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18)
    art = ParagraphStyle("art", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceBefore=12,
                         textColor=colors.HexColor("#333333"))
    story = []
    if tpl == "A":
        story += [Paragraph("MASTER SERVICES AGREEMENT", title), Paragraph(f"Contract reference {c['ref']}", body),
                  Paragraph(f"Between Tarangini Distribution Pvt Ltd and {c['chain']}", body), Spacer(1, 8)]
    else:
        story += [Spacer(1, 40 * mm), Paragraph("SUPPLY AND SERVICES AGREEMENT", title),
                  Paragraph(f"Agreement no. {c['ref']}", body), Paragraph(f"Customer: {c['chain']}", body),
                  Paragraph("Supplier: Tarangini Distribution Pvt Ltd", body), PageBreak()]
    last_art = None
    for num in ORDER:
        if num in MONEY[tpl]:
            heading, text = MONEY[tpl][num]
        else:
            heading, _, text = FILLER[num]
        text = text.format(chain=c["chain"], **vals)
        if tpl == "A":
            story += [Paragraph(f"MSA-{num}&nbsp;&nbsp;{heading}", head), Paragraph(text, body)]
        else:
            major = num.split(".")[0]
            if major != last_art:
                story.append(Paragraph(f"ARTICLE {major}", art))
                last_art = major
            story += [Paragraph(f"Clause {num} - {heading.upper()}", head), Paragraph(text, body)]
        if tpl == "A" and num == "7.1":
            story.append(Spacer(1, 40 * mm))   # shifts later clauses across a page boundary
    footer_left = f"Tarangini Distribution - MSA - {c['chain']}" if tpl == "A" else f"{c['ref']}"

    def footer(cv: canvas.Canvas, doc) -> None:
        cv.setFont("Helvetica", 8)
        cv.drawString(20 * mm, 12 * mm, footer_left)
        cv.drawRightString(190 * mm, 12 * mm, f"Page {doc.page}")

    SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                      bottomMargin=20 * mm, title=f"MSA {c['chain']}").build(story, onFirstPage=footer, onLaterPages=footer)


def make_bol(b: dict, out: Path) -> None:
    cv = canvas.Canvas(str(out), pagesize=A4)
    w, h = A4
    mode = b.get("mode", "ok")
    if mode == "blank":   # a scan with no text layer: shapes only
        cv.setStrokeColor(colors.grey)
        for i in range(14):
            cv.rect(20 * mm, h - (40 + i * 14) * mm, 170 * mm, 9 * mm)
        cv.showPage()
        cv.save()
        return
    rng = random.Random(b["bol_no"])
    lines = [
        ("BOL No", b["bol_no"]), ("Shipment", b["shipment_id"]), ("Carrier", b["carrier"]), ("Vehicle", b["vehicle"]),
        ("Driver", b["driver"]), ("Driver phone", b["phone"]), ("Consignee", b["consignee"]),
        ("Cases", str(b["cases"])), ("Declared value", b["value"]), ("Date", b["date"]),
    ]
    cv.setFont("Helvetica-Bold", 16)
    cv.drawString(20 * mm, h - 25 * mm, "BILL OF LADING" if mode != "garbled" else "B1LL 0F L@D1NG")
    cv.setFont("Helvetica", 11)
    cv.drawString(20 * mm, h - 32 * mm, "Tarangini Distribution Pvt Ltd - outbound consignment note")
    y = h - 50 * mm
    for label, value in lines:
        if mode == "garbled":
            noise = "".join(rng.choice("#@%&*~^|<>?;:xXzZ0O1l") for _ in range(len(label) + 3))
            val = "".join(rng.choice("#@%&*~^|<>?;:xXzZ0O1l") for _ in range(len(value)))
            cv.drawString(20 * mm, y, noise)
            cv.drawString(70 * mm, y, val)
        else:
            cv.setFont("Helvetica-Bold", 11)
            cv.drawString(20 * mm, y, f"{label}:")
            cv.setFont("Helvetica", 11)
            cv.drawString(70 * mm, y, value)
        y -= 9 * mm
    cv.setFont("Helvetica", 8)
    cv.drawString(20 * mm, 15 * mm, "Driver signature: ______________   Receiver signature: ______________")
    cv.showPage()
    cv.save()


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(spec["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    for c in spec.get("contracts", []):
        make_contract(c, out / c["file"])
    for b in spec.get("bols", []):
        make_bol(b, out / b["file"])
    print(json.dumps({"contracts": len(spec.get("contracts", [])), "bols": len(spec.get("bols", []))}))


if __name__ == "__main__":
    main()
