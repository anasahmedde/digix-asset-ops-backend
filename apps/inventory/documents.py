"""The store's paper: the issue slip for material leaving the store, and
the goods received note for material arriving.

The issue slip: the paper that goes with material leaving the store.

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

from django.utils import timezone

from common.branding import letterhead

COMPANY_NAME = "DIGIX Asset Management"

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


def bought_for(po_item) -> list[tuple[str, str]]:
    """What a received line was bought for, as (kind, words): a project
    (through a requirement, a complete asset, a BOM line, or a material
    request the store reordered for), a maintenance job, a material request's
    own purpose - or, with none of those, stock replenishment."""
    if po_item is None:
        return [("stock", "Stock replenishment")]
    out = []

    def project_of_request(ir):
        if ir.project_id:
            return ir.project
        device = (ir.asset_component.device if ir.asset_component_id
                  else ir.maintenance_schedule.device if ir.maintenance_schedule_id else None)
        return device.project_on if device is not None else None

    for component in po_item.asset_components.select_related("device"):
        project = component.device.project_on if component.device_id else None
        if project is not None:
            out.append(("project", project.name))
    for device in po_item.procured_devices.all():
        if device.project_on is not None:
            out.append(("project", device.project_on.name))
    if po_item.bom_line_id:
        out.append(("project", po_item.bom_line.project.name))
    for reorder in po_item.reorder_requests.select_related("issuance_request"):
        ir = reorder.issuance_request
        if ir is None:
            out.append(("stock", "Stock replenishment"))
            continue
        project = project_of_request(ir)
        if project is not None:
            out.append(("project", project.name))
        elif ir.maintenance_schedule_id:
            job = ir.maintenance_schedule
            asset = job.device.asset_code if job.device_id else ""
            out.append(("maintenance", f"Maintenance: {job.title}" + (f" · {asset}" if asset else "")))
        else:
            out.append(("request", f"{ir.request_number}: {ir.purpose}" if ir.purpose else ir.request_number))
    return list(dict.fromkeys(out)) or [("stock", "Stock replenishment")]


def sent_to(po_item) -> list[str]:
    """Where a received line goes: the projects it was bought for; anything
    else goes into the store, "Inventory"."""
    found = bought_for(po_item)
    names = [words for kind, words in found if kind == "project"]
    return names + (["Inventory"] if len(names) < len(found) else [])


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
    story: list = [letterhead()]

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


def _table(rows, widths):
    t = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    t.setStyle(TableStyle(style))
    return t


def _stamp(user, at):
    when = f"{timezone.localtime(at):%d %b %Y, %H:%M}"
    return f"{_who(user)}<br/>{when}" if user is not None else when


def render_goods_receipt_pdf(receipt) -> bytes:
    """The goods received note: what arrived, against which order, who
    received it, and what inspection and the store made of each line."""
    from apps.procurement.lines import describe_item

    from .serializers import GoodsReceiptLineSerializer

    s = _styles()
    small = ParagraphStyle("grn_small", parent=s["cell"], fontSize=7.5, textColor=MUTED, leading=10)
    num = ParagraphStyle("grn_num", parent=s["cell"], alignment=2)
    num_head = ParagraphStyle("grn_num_head", parent=s["head"], alignment=2)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Goods Received Note {receipt.grn_number}",
        author=COMPANY_NAME,
    )
    story: list = [letterhead()]
    masthead = Table(
        [[Paragraph("GOODS RECEIVED NOTE", s["title"]), Paragraph(f"<b>{COMPANY_NAME}</b>", s["company"])]],
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

    po = receipt.purchase_order
    supplier = po.supplier if po is not None and po.supplier_id else None
    when = timezone.localtime(receipt.created_at)
    facts = Table(
        [
            field("GRN No.", receipt.grn_number) + field("Received On", when.strftime("%d %b %Y, %H:%M")),
            field("Received By", _who(receipt.received_by)) + field("Source", receipt.get_source_display()),
            field("Purchase Order", po.po_number if po else "—") + field("Supplier", supplier.name if supplier else "—"),
            field("Order Date", po.order_date.strftime("%d %b %Y") if po and po.order_date else "—")
            + field("Delivery Ref.", receipt.reference),
        ],
        colWidths=[30 * mm, 57 * mm, 30 * mm, 57 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [facts, Spacer(1, 4 * mm)]

    lines = list(receipt.lines.select_related(
        "po_item", "inventory_item__material_type", "inspected_by", "stocked_by",
    ).all())

    # --- What arrived ----------------------------------------------------
    rows = [[Paragraph(h, s["head"]) for h in ("#", "DESCRIPTION", "UOM")]
            + [Paragraph(h, num_head) for h in ("ORDERED", "RECEIVED", "ACCEPTED", "REJECTED")]]
    for n, line in enumerate(lines, 1):
        d = GoodsReceiptLineSerializer(line).data
        if line.po_item_id:
            title, detail = describe_item(line.po_item)
        else:
            title, detail = d.get("known_component") or d.get("inventory_item_name") or "Item", ""
        extra = []
        if line.batch_number:
            extra.append(f"Batch {line.batch_number}")
        if line.serial_numbers:
            extra.append("S/N " + ", ".join(line.serial_numbers))
        cell = [Paragraph(f"<b>{title}</b>", s["cell"])]
        if detail:
            cell.append(Paragraph(detail, small))
        if extra:
            cell.append(Paragraph(" · ".join(extra), small))
        accepted = line.accepted_quantity if line.accepted_quantity is not None else "—"
        rows.append([
            Paragraph(str(n), s["cell"]), cell, Paragraph(d.get("unit") or "piece", s["cell"]),
            Paragraph(str(line.po_item.quantity) if line.po_item_id else "—", num),
            Paragraph(str(line.quantity), num),
            Paragraph(str(accepted), num),
            Paragraph(str(line.rejected_quantity or 0), num),
        ])
    story += [_table(rows, [7 * mm, 66 * mm, 15 * mm, 21 * mm, 22 * mm, 22 * mm, 21 * mm]),
              Spacer(1, 5 * mm)]

    # Goods coming back (from a project, from maintenance) were bought for
    # nothing new: they return to the shelf, and say where they came from.
    returned = receipt.source != receipt.Source.PURCHASE

    def line_for(line):
        if line.po_item_id or not returned:
            return bought_for(line.po_item)
        return [("return", receipt.get_source_display())]

    # --- What inspection and the store made of it ------------------------
    story += [Paragraph("<b>Inspection and stocking</b>", s["body"]), Spacer(1, 2 * mm)]
    rows = [[Paragraph(h, s["head"]) for h in ("#", "INSPECTION", "INSPECTED BY", "STOCKED BY", "FOR")]]
    for n, line in enumerate(lines, 1):
        status = line.get_inspection_status_display()
        if line.routed_to:
            status += f"<br/>{line.get_routed_to_display()}"
        if line.warranty_months:
            status += f"<br/>Vendor warranty {line.warranty_months} months"
        # What it was bought for - a project, a maintenance job, the shelf -
        # then why inspection said what it said.
        notes = [("Project: " + words) if kind == "project" else words for kind, words in line_for(line)]
        notes += [x for x in (line.inspection_notes,) if x]
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(status, s["cell"]),
            Paragraph(_stamp(line.inspected_by, line.inspected_at) if line.inspected_at else "—", s["cell"]),
            Paragraph(_stamp(line.stocked_by, line.stocked_at) if line.stocked_at else "Not yet in stock", s["cell"]),
            Paragraph("<br/>".join(notes) or "—", s["cell"]),
        ])
    story += [_table(rows, [8 * mm, 40 * mm, 38 * mm, 38 * mm, 50 * mm]), Spacer(1, 5 * mm)]

    if receipt.notes:
        remarks = receipt.notes.replace(chr(10), "<br/>")
        story += [Paragraph(f"<b>Remarks</b><br/>{remarks}", s["cell"]), Spacer(1, 4 * mm)]

    # --- Signatures --------------------------------------------------------
    inspectors = ", ".join(dict.fromkeys(_who(x.inspected_by) for x in lines if x.inspected_by_id))
    # Where the delivery goes: the project(s) it was bought for, or the store.
    destinations = ", ".join(dict.fromkeys(
        name for x in lines
        for name in (sent_to(x.po_item) if x.po_item_id or not returned else ["Inventory"])
    ))
    story += [Spacer(1, 10 * mm)]
    sign = Table(
        [
            [Paragraph("", s["body"]) for _ in range(4)],
            [Paragraph(x, s["label"]) for x in ("Delivered by (supplier)", "Received by", "Inspected by", "Sent to")],
            [Paragraph(supplier.name if supplier else "", s["body"]), Paragraph(_who(receipt.received_by), s["body"]),
             Paragraph(inspectors, s["body"]), Paragraph(destinations, s["body"])],
        ],
        colWidths=[43.5 * mm] * 4,
    )
    sign.setStyle(TableStyle(
        [("LINEBELOW", (i, 0), (i, 0), 0.5, INK) for i in range(4)]
        + [("TOPPADDING", (0, 0), (-1, 0), 12), ("LEFTPADDING", (0, 0), (-1, -1), 0),
           ("RIGHTPADDING", (0, 0), (-1, -1), 8)]
    ))
    story += [sign]
    doc.build(story)
    return buf.getvalue()
