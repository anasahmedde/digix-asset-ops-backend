"""The issue slip: the paper that goes with material leaving the store.

Whoever signs for stock should be able to see exactly what they took, what it
was for, and who handed it over — so the slip carries the serial numbers of
every unit issued rather than just a count.
"""
from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COMPANY_NAME = "DIGIX Asset Ops"

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
BAND = colors.HexColor("#f3f4f6")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("slip_title", parent=base["Title"], fontSize=18,
                                textColor=INK, alignment=0),
        "company": ParagraphStyle("slip_company", parent=base["Normal"], fontSize=10,
                                  textColor=INK, leading=14),
        "label": ParagraphStyle("slip_label", parent=base["Normal"], fontSize=7.5,
                                textColor=MUTED, leading=10),
        "body": ParagraphStyle("slip_body", parent=base["Normal"], fontSize=9,
                               textColor=INK, leading=13),
        "cell": ParagraphStyle("slip_cell", parent=base["Normal"], fontSize=8.5,
                               textColor=INK, leading=12),
        "head": ParagraphStyle("slip_head", parent=base["Normal"], fontSize=7.5,
                               textColor=MUTED, leading=10),
    }


def _who(user):
    if user is None:
        return "—"
    return user.get_full_name() or user.username


def render_issue_slip_pdf(issuance_request) -> bytes:
    """One slip for one request, listing what has actually been handed over."""
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Issue Slip {issuance_request.request_number}",
        author=COMPANY_NAME,
    )
    story: list = []

    masthead = Table(
        [[Paragraph("MATERIAL ISSUE SLIP", s["title"]),
          Paragraph(f"<b>{COMPANY_NAME}</b>", s["company"])]],
        colWidths=[110 * mm, 64 * mm],
    )
    masthead.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, INK),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [masthead, Spacer(1, 7 * mm)]

    # --- What it was issued for -----------------------------------------
    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(str(value or "—"), s["body"])]

    against = "—"
    if issuance_request.asset_component_id:
        component = issuance_request.asset_component
        against = f"{component.device.asset_code} · {component.name}"
    elif issuance_request.maintenance_schedule_id:
        against = f"Maintenance · {issuance_request.maintenance_schedule.title}"
    elif issuance_request.project_id:
        against = issuance_request.project.name

    facts = Table(
        [
            field("Request No.", issuance_request.request_number)
            + field("Date", issuance_request.updated_at.date().isoformat()),
            field("Raised For", issuance_request.get_source_display()) + field("Against", against),
            field("Project", issuance_request.project.name if issuance_request.project_id else "—")
            + field("Purpose", issuance_request.purpose),
        ],
        colWidths=[24 * mm, 63 * mm, 22 * mm, 65 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [facts, Spacer(1, 4 * mm)]

    # --- What went out ---------------------------------------------------
    rows = [[
        Paragraph("ITEM", s["head"]),
        Paragraph("REQUESTED", s["head"]),
        Paragraph("ISSUED", s["head"]),
    ], [
        Paragraph(issuance_request.what, s["cell"]),
        Paragraph(str(issuance_request.quantity_requested), s["cell"]),
        Paragraph(str(issuance_request.quantity_issued), s["cell"]),
    ]]
    items = Table(rows, colWidths=[114 * mm, 30 * mm, 30 * mm])
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [items, Spacer(1, 5 * mm)]

    serials = issuance_request.issued_serials or []
    if serials:
        story += [Paragraph("<b>Serial numbers issued</b>", s["body"]), Spacer(1, 2 * mm)]
        # Three to a row keeps a long list readable on one page.
        grid = [serials[i:i + 3] for i in range(0, len(serials), 3)]
        grid = [[Paragraph(x, s["cell"]) for x in row] + [""] * (3 - len(row)) for row in grid]
        serial_table = Table(grid, colWidths=[58 * mm] * 3)
        serial_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [serial_table, Spacer(1, 5 * mm)]

    if issuance_request.notes:
        story += [
            Paragraph(f"<b>Notes</b><br/>{issuance_request.notes}", s["cell"]),
            Spacer(1, 4 * mm),
        ]

    # --- Who handed it over, who took it --------------------------------
    story += [Spacer(1, 10 * mm)]
    sign = Table(
        [
            [Paragraph("", s["body"]), Paragraph("", s["body"])],
            [Paragraph("Issued by", s["label"]), Paragraph("Received by", s["label"])],
            [
                Paragraph(_who(issuance_request.issued_by), s["body"]),
                Paragraph(issuance_request.received_by or "—", s["body"]),
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

    doc.build(story)
    return buf.getvalue()
