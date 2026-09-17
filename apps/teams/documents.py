"""Printable project costing: the cost plan and the bill of quantities.

The cost plan is what goes up for approval — read asset by asset, so whoever
signs can see what each thing costs to build. The BOQ is what the buyer works
from: one line per component with the total across every asset on the job.
"""
from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COMPANY_NAME = "DIGIX Asset Ops"

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
BAND = colors.HexColor("#f3f4f6")
SUBBAND = colors.HexColor("#f9fafb")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=18, textColor=INK, alignment=0),
        "company": ParagraphStyle("c", parent=base["Normal"], fontSize=10, textColor=INK, leading=14),
        "label": ParagraphStyle("l", parent=base["Normal"], fontSize=7.5, textColor=MUTED, leading=10),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=9, textColor=INK, leading=13),
        "cell": ParagraphStyle("cl", parent=base["Normal"], fontSize=8.5, textColor=INK, leading=12),
        "num": ParagraphStyle("n", parent=base["Normal"], fontSize=8.5, textColor=INK, leading=12,
                              alignment=TA_RIGHT),
        "head": ParagraphStyle("h", parent=base["Normal"], fontSize=7.5, textColor=MUTED, leading=10),
        "headr": ParagraphStyle("hr", parent=base["Normal"], fontSize=7.5, textColor=MUTED, leading=10,
                                alignment=TA_RIGHT),
        "section": ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, textColor=INK, leading=13),
    }


def _money(value) -> str:
    return "—" if value is None else f"PKR {float(value):,.2f}"


def _masthead(title, project, s, width_left=110):
    head = Table(
        [[Paragraph(title, s["title"]), Paragraph(f"<b>{COMPANY_NAME}</b>", s["company"])]],
        colWidths=[width_left * mm, (174 - width_left) * mm],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, INK),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(str(value or "—"), s["body"])]

    client = project.client.name if project.client_id else "—"
    sites = ", ".join(site.name for site in project.sites.all()) or (
        project.site.name if project.site_id else "—"
    )
    facts = Table(
        [
            field("Project", project.name) + field("Client", client),
            field("Sites", sites) + field("Manager", project.manager.get_full_name() if project.manager_id else "—"),
        ],
        colWidths=[18 * mm, 69 * mm, 18 * mm, 69 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [head, Spacer(1, 6 * mm), facts, Spacer(1, 5 * mm)]


def _doc(buf, title):
    return SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=title, author=COMPANY_NAME,
    )


def _grid(rows, widths, s, bands=()):
    table = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1)
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
    for r in bands:
        style.append(("BACKGROUND", (0, r), (-1, r), SUBBAND))
    table.setStyle(TableStyle(style))
    return table


