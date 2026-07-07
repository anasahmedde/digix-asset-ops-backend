"""Render a Work Order to a PDF for the supplier (reportlab)."""

from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _company():
    from apps.setup.models import Company

    return Company.objects.filter(is_primary=True).first() or Company.objects.first()


def build_work_order_pdf(work_order) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Work Order {work_order.wo_number}",
    )
    styles = getSampleStyleSheet()
    h_style = ParagraphStyle("h", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    label = ParagraphStyle("l", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9)

    elements = []
    company = _company()

    # ── Header: company + work-order number ──────────────────────────
    company_name = company.name if company else "DIGIX Asset Ops"
    company_lines = [company_name]
    if company:
        if company.address:
            company_lines.append(company.address)
        contact = " · ".join([p for p in [company.phone, company.email] if p])
        if contact:
            company_lines.append(contact)
        if company.tax_id:
            company_lines.append(f"Tax ID: {company.tax_id}")

    header = Table(
        [[
            Paragraph("<br/>".join(company_lines), body),
            Paragraph(f"<b>WORK ORDER</b><br/>{work_order.wo_number}", h_style),
        ]],
        colWidths=[95 * mm, 79 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    elements += [header, Spacer(1, 8)]

    # ── Meta grid: supplier / dates / terms ──────────────────────────
    supplier = work_order.supplier
    supplier_block = [f"<b>{supplier.name}</b>"]
    if supplier.code:
        supplier_block.append(f"Code: {supplier.code}")
    if supplier.contact_person:
        supplier_block.append(supplier.contact_person)
    for v in [supplier.contact_phone, supplier.contact_email, supplier.address]:
        if v:
            supplier_block.append(v)

    meta_right = [
        f"<b>Type:</b> {work_order.get_order_type_display()}",
        f"<b>Status:</b> {work_order.get_status_display()}",
        f"<b>Order date:</b> {work_order.order_date or '—'}",
        f"<b>Expected delivery:</b> {work_order.expected_delivery or '—'}",
        f"<b>Payment terms:</b> {work_order.payment_terms.name if work_order.payment_terms else '—'}",
        f"<b>Warranty:</b> {str(work_order.warranty_months) + ' months' if work_order.warranty_months else '—'}",
    ]
    meta = Table(
        [[
            Paragraph("SUPPLIER", label),
            Paragraph("DETAILS", label),
        ], [
            Paragraph("<br/>".join(supplier_block), body),
            Paragraph("<br/>".join(meta_right), body),
        ]],
        colWidths=[87 * mm, 87 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]))
    elements += [meta, Spacer(1, 10)]

    elements.append(Paragraph(f"<b>{work_order.title}</b>", styles["Heading3"]))
    if work_order.description:
        elements += [Paragraph(work_order.description, body), Spacer(1, 6)]

    # ── Line items ───────────────────────────────────────────────────
    data = [["#", "Description", "Qty", "Unit Price", "Line Total"]]
    for i, item in enumerate(work_order.items.all(), start=1):
        data.append([
            str(i),
            item.description,
            str(item.quantity),
            f"{item.unit_price:,.2f}",
            f"{item.line_total:,.2f}",
        ])
    data.append(["", "", "", f"Total ({work_order.currency})", f"{work_order.total_amount:,.2f}"])

    items_table = Table(data, colWidths=[10 * mm, 96 * mm, 16 * mm, 26 * mm, 26 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#d1d5db")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#1f2937")),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements += [items_table, Spacer(1, 12)]

    # ── Terms, safety ────────────────────────────────────────────────
    terms = work_order.terms_conditions or (work_order.terms_template.body if work_order.terms_template else "")
    if terms:
        elements += [Paragraph("Terms &amp; Conditions", styles["Heading4"]), Paragraph(terms.replace("\n", "<br/>"), body), Spacer(1, 8)]
    if work_order.safety_instructions:
        elements += [
            Paragraph("Safety Instructions", styles["Heading4"]),
            Paragraph(work_order.safety_instructions.replace("\n", "<br/>"), body),
            Spacer(1, 8),
        ]

    elements += [Spacer(1, 16), Paragraph("Authorised signature: ______________________________", small)]

    doc.build(elements)
    return buffer.getvalue()
