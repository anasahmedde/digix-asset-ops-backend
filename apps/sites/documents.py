"""The handover document — what the client signs to accept an asset.

Printed from the Installation Tracker before the visit, taken to site, signed,
and uploaded back against the handover record. It states what is being handed
over, where it is, what was done to it, and leaves the acceptance to be filled
in by hand, because that is the part the client is agreeing to.

The layout is the one the purchase and work orders use, so everything the
company sends out reads as coming from the same place.
"""
from __future__ import annotations

import io

from reportlab.lib import colors
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

from apps.procurement.documents import BAND, COMPANY_NAME, RULE, _styles

# A line somebody writes on. Long enough to sign across.
_RULE_LINE = colors.HexColor("#9ca3af")


def _pairs_table(rows, s, widths=(38 * mm, 62 * mm)):
    """A label-above-value block, two columns of pairs across the page."""
    data = []
    for left, right in rows:
        data.append([
            Paragraph(left[0], s["label"]),
            Paragraph(left[1] or "—", s["body"]),
            Paragraph(right[0] if right else "", s["label"]),
            Paragraph((right[1] if right else "") or ("" if not right else "—"), s["body"]),
        ])
    table = Table(data, colWidths=[widths[0], widths[1], widths[0], widths[1]], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _signing_row(label, s, width):
    """A labelled line for someone to write on."""
    cell = Table([[""]], colWidths=[width], rowHeights=[13 * mm])
    cell.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, _RULE_LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [cell, Paragraph(label, s["label"])]


def render_handover_pdf(installation) -> bytes:
    """The handover document for one installation, ready to be signed."""
    s = _styles()
    device = installation.device
    site = installation.site
    record = getattr(installation, "handover", None)
    client = (
        record.client if record is not None
        else device.assigned_client or (site.client if site.client_id else None)
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Handover — {device.asset_code}",
    )
    body = []

    # ── Letterhead ──
    head = Table(
        [[
            Paragraph(f"<b>{COMPANY_NAME}</b>", s["company"]),
            Paragraph("HANDOVER<br/>CERTIFICATE", s["title"]),
        ]],
        colWidths=[105 * mm, 69 * mm], hAlign="LEFT",
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    body.append(head)
    body.append(Spacer(1, 4 * mm))
    rule = Table([[""]], colWidths=[174 * mm])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, RULE)]))
    body.append(rule)
    body.append(Spacer(1, 6 * mm))

    # ── What is being handed over ──
    body.append(Paragraph("<b>The asset</b>", s["section"]))
    body.append(_pairs_table([
        (("Asset ID", device.asset_code), ("Description", device.display_name or "—")),
        (("Type", device.asset_type.name if device.asset_type_id else "—"),
         ("Make / model", str(device.device_model) if device.device_model_id else "—")),
        (("Site", site.name), ("Location", site.city or installation.position_label or "—")),
        (("Client", client.name if client is not None else "—"),
         # An asset reaches a project by its own link or a Scope row; ask it.
         ("Project", project.name if (project := device.project_on) else "—")),
    ], s))
    body.append(Spacer(1, 5 * mm))

    # ── What was done ──
    steps = list(installation.steps.all().order_by("step_number"))
    if steps:
        body.append(Paragraph("<b>Work completed</b>", s["section"]))
        rows = [[
            Paragraph("#", s["head"]),
            Paragraph("Step", s["head"]),
            Paragraph("Status", s["head"]),
            Paragraph("Completed", s["head"]),
        ]]
        for step in steps:
            done = getattr(step, "completed_at", None)
            rows.append([
                Paragraph(str(step.step_number), s["cell"]),
                Paragraph(step.custom_label or step.get_step_type_display(), s["cell"]),
                Paragraph(step.get_status_display(), s["cell"]),
                Paragraph(done.strftime("%d %b %Y") if done else "—", s["cell"]),
            ])
        table = Table(rows, colWidths=[12 * mm, 84 * mm, 39 * mm, 39 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.3, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        body.append(table)
        body.append(Spacer(1, 5 * mm))

    # ── The acceptance ──
    declaration = (
        "The client confirms that the asset described above has been installed at the stated "
        "site, demonstrated, and accepted in working order. Any outstanding items are listed "
        "under Notes below and do not affect this acceptance unless stated."
    )
    accept = [Paragraph("<b>Acceptance</b>", s["section"]), Paragraph(declaration, s["terms"])]
    accept.append(Spacer(1, 6 * mm))

    if record is not None:
        accept.append(_pairs_table([
            (("Accepted by", record.accepted_by_name),
             ("Handover date", record.handover_date.strftime("%d %b %Y"))),
            (("Recorded by",
              (record.performed_by.get_full_name() or record.performed_by.username)
              if record.performed_by_id else "—"),
             ("Notes", record.acceptance_notes or "—")),
        ], s))
    else:
        name_cell, name_label = _signing_row("Name and position", s, 84 * mm)
        sign_cell, sign_label = _signing_row("Signature", s, 84 * mm)
        date_cell, date_label = _signing_row("Date", s, 84 * mm)
        notes_cell, notes_label = _signing_row("Notes / outstanding items", s, 84 * mm)
        grid = Table(
            [
                [name_cell, sign_cell],
                [name_label, sign_label],
                [date_cell, notes_cell],
                [date_label, notes_label],
            ],
            colWidths=[87 * mm, 87 * mm], hAlign="LEFT",
        )
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, -1), 6 * mm),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 8 * mm),
        ]))
        accept.append(grid)

    body.append(KeepTogether(accept))
    body.append(Spacer(1, 8 * mm))
    body.append(Paragraph(
        f"{COMPANY_NAME} · {device.asset_code} · this document is the record of acceptance "
        "and is filed against the installation.",
        s["label"],
    ))

    doc.build(body)
    return buffer.getvalue()