def render_cost_plan_pdf(project, plan: dict) -> bytes:
    """The estimate as it stands, asset by asset, then the totals."""
    s = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"Cost Plan — {project.name}")
    story = _masthead("COST PLAN", project, s)

    status = plan.get("status_display") or "Draft"
    approved = plan.get("approved_total")
    story.append(Paragraph(
        f"<b>Status:</b> {status}" + (f" · <b>Approved at:</b> {_money(approved)}" if approved else ""),
        s["body"],
    ))
    story.append(Spacer(1, 4 * mm))

    # ── Asset development cost ──
    story.append(Paragraph("<b>Asset Development Cost</b>", s["section"]))
    story.append(Spacer(1, 2 * mm))
    rows = [[
        Paragraph("ITEM", s["head"]), Paragraph("QTY", s["headr"]), Paragraph("UNIT", s["head"]),
        Paragraph("UNIT PRICE", s["headr"]), Paragraph("AMOUNT", s["headr"]),
    ]]
    bands = []
    materials = plan.get("materials", [])
    for asset in plan.get("assets", []):
        bands.append(len(rows))
        rows.append([
            Paragraph(f"<b>{asset['asset_code']}</b>  {asset.get('asset_name', '')}", s["cell"]),
            "", "", "",
            Paragraph(f"<b>{_money(asset['asset_total'])}</b>", s["num"]),
        ])
        lines = [m for m in materials if m["asset_code"] == asset["asset_code"]]
        rows.append([Paragraph("Components", s["head"]), "", "", "",
                     Paragraph(_money(asset["materials_total"]), s["num"])])
        for m in lines:
            rows.append([
                Paragraph(f"&nbsp;&nbsp;&nbsp;{m['name']}", s["cell"]),
                Paragraph(str(m["quantity"]), s["num"]),
                Paragraph(m.get("unit") or "", s["cell"]),
                Paragraph(_money(m["unit_price"]), s["num"]),
                Paragraph(_money(m["line_total"]), s["num"]),
            ])
        rows.append([Paragraph("Production", s["head"]), "", "", "",
                     Paragraph(_money(asset["production_total"]), s["num"])])
        for step in asset.get("steps", []):
            rows.append([
                Paragraph(f"&nbsp;&nbsp;&nbsp;{step['step_number']}. {step['name']}", s["cell"]),
                "", "", "",
                Paragraph(_money(step["planned_cost"]), s["num"]),
            ])
    if len(rows) == 1:
        rows.append([Paragraph("No assets on this project yet.", s["cell"]), "", "", "", ""])
    story.append(_grid(rows, [86, 14, 18, 28, 28], s, bands))
    story.append(Spacer(1, 5 * mm))

    # ── Overheads ──
    overheads = plan.get("overheads", [])
    if overheads:
        story.append(Paragraph("<b>Overheads</b>", s["section"]))
        story.append(Spacer(1, 2 * mm))
        rows = [[Paragraph("DESCRIPTION", s["head"]), Paragraph("QTY", s["headr"]),
                 Paragraph("RATE", s["headr"]), Paragraph("AMOUNT", s["headr"])]]
        for o in overheads:
            rows.append([
                Paragraph(o.get("description") or o.get("cost_type") or "—", s["cell"]),
                Paragraph(f"{float(o['quantity']):g}", s["num"]),
                Paragraph(_money(o["unit_cost"]), s["num"]),
                Paragraph(_money(o["amount"]), s["num"]),
            ])
        story.append(_grid(rows, [100, 18, 28, 28], s))
        story.append(Spacer(1, 5 * mm))

    # ── Totals ──
    totals = [
        ("Materials", plan["materials_total"]),
        ("Production", plan["production_total"]),
        ("Overheads", plan["overheads_total"]),
        (f"Contingency ({float(plan['contingency_percent']):g}% on materials)", plan["contingency_amount"]),
    ]
    rows = [[Paragraph(label, s["cell"]), Paragraph(_money(value), s["num"])] for label, value in totals]
    rows.append([Paragraph("<b>ESTIMATED TOTAL</b>", s["cell"]),
                 Paragraph(f"<b>{_money(plan['total'])}</b>", s["num"])])
    total = Table(rows, colWidths=[124 * mm, 50 * mm])
    total.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("LINEABOVE", (0, -1), (-1, -1), 1, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(total)

    doc.build(story)
    return buf.getvalue()


def render_boq_pdf(project, boq: dict) -> bytes:
    """One line per component, totalled across every asset on the project."""
    s = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"BOQ — {project.name}")
    story = _masthead("BILL OF QUANTITIES", project, s, width_left=120)

    rows = [[
        Paragraph("#", s["head"]), Paragraph("COMPONENT", s["head"]), Paragraph("QTY", s["headr"]),
        Paragraph("UNIT", s["head"]), Paragraph("UNIT PRICE", s["headr"]), Paragraph("AMOUNT", s["headr"]),
        Paragraph("USED ON", s["head"]),
    ]]
    for n, line in enumerate(boq.get("lines", []), start=1):
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(line["name"], s["cell"]),
            Paragraph(str(line["quantity"]), s["num"]),
            Paragraph(line.get("unit") or "", s["cell"]),
            Paragraph(_money(line["unit_price"]), s["num"]),
            Paragraph(_money(line["amount"]), s["num"]),
            Paragraph(", ".join(line.get("assets", [])), s["head"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No components on this project yet.", s["cell"]), "", "", "", "", "", ""])
    story.append(_grid(rows, [8, 52, 14, 14, 26, 26, 34], s))
    story.append(Spacer(1, 4 * mm))

    total = Table(
        [[Paragraph("<b>TOTAL</b>", s["cell"]), Paragraph(f"<b>{_money(boq['total'])}</b>", s["num"])]],
        colWidths=[124 * mm, 50 * mm],
    )
    total.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(total)
    if boq.get("unpriced_lines"):
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            f"{boq['unpriced_lines']} line(s) have no price on record and are not in the total.",
            s["label"],
        ))

    doc.build(story)
    return buf.getvalue()
