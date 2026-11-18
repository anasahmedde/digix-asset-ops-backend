"""Render a client invoice to a PDF (reportlab, laid out like the quotation)."""

from __future__ import annotations

import io
from xml.sax.saxutils import escape as esc

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
    company_lines = [esc(company.name)] if company else []
    if company:
        if company.address:
            company_lines.append(esc(company.address))
        contact = " · ".join([esc(p) for p in [company.phone, company.email] if p])
        if contact:
            company_lines.append(contact)
        if company.tax_id:
            company_lines.append(f"Tax ID: {esc(company.tax_id)}")
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
        client_block.append(f"<b>{esc(client.name)}</b>")
        for v in [client.contact_person, client.contact_phone, client.contact_email, client.address]:
            if v:
                client_block.append(esc(v))
    q = invoice.quotation
    meta_right = [
        f"<b>Issue date:</b> {invoice.issue_date}",
        f"<b>Due date:</b> {invoice.due_date}",
        f"<b>Currency:</b> {cur}",
    ]
    if q:
        meta_right.insert(0, f"<b>Quotation:</b> {q.quote_number}")
        if q.client_po_number:
            meta_right.insert(1, f"<b>Your PO:</b> {esc(q.client_po_number)}")
    meta = Table(
        [[Paragraph("BILL TO", label), Paragraph("DETAILS", label)],
         [Paragraph("<br/>".join(client_block) or "—", body), Paragraph("<br/>".join(meta_right), body)]],
        colWidths=[87 * mm, 87 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]))
    elements += [meta, Spacer(1, 10)]

    # The items carried over from the quotation the invoice bills.
    items = list(q.items.all()) if q else []
    if items:
        rows = [["Item", "Qty", f"Unit price ({cur})", f"Line total ({cur})"]] + [
            [Paragraph(esc(i.description), body), str(i.quantity), f"{i.unit_price:,.2f}", f"{i.line_total:,.2f}"]
            for i in items
        ]
        quoted = Table(rows, colWidths=[94 * mm, 16 * mm, 32 * mm, 32 * mm], repeatRows=1)
        quoted.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef2")),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements += [Paragraph("Items quoted", label), Spacer(1, 3), quoted, Spacer(1, 10)]

    what = esc(invoice.description or (f"Against quotation {q.quote_number}" if q else "Services"))
    if q:
        what = f"{what}<br/><font size=7 color='grey'>{q.quote_number} · {esc(q.title)}</font>"
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
        elements += [Spacer(1, 8), Paragraph("Notes", styles["Heading4"]), Paragraph(esc(invoice.notes).replace("\n", "<br/>"), body)]

    from common.signatures import signature_of

    signer = invoice.approved_by or invoice.created_by
    elements += [Spacer(1, 16), signature_of(signer),
                 Paragraph("Authorised signature: ______________________________"
                           + (f"<br/>{signer.get_full_name() or signer.username}" if signer else ""), small)]
    doc.build(elements)
    return buffer.getvalue()
