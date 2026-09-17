"""The purchase order as the supplier receives it.

A PO is the document the company is held to, so it prints the same way every
time: who is ordering, from whom, what exactly, at what price, and on what
terms. The terms travel on the order itself, so an order agreed on unusual
terms still prints the terms it was agreed on.
"""
from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Printed as the buyer. One place to change it when the letterhead changes.
COMPANY_NAME = "DIGIX Asset Ops"

# The house standard, seeded onto a new order and editable per order.
DEFAULT_TERMS = """1. This purchase order number must be quoted on all invoices, packing notes and correspondence.
2. Goods are received subject to inspection. Anything rejected on inspection is returned at the supplier's cost.
3. Delivery is to be completed by the required delivery date stated above.
4. Payment terms are 30 days from receipt of a correct invoice and acceptance of the goods.
5. Prices are fixed for the duration of this order and include all applicable taxes and duties unless stated otherwise.
6. The supplier warrants the goods against defects in material and workmanship for the agreed warranty period.
7. Partial deliveries are accepted only where agreed in writing beforehand."""

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
BAND = colors.HexColor("#f3f4f6")
WATERMARK = colors.Color(0.55, 0.55, 0.6, alpha=0.16)

# Until the Group Head has approved it, the printout is a draft and says so.
UNAPPROVED = ("draft", "pending_approval")


def _watermark(text):
    """A page callback that stamps a large rotated word across the sheet."""
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 96)
        canvas.setFillColor(WATERMARK)
        width, height = doc.pagesize
        canvas.translate(width / 2, height / 2)
        canvas.rotate(38)
        canvas.drawCentredString(0, -30, text)
        canvas.restoreState()
    return draw


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("po_title", parent=base["Title"], fontSize=20,
                                textColor=INK, spaceAfter=2, alignment=0),
        "company": ParagraphStyle("po_company", parent=base["Normal"], fontSize=11,
                                  textColor=INK, leading=15),
        "label": ParagraphStyle("po_label", parent=base["Normal"], fontSize=7.5,
                                textColor=MUTED, leading=10, spaceAfter=1),
        "body": ParagraphStyle("po_body", parent=base["Normal"], fontSize=9,
                               textColor=INK, leading=13),
        "cell": ParagraphStyle("po_cell", parent=base["Normal"], fontSize=8.5,
                               textColor=INK, leading=12),
        "num": ParagraphStyle("po_num", parent=base["Normal"], fontSize=8.5,
                              textColor=INK, leading=12, alignment=TA_RIGHT),
        "head": ParagraphStyle("po_head", parent=base["Normal"], fontSize=7.5,
                               textColor=MUTED, leading=10),
        "terms": ParagraphStyle("po_terms", parent=base["Normal"], fontSize=8,
                                textColor=INK, leading=12),
        "section": ParagraphStyle("po_section", parent=base["Normal"], fontSize=9,
                                  textColor=INK, leading=12, spaceAfter=4),
    }


def _money(value, currency: str) -> str:
    return f"{currency} {float(value or 0):,.2f}"


def _address_block(supplier, s) -> list:
    """Who the order is being placed with."""
    lines = [f"<b>{supplier.name}</b>"]
    if supplier.contact_person:
        lines.append(supplier.contact_person)
    if supplier.address:
        lines.extend(supplier.address.splitlines())
    contact = " · ".join(x for x in (supplier.contact_phone, supplier.contact_email) if x)
    if contact:
        lines.append(contact)
    return [Paragraph("<br/>".join(lines), s["body"])]


