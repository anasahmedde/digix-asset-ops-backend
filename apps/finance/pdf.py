"""Render a client invoice to a PDF (reportlab, laid out like the quotation)."""

from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from common.branding import letterhead


def build_invoice_pdf(invoice) -> bytes:
    from apps.quotations.pdf import _company

    from .ledger import quotation_billing

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Invoice {invoice.invoice_number}",
    )
    styles = getSampleStyleSheet()
    h_style = ParagraphStyle("h", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    label = ParagraphStyle("l", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9)
    cur = invoice.currency

    elements = [letterhead()]
    company = _company()
    company_lines = [company.name] if company else []
    if company:
        if company.address:
            company_lines.append(company.address)
        contact = " · ".join([p for p in [company.phone, company.email] if p])
        if contact:
            company_lines.append(contact)
        if company.tax_id:
            company_lines.append(f"Tax ID: {company.tax_id}")
    title = "INVOICE" if invoice.status != invoice.Status.DRAFT else "INVOICE (DRAFT)"
    header = Table(
        [[Paragraph("<br/>".join(company_lines), body), Paragraph(f"<b>{title}</b><br/>{invoice.invoice_number}", h_style)]],
        colWidths=[95 * mm, 79 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    elements += [header, Spacer(1, 8)]

    client = invoice.client
    client_block = []
    if client:
        client_block.append(f"<b>{client.name}</b>")
        for v in [client.contact_person, client.contact_phone, client.contact_email, client.address]:
            if v:
                client_block.append(v)
    q = invoice.quotation
    meta_right = [
        f"<b>Issue date:</b> {invoice.issue_date}",
        f"<b>Due date:</b> {invoice.due_date}",
        f"<b>Currency:</b> {cur}",
    ]
    if q:
        meta_right.insert(0, f"<b>Quotation:</b> {q.quote_number}")
    meta = Table(
        [[Paragraph("BILL TO", label), Paragraph("DETAILS", label)],
         [Paragraph("<br/>".join(client_block) or "—", body), Paragraph("<br/>".join(meta_right), body)]],
        colWidths=[87 * mm, 87 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]))
    elements += [meta, Spacer(1, 10)]

    what = invoice.description or (f"Against quotation {q.quote_number}" if q else "Services")
    if q:
        what = f"{what}<br/><font size=7 color='grey'>{q.quote_number} · {q.title}</font>"
    data = [["Description", f"Amount ({cur})"], [Paragraph(what, body), f"{invoice.amount:,.2f}"]]
    if invoice.tax_amount:
        data.append(["Tax", f"{invoice.tax_amount:,.2f}"])
    data.append([f"Total ({cur})", f"{invoice.total_amount:,.2f}"])
    paid = sum((p.amount for p in invoice.payments.all()), 0)
    if paid:
        data += [["Received", f"{paid:,.2f}"], ["Balance due", f"{invoice.total_amount - paid:,.2f}"]]
    table = Table(data, colWidths=[134 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1b405b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, 1), 0.4, colors.HexColor("#d1d5db")),
        ("LINEABOVE", (0, 2), (-1, 2), 0.8, colors.HexColor("#1b405b")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements += [table, Spacer(1, 10)]

    if q:
        b = quotation_billing(q)
        elements.append(Paragraph(
            f"Quotation value {cur} {b['quoted']:,.2f} · invoiced to date {cur} {b['invoiced']:,.2f} · "
            f"left to invoice {cur} {b['to_invoice']:,.2f}", small))
    if invoice.notes:
        elements += [Spacer(1, 8), Paragraph("Notes", styles["Heading4"]), Paragraph(invoice.notes.replace("\n", "<br/>"), body)]

    from common.signatures import signature_of

    signer = invoice.approved_by or invoice.created_by
    elements += [Spacer(1, 16), signature_of(signer),
                 Paragraph("Authorised signature: ______________________________"
                           + (f"<br/>{signer.get_full_name() or signer.username}" if signer else ""), small)]
    doc.build(elements)
    return buffer.getvalue()
