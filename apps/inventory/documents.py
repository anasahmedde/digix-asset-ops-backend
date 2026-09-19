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


def _against(req):
    """What the material was issued against, and what to call that field."""
    if req.asset_component_id:
        component = req.asset_component
        return "Asset / Job", f"{component.device.asset_code} · {component.name}"
    if req.maintenance_schedule_id:
        return "Maintenance Job", req.maintenance_schedule.title
    if req.project_id:
        return "Project", req.project.name
    return "Against", "—"


def _purpose_text(req, against):
    """The purpose in its own words — never a repeat of the job reference."""
    text = (req.purpose or "").strip()
    if against and against != "—" and text.startswith(against):
        text = text[len(against):].strip(" —-·")
    if text:
        return text[0].upper() + text[1:]
    if req.source == "project":
        return "Build requirement" + (f" — {req.project.name}" if req.project_id else "")
    if req.source == "maintenance":
        return "Maintenance job"
    return req.get_source_display()


def _unit_of(req):
    if req.unit_type_id:
        return req.unit_type.unit or "piece"
    if req.item_id and req.item.material_type_id:
        return req.item.material_type.unit or "piece"
    return "piece"


def _code_of(req):
    if req.unit_type_id:
        return req.unit_type.type_code
    return req.item.sku if req.item_id else ""


def _receivers(req):
    """Everyone who took material against this request, in order, once each."""
    names = []
    for h in req.handovers or []:
        name = (h.get("received_by") or "").strip()
        if name and name not in names:
            names.append(name)
    if not names and req.received_by:
        names.append(req.received_by)
    return names


def render_issue_slip_pdf(issuance_request) -> bytes:
    """One slip for one request: what was issued, to whom, against what, and
    who handed it over — the record the store and the receiver both sign."""
    req = issuance_request
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Material Issue Slip {req.request_number}",
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

    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(str(value or "—"), s["body"])]

    against_label, against = _against(req)
    receivers = _receivers(req)
    issued_to = ", ".join(receivers) if receivers else "—"
    when = (req.last_issued_at or req.updated_at)
    requested_by = _who(req.requested_by) if req.requested_by_id else "—"
    requested_on = req.created_at.date().isoformat()

    facts = Table(
        [
            field("Slip No.", req.request_number) + field("Date", when.date().isoformat()),
            field("Issued To", issued_to) + field("Raised For", req.get_source_display()),
            field("Project", req.project.name if req.project_id else "—") + field(against_label, against),
            field("Purpose", _purpose_text(req, against)) + field("Requested By", f"{requested_by} · {requested_on}"),
        ],
        colWidths=[24 * mm, 63 * mm, 24 * mm, 63 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [facts, Spacer(1, 4 * mm)]

    # --- The line: code, description, unit, quantities -------------------
    unit = _unit_of(req)
    head = [Paragraph(h, s["head"]) for h in ("CODE", "DESCRIPTION", "UOM", "REQUESTED", "ISSUED", "BALANCE")]
    line = [
        Paragraph(_code_of(req) or "—", s["cell"]),
        Paragraph(req.unit_type.name if req.unit_type_id else (req.item.material_type.name if req.item_id and req.item.material_type_id else req.what), s["cell"]),
        Paragraph(unit, s["cell"]),
        Paragraph(str(req.quantity_requested), s["cell"]),
        Paragraph(str(req.quantity_issued), s["cell"]),
        Paragraph(str(req.outstanding_quantity), s["cell"]),
    ]
    items = Table([head, line], colWidths=[30 * mm, 60 * mm, 16 * mm, 24 * mm, 20 * mm, 24 * mm])
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [items, Spacer(1, 5 * mm)]

    # --- Each hand-over: when, how much, by whom, to whom, which serials ---
    handovers = req.handovers or []
    if handovers:
        story += [Paragraph("<b>Hand-overs</b>", s["body"]), Spacer(1, 2 * mm)]
        # Serial numbers belong to unique items only; a stock line carries a note.
        serialised = bool(req.unit_type_id)
        last = "SERIAL NOS" if serialised else "NOTE"
        rows = [[Paragraph(h, s["head"]) for h in ("DATE", "QTY", "ISSUED BY", "RECEIVED BY", last)]]
        for h in handovers:
            detail = (", ".join(h.get("serials") or []) or "—") if serialised else (h.get("note") or "—")
            rows.append([
                Paragraph((h.get("at") or "")[:10], s["cell"]),
                Paragraph(f"{h.get('quantity', '')} {unit}", s["cell"]),
                Paragraph(h.get("issued_by") or "—", s["cell"]),
                Paragraph(h.get("received_by") or "—", s["cell"]),
                Paragraph(detail, s["cell"]),
            ])
        hand = Table(rows, colWidths=[22 * mm, 22 * mm, 34 * mm, 34 * mm, 62 * mm])
        hand.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [hand, Spacer(1, 5 * mm)]
    else:
        serials = req.issued_serials or []
        if serials:
            story += [Paragraph("<b>Serial numbers issued</b>", s["body"]), Spacer(1, 2 * mm)]
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

    if req.notes:
        story += [Paragraph(f"<b>Remarks</b><br/>{req.notes.replace(chr(10), '<br/>')}", s["cell"]), Spacer(1, 4 * mm)]

    # --- Signatures: who handed it over, who took it, who authorised -----
    story += [Spacer(1, 10 * mm)]
    sign = Table(
        [
            [Paragraph("", s["body"]), Paragraph("", s["body"]), Paragraph("", s["body"])],
            [Paragraph("Issued by (store)", s["label"]), Paragraph("Received by", s["label"]), Paragraph("Authorised by", s["label"])],
            [Paragraph(_who(req.issued_by), s["body"]), Paragraph(issued_to, s["body"]), Paragraph("", s["body"])],
        ],
        colWidths=[56 * mm, 56 * mm, 56 * mm],
    )
    sign.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.5, INK),
        ("LINEBELOW", (1, 0), (1, 0), 0.5, INK),
        ("LINEBELOW", (2, 0), (2, 0), 0.5, INK),
        ("TOPPADDING", (0, 0), (-1, 0), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [sign]

    doc.build(story)
    return buf.getvalue()
