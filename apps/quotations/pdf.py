"""Render a Quotation to a PDF for the client (reportlab, mirrors workorders.pdf)."""

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


def build_quotation_pdf(quotation) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Quotation {quotation.quote_number}",
    )
    styles = getSampleStyleSheet()
    h_style = ParagraphStyle("h", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    label = ParagraphStyle("l", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9)

    elements = []
    company = _company()

    # ── Header: company + quotation number ───────────────────────────
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
            Paragraph(f"<b>QUOTATION</b><br/>{quotation.quote_number}", h_style),
        ]],
        colWidths=[95 * mm, 79 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    elements += [header, Spacer(1, 8)]

    # ── Meta grid: client / details ──────────────────────────────────
    client = quotation.client
    client_block = [f"<b>{client.name}</b>"]
    if client.code:
        client_block.append(f"Code: {client.code}")
    if client.contact_person:
        client_block.append(client.contact_person)
    for v in [client.contact_phone, client.contact_email, client.address]:
        if v:
            client_block.append(v)

    meta_right = [
        f"<b>Status:</b> {quotation.get_status_display()}",
        f"<b>Date:</b> {quotation.created_at.date()}",
        f"<b>Valid until:</b> {quotation.valid_until or '—'}",
        f"<b>Site:</b> {quotation.site.name if quotation.site else '—'}",
        f"<b>Currency:</b> {quotation.currency}",
    ]
    meta = Table(
        [[
            Paragraph("CLIENT", label),
            Paragraph("DETAILS", label),
        ], [
            Paragraph("<br/>".join(client_block), body),
            Paragraph("<br/>".join(meta_right), body),
        ]],
        colWidths=[87 * mm, 87 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]))
    elements += [meta, Spacer(1, 10)]

    elements.append(Paragraph(f"<b>{quotation.title}</b>", styles["Heading3"]))
    if quotation.description:
        elements += [Paragraph(quotation.description, body), Spacer(1, 6)]

    # ── Line items ───────────────────────────────────────────────────
    data = [["#", "Description", "Qty", "Unit Price", "Line Total"]]
    for i, item in enumerate(quotation.items.all(), start=1):
        data.append([
            str(i),
            item.description,
            str(item.quantity),
            f"{item.unit_price:,.2f}",
            f"{item.line_total:,.2f}",
        ])
    data.append(["", "", "", f"Total ({quotation.currency})", f"{quotation.total_amount:,.2f}"])

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

    # ── Notes ────────────────────────────────────────────────────────
    if quotation.notes:
        elements += [
            Paragraph("Notes", styles["Heading4"]),
            Paragraph(quotation.notes.replace("\n", "<br/>"), body),
            Spacer(1, 8),
        ]

    elements += [Spacer(1, 16), Paragraph("Authorised signature: ______________________________", small)]

    doc.build(elements)
    return buffer.getvalue()
