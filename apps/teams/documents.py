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
        Paragraph("ITEM", s["head"]), Paragraph("QTY", s["headr"]),
        Paragraph("UNIT PRICE", s["headr"]), Paragraph("AMOUNT", s["headr"]),
    ]]
    bands = []
    materials = plan.get("materials", [])
    for asset in plan.get("assets", []):
        bands.append(len(rows))
        rows.append([
            Paragraph(f"<b>{asset['asset_code']}</b>  {asset.get('asset_name', '')}", s["cell"]),
            "", "",
            Paragraph(f"<b>{_money(asset['asset_total'])}</b>", s["num"]),
        ])
        if asset.get("vendor_asset"):
            # Bought complete: one line, priced as the vendor quoted it.
            price = asset.get("asset_price")
            rows.append([
                Paragraph("&nbsp;&nbsp;&nbsp;Complete asset from the vendor"
                          + (f"  <font color='#6b7280'>{asset['supply_vendor_name']}</font>" if asset.get("supply_vendor_name") else ""), s["cell"]),
                Paragraph("1 asset", s["num"]),
                Paragraph(_money(price) if price is not None else "No price on record", s["num"]),
                Paragraph(_money(price) if price is not None else "—", s["num"]),
            ])
            continue
        lines = [m for m in materials if m["asset_code"] == asset["asset_code"]]
        rows.append([Paragraph("Components", s["head"]), "", "",
                     Paragraph(_money(asset["materials_total"]), s["num"])])
        for m in lines:
            rows.append([
                Paragraph(f"&nbsp;&nbsp;&nbsp;{m['name']}", s["cell"]),
                Paragraph(f"{m['quantity']} {m.get('unit') or 'piece'}", s["num"]),
                Paragraph(_money(m["unit_price"]), s["num"]),
                Paragraph(_money(m["line_total"]), s["num"]),
            ])
        rows.append([Paragraph("Production", s["head"]), "", "",
                     Paragraph(_money(asset["production_total"]), s["num"])])
        for step in asset.get("steps", []):
            rows.append([
                Paragraph(f"&nbsp;&nbsp;&nbsp;{step['step_number']}. {step['name']}", s["cell"]),
                "", "",
                Paragraph(_money(step["planned_cost"]), s["num"]),
            ])
    if len(rows) == 1:
        rows.append([Paragraph("No assets on this project yet.", s["cell"]), "", "", ""])
    story.append(_grid(rows, [86, 32, 28, 28], s, bands))
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
        ("Components", plan["materials_total"]),
        ("Production", plan["production_total"]),
        ("Installation & activation", plan.get("installation_total")),
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
        Paragraph("UNIT PRICE", s["headr"]), Paragraph("AMOUNT", s["headr"]),
        Paragraph("USED ON", s["head"]),
    ]]
    for n, line in enumerate(boq.get("lines", []), start=1):
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(line["name"], s["cell"]),
            Paragraph(f"{line['quantity']} {line.get('unit') or 'piece'}", s["num"]),
            Paragraph(_money(line["unit_price"]), s["num"]),
            Paragraph(_money(line["amount"]), s["num"]),
            Paragraph(", ".join(line.get("assets", [])), s["head"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No components on this project yet.", s["cell"]), "", "", "", "", ""])
    story.append(_grid(rows, [8, 52, 28, 26, 26, 34], s))
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


def render_actuals_pdf(project, actuals: dict) -> bytes:
    """Execution actuals: what each asset has really cost so far, the
    overheads planned against actual, and the position against the budget."""
    s = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"Actual Cost — {project.name}")
    story = _masthead("EXECUTION — ACTUAL COST", project, s)

    budget = (actuals.get("budget_status") or "no plan").replace("_", " ").title()
    approved = actuals.get("approved_total")
    variance = actuals.get("variance_vs_approved")
    line = f"<b>Budget:</b> {budget}"
    if approved is not None:
        line += f" · <b>Approved at:</b> {_money(approved)}"
    line += f" · <b>Actual to date:</b> {_money(actuals['actual_total'])}"
    if variance is not None:
        sign = "over" if float(variance) > 0 else "under"
        line += f" · <b>{_money(abs(float(variance)))} {sign}</b>"
    story.append(Paragraph(line, s["body"]))
    story.append(Spacer(1, 4 * mm))

    # ── Assets: materials, production, vendor work ──
    story.append(Paragraph("<b>Assets — materials, production and vendor work</b>", s["section"]))
    story.append(Spacer(1, 2 * mm))
    rows = [[
        Paragraph("ITEM", s["head"]), Paragraph("REQUIRED", s["headr"]), Paragraph("USED", s["headr"]),
        Paragraph("UNIT PRICE", s["headr"]), Paragraph("VALUED AT", s["head"]), Paragraph("ACTUAL", s["headr"]),
    ]]
    bands = []
    for asset in actuals.get("assets", []):
        bands.append(len(rows))
        outstanding = asset.get("outstanding") or 0
        rows.append([
            Paragraph(
                f"<b>{asset['asset_code']}</b>  {asset.get('asset_name', '')}"
                + (f"  <font color='#b45309'>· {outstanding} still to come</font>" if outstanding else ""),
                s["cell"],
            ),
            "", "", "", "",
            Paragraph(f"<b>{_money(asset['actual_total'])}</b>", s["num"]),
        ])
        if asset.get("vendor_asset"):
            paid = float(asset.get("materials_actual") or 0)
            rows.append([
                Paragraph("&nbsp;&nbsp;&nbsp;Complete asset from the vendor", s["cell"]),
                Paragraph("1", s["num"]), Paragraph("1" if paid else "0", s["num"]),
                Paragraph(_money(asset.get("asset_price")), s["num"]),
                Paragraph("Vendor price" if paid else "Not yet received", s["cell"]),
                Paragraph(_money(asset["materials_actual"]) if paid else "—", s["num"]),
            ])
        else:
            rows.append([Paragraph("Components", s["head"]), "", "", "", "",
                         Paragraph(_money(asset["materials_actual"]), s["num"])])
            for m in asset.get("lines", []):
                rows.append([
                    Paragraph(f"&nbsp;&nbsp;&nbsp;{m['name']}", s["cell"]),
                    Paragraph(f"{m['required']} {m.get('unit') or 'piece'}", s["num"]),
                    Paragraph(f"{m['issued']} {m.get('unit') or 'piece'}", s["num"]),
                    Paragraph(_money(m["unit_price"]), s["num"]),
                    Paragraph(m.get("price_source") or "", s["cell"]),
                    Paragraph(_money(m["line_total"]), s["num"]),
                ])
            if asset.get("steps"):
                rows.append([Paragraph("Production", s["head"]), "", "", "", "",
                             Paragraph(_money(asset["production_actual"]), s["num"])])
                for st in asset["steps"]:
                    status = (st.get("status") or "").replace("_", " ")
                    rows.append([
                        Paragraph(f"&nbsp;&nbsp;&nbsp;{st['step_number']}. {st['name']}  <font color='#6b7280'>{status}</font>", s["cell"]),
                        "", "",
                        Paragraph(_money(st.get("planned_cost")), s["num"]),
                        Paragraph(st.get("actual_source") or ("Planned" if st.get("planned_cost") is not None else ""), s["cell"]),
                        Paragraph(_money(st.get("actual_cost")), s["num"]),
                    ])
        if asset.get("work_orders"):
            rows.append([Paragraph("Work orders", s["head"]), "", "", "", "",
                         Paragraph(_money(asset["work_orders_actual"]), s["num"])])
            for w in asset["work_orders"]:
                rows.append([
                    Paragraph(f"&nbsp;&nbsp;&nbsp;{w['wo_number']}  {w.get('supplier') or ''}", s["cell"]),
                    "", "", "",
                    Paragraph((w.get("status") or "").replace("_", " "), s["cell"]),
                    Paragraph(_money(w["amount"]), s["num"]),
                ])
    if len(rows) == 1:
        rows.append([Paragraph("No assets on this project yet.", s["cell"]), "", "", "", "", ""])
    story.append(_grid(rows, [66, 16, 14, 26, 28, 24], s, bands))
    story.append(Spacer(1, 5 * mm))

    # ── Overheads: planned against actual ──
    overheads = actuals.get("overheads", [])
    if overheads:
        story.append(Paragraph("<b>Overheads — planned against actual</b>", s["section"]))
        story.append(Spacer(1, 2 * mm))
        rows = [[Paragraph("DESCRIPTION", s["head"]), Paragraph("PLANNED", s["headr"]),
                 Paragraph("ACTUAL QTY", s["headr"]), Paragraph("ACTUAL RATE", s["headr"]),
                 Paragraph("ACTUAL", s["headr"])]]
        for o in overheads:
            label = o.get("description") or o.get("cost_type") or "—"
            if o.get("unplanned"):
                label += "  <font color='#b45309'>unplanned</font>"
            rows.append([
                Paragraph(label, s["cell"]),
                Paragraph(_money(o["planned_amount"]), s["num"]),
                Paragraph("" if o.get("actual_quantity") is None else f"{float(o['actual_quantity']):g}", s["num"]),
                Paragraph(_money(o.get("actual_unit_cost")), s["num"]),
                Paragraph(_money(o.get("actual_amount")), s["num"]),
            ])
        rows.append([
            Paragraph("<b>Overheads</b>", s["cell"]),
            Paragraph(f"<b>{_money(actuals['overheads_planned_total'])}</b>", s["num"]), "", "",
            Paragraph(f"<b>{_money(actuals['overheads_actual_total'])}</b>", s["num"]),
        ])
        story.append(_grid(rows, [80, 26, 22, 22, 24], s))
        story.append(Spacer(1, 5 * mm))

    # ── Position ──
    totals = [
        ("Components used", actuals["materials_actual"]),
        ("Production", actuals.get("production_actual")),
        ("Installation & activation", actuals.get("installation_actual")),
        ("Vendor work orders", actuals.get("work_orders_actual")),
        ("Overheads (actual)", actuals["overheads_actual_total"]),
    ]
    rows = [[Paragraph(label, s["cell"]), Paragraph(_money(value), s["num"])] for label, value in totals]
    rows.append([Paragraph("<b>ACTUAL TO DATE</b>", s["cell"]),
                 Paragraph(f"<b>{_money(actuals['actual_total'])}</b>", s["num"])])
    if approved is not None:
        rows.append([Paragraph("Approved budget", s["cell"]), Paragraph(_money(approved), s["num"])])
        rows.append([Paragraph("Estimate as it stands", s["cell"]), Paragraph(_money(actuals.get("estimate_total")), s["num"])])
        if variance is not None:
            sign = "over" if float(variance) > 0 else "under"
            rows.append([Paragraph(f"<b>Variance ({sign} the approved budget)</b>", s["cell"]),
                         Paragraph(f"<b>{_money(abs(float(variance)))}</b>", s["num"])])
    total = Table(rows, colWidths=[124 * mm, 50 * mm])
    total.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("LINEABOVE", (0, 4), (-1, 4), 1, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(total)

    doc.build(story)
    return buf.getvalue()
