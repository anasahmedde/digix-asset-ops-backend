"""The work order as the vendor receives it — laid out like the purchase order.

One house style for every document a supplier or vendor is held to: the same
masthead, facts block, party box, line table, total, terms and signature
lines as the purchase order, so the two read as a pair. The styles, money
format, address block and DRAFT watermark are the purchase order's own.
"""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from apps.procurement.documents import (
    BAND,
    COMPANY_NAME,
    INK,
    RULE,
    UNAPPROVED,
    _address_block,
    _money,
    _styles,
    _watermark,
)

# The house standard for services, printed when an order carries no terms of
# its own and no template.
DEFAULT_TERMS = """1. This work order number must be quoted on all invoices, delivery notes and correspondence.
2. Work is accepted subject to inspection. Anything rejected on inspection is redone at the vendor's cost.
3. The work is to be completed and delivered by the required delivery date stated above.
4. Payment terms are 30 days from receipt of a correct invoice and acceptance of the work.
5. Prices are fixed for the duration of this order and include all applicable taxes and duties unless stated otherwise.
6. The vendor warrants the workmanship for the agreed warranty period.
7. The vendor follows the safety instructions on this order and the site rules wherever the work is done."""


def _company_name() -> str:
    from apps.setup.models import Company

    company = Company.objects.filter(is_primary=True).first() or Company.objects.first()
    return company.name if company else COMPANY_NAME


def _draft_stamp(canvas, doc):
    """The DRAFT stamp across the page until the order is approved."""
    _watermark("DRAFT")(canvas, doc)


def _line_detail(item) -> str:
    """What the line is, beneath its description: the operation and the asset."""
    step = item.production_step
    if step is None:
        return ""
    device = step.device
    asset = device.asset_code + (f" · {device.display_name}" if device.display_name else "")
    return f"Operation {step.step_number} on {asset}"


def build_work_order_pdf(work_order) -> bytes:
    """The order as a single-page (or flowing) A4 document."""
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Work Order {work_order.wo_number}",
        author=COMPANY_NAME,
    )
    currency = work_order.currency
    story: list = []

    # --- Masthead -------------------------------------------------------
    masthead = Table(
        [[
            Paragraph("WORK ORDER", s["title"]),
            Paragraph(f"<b>{_company_name()}</b>", s["company"]),
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

    # --- Order facts and the vendor -------------------------------------
    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(value or "—", s["body"])]

    warranty = f"{work_order.warranty_months} months" if work_order.warranty_months else "—"
    payment = work_order.payment_terms.name if work_order.payment_terms_id else "—"
    facts_rows = [
        field("WO Number", work_order.wo_number) + field("Status", work_order.get_status_display()),
        field("Order Date", work_order.order_date.isoformat() if work_order.order_date else "—")
        + field("Required Delivery",
                work_order.expected_delivery.isoformat() if work_order.expected_delivery else "—"),
        field("Type", work_order.get_order_type_display()) + field("Payment Terms", payment),
    ]
    if work_order.project_id or work_order.warranty_months:
        facts_rows.append(
            field("Project", work_order.project.name if work_order.project_id else "—")
            + field("Warranty", warranty)
        )
    facts = Table(facts_rows, colWidths=[24 * mm, 30 * mm, 26 * mm, 24 * mm])
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    vendor_box = Table(
        [[Paragraph("VENDOR", s["label"])], _address_block(work_order.supplier, s)],
        colWidths=[64 * mm],
    )
    vendor_box.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
    ]))

    header = Table([[facts, vendor_box]], colWidths=[110 * mm, 64 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [header, Spacer(1, 7 * mm)]

    # --- Scope of work ----------------------------------------------------
    story += [Paragraph(f"<b>{work_order.title}</b>", s["section"])]
    if work_order.description:
        story += [Paragraph(work_order.description.replace("\n", "<br/>"), s["terms"])]
    story += [Spacer(1, 4 * mm)]

    # --- What is being ordered ------------------------------------------
    rows = [[
        Paragraph("#", s["head"]), Paragraph("DESCRIPTION", s["head"]),
        Paragraph("QTY", s["head"]), Paragraph("UNIT PRICE", s["head"]),
        Paragraph("AMOUNT", s["head"]),
    ]]
    items = work_order.items.select_related("production_step__device").all()
    for n, item in enumerate(items, start=1):
        detail = _line_detail(item)
        text = f"<b>{item.description}</b>" + (
            f"<br/><font size='7.5' color='#6b7280'>{detail}</font>" if detail else ""
        )
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(text, s["cell"]),
            Paragraph(str(item.quantity), s["num"]),
            Paragraph(_money(item.unit_price, currency), s["num"]),
            Paragraph(_money(item.line_total, currency), s["num"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No lines on this order.", s["cell"]), "", "", "", ""])

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
          Paragraph(f"<b>{_money(work_order.total_amount, currency)}</b>", s["num"])]],
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

    if work_order.notes:
        story += [
            Paragraph("<b>Notes</b>", s["section"]),
            Paragraph(work_order.notes.replace("\n", "<br/>"), s["terms"]),
            Spacer(1, 5 * mm),
        ]

    terms = (
        work_order.terms_conditions
        or (work_order.terms_template.body if work_order.terms_template_id else "")
        or DEFAULT_TERMS
    )
    story += [KeepTogether([
        Paragraph("<b>Terms &amp; Conditions</b>", s["section"]),
        Paragraph(terms.replace("\n", "<br/>"), s["terms"]),
    ])]
    if work_order.safety_instructions:
        story += [Spacer(1, 5 * mm), KeepTogether([
            Paragraph("<b>Safety Instructions</b>", s["section"]),
            Paragraph(work_order.safety_instructions.replace("\n", "<br/>"), s["terms"]),
        ])]

    # --- Signatures ------------------------------------------------------
    story += [Spacer(1, 14 * mm)]
    prepared_by = work_order.created_by
    approved_by = work_order.approved_by
    sign = Table(
        [
            [Paragraph("", s["body"]), Paragraph("", s["body"])],
            [Paragraph("Prepared by", s["label"]), Paragraph("Approved by", s["label"])],
            [
                Paragraph(prepared_by.get_full_name() or prepared_by.username if prepared_by else "", s["body"]),
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

    if work_order.status in UNAPPROVED:
        doc.build(story, onFirstPage=_draft_stamp, onLaterPages=_draft_stamp)
    else:
        doc.build(story)
    return buf.getvalue()