def render_purchase_order_pdf(purchase_order) -> bytes:
    """The order as a single-page (or flowing) A4 document."""
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Purchase Order {purchase_order.po_number}",
        author=COMPANY_NAME,
    )
    currency = purchase_order.currency
    story: list = []

    # --- Masthead -------------------------------------------------------
    masthead = Table(
        [[
            Paragraph("PURCHASE ORDER", s["title"]),
            Paragraph(f"<b>{COMPANY_NAME}</b>", s["company"]),
        ]],
        colWidths=[100 * mm, 74 * mm],
    )
    masthead.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, INK),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [masthead, Spacer(1, 8 * mm)]

    # --- Order facts and the supplier -----------------------------------
    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(value or "—", s["body"])]

    # Four facts in two columns of label + value; each pair gets room enough
    # for a date or a status word, so nothing runs under the supplier box.
    facts = Table(
        [
            field("PO Number", purchase_order.po_number) + field("Status", purchase_order.get_status_display()),
            field("Order Date", purchase_order.order_date.isoformat() if purchase_order.order_date else "—")
            + field("Required Delivery",
                    purchase_order.expected_delivery.isoformat() if purchase_order.expected_delivery else "—"),
        ],
        colWidths=[24 * mm, 30 * mm, 26 * mm, 24 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    supplier_box = Table(
        [[Paragraph("SUPPLIER", s["label"])], _address_block(purchase_order.supplier, s)],
        colWidths=[64 * mm],
    )
    supplier_box.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
    ]))

    header = Table([[facts, supplier_box]], colWidths=[110 * mm, 64 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [header, Spacer(1, 7 * mm)]

    # --- What is being ordered ------------------------------------------
    rows = [[
        Paragraph("#", s["head"]), Paragraph("DESCRIPTION", s["head"]),
        Paragraph("QTY", s["head"]), Paragraph("UNIT PRICE", s["head"]),
        Paragraph("AMOUNT", s["head"]),
    ]]
    from .lines import describe_item

    for n, item in enumerate(purchase_order.items.all(), start=1):
        title, detail = describe_item(item)
        text = f"<b>{title}</b>" + (f"<br/><font size='7.5' color='#6b7280'>{detail}</font>" if detail else "")
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(text, s["cell"]),
            Paragraph(str(item.quantity), s["num"]),
            Paragraph(_money(item.unit_price, currency), s["num"]),
            Paragraph(_money(item.line_total, currency), s["num"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No items on this order.", s["cell"]), "", "", "", ""])

    items_table = Table(rows, colWidths=[9 * mm, 83 * mm, 16 * mm, 33 * mm, 33 * mm], repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [items_table]

    total = Table(
        [[Paragraph("TOTAL", s["head"]),
          Paragraph(f"<b>{_money(purchase_order.total_amount, currency)}</b>", s["num"])]],
        colWidths=[108 * mm, 66 * mm],
    )
    total.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1, INK),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [total, Spacer(1, 6 * mm)]

    if purchase_order.notes:
        story += [
            Paragraph("<b>Notes</b>", s["section"]),
            Paragraph(purchase_order.notes.replace("\n", "<br/>"), s["terms"]),
            Spacer(1, 5 * mm),
        ]

    terms = purchase_order.terms or DEFAULT_TERMS
    story += [KeepTogether([
        Paragraph("<b>Terms &amp; Conditions</b>", s["section"]),
        Paragraph(terms.replace("\n", "<br/>"), s["terms"]),
    ])]

    # --- Signatures ------------------------------------------------------
    story += [Spacer(1, 14 * mm)]
    ordered_by = purchase_order.ordered_by
    approved_by = purchase_order.approved_by
    sign = Table(
        [
            [Paragraph("", s["body"]), Paragraph("", s["body"])],
            [Paragraph("Prepared by", s["label"]), Paragraph("Approved by", s["label"])],
            [
                Paragraph(ordered_by.get_full_name() or ordered_by.username if ordered_by else "", s["body"]),
                Paragraph(approved_by.get_full_name() or approved_by.username if approved_by else "", s["body"]),
            ],
        ],
        colWidths=[80 * mm, 80 * mm],
    )
    sign.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.5, INK),
        ("LINEBELOW", (1, 0), (1, 0), 0.5, INK),
        ("TOPPADDING", (0, 0), (-1, 0), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 14),
    ]))
    story += [sign]

    if purchase_order.status in UNAPPROVED:
        stamp = _watermark("DRAFT")
        doc.build(story, onFirstPage=stamp, onLaterPages=stamp)
    else:
        doc.build(story)
    return buf.getvalue()
