"""The bill of materials for one asset, as the floor receives it.

What the asset is built from, line by line: how much is needed, how much has
been issued, where each line comes from and where it stands. Printed in the
same house style as the purchase and work orders.
"""
from __future__ import annotations

import io

from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.procurement.documents import BAND, COMPANY_NAME, INK, RULE, _styles


def _company_name() -> str:
    from apps.setup.models import Company

    company = Company.objects.filter(is_primary=True).first() or Company.objects.first()
    return company.name if company else COMPANY_NAME


def _unit_of(component) -> str:
    from apps.teams.costing import component_unit

    return component_unit(component)


def _source_of(component) -> str:
    """Where the line is drawn from — the stock row or unique product it names."""
    if component.inventory_unit_type_id:
        return f"Unique · {component.inventory_unit_type}"
    if component.inventory_item_id:
        return f"Stock · {component.inventory_item.sku}"
    return "Not linked to inventory"


def render_bom_pdf(device) -> bytes:
    """The asset's parts list as an A4 document."""
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Bill of Materials {device.asset_code}",
        author=COMPANY_NAME,
    )
    story: list = []

    masthead = Table(
        [[
            Paragraph("BILL OF MATERIALS", s["title"]),
            Paragraph(f"<b>{_company_name()}</b>", s["company"]),
        ]],
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
    story += [masthead, Spacer(1, 8 * mm)]

    def field(label, value):
        return [Paragraph(label.upper(), s["label"]), Paragraph(str(value or "—"), s["body"])]

    project = device.project
    if project is None:
        from apps.teams.models import ProjectScopeItem

        scope = ProjectScopeItem.objects.filter(device=device).select_related("project").first()
        project = scope.project if scope is not None else None

    facts = Table(
        [
            field("Asset", device.asset_code) + field("Status", device.get_status_display()),
            field("Name", device.display_name or "—") + field("Route", device.get_source_display()),
            field("Project", project.name if project is not None else "—")
            + field("Printed", timezone.localdate().isoformat()),
        ],
        colWidths=[24 * mm, 54 * mm, 26 * mm, 40 * mm],
    )
    facts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [facts, Spacer(1, 6 * mm)]

    rows = [[
        Paragraph("#", s["head"]), Paragraph("COMPONENT", s["head"]),
        Paragraph("REQUIRED", s["head"]), Paragraph("ISSUED", s["head"]),
        Paragraph("OUTSTANDING", s["head"]), Paragraph("WHERE IT STANDS", s["head"]),
    ]]
    components = list(
        device.components.select_related(
            "inventory_item__material_type", "inventory_unit_type",
        ).all()
    )
    for n, c in enumerate(components, start=1):
        unit = _unit_of(c)
        rows.append([
            Paragraph(str(n), s["cell"]),
            Paragraph(
                f"<b>{c.name}</b><br/><font size='7.5' color='#6b7280'>{_source_of(c)}</font>",
                s["cell"],
            ),
            Paragraph(f"{c.quantity} {unit}", s["num"]),
            Paragraph(f"{c.issued_quantity} {unit}", s["num"]),
            Paragraph(f"{c.outstanding_quantity} {unit}", s["num"]),
            Paragraph(c.get_fulfilment_display(), s["cell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("This asset has no components.", s["cell"]), "", "", "", "", ""])

    table = Table(rows, colWidths=[8 * mm, 62 * mm, 25 * mm, 25 * mm, 27 * mm, 27 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (4, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    # The route this parts list feeds, so the floor reads both on one sheet.
    steps = sorted(device.production_steps.all(), key=lambda x: x.step_number)
    if steps:
        story += [Paragraph("<b>Production route</b>", s["section"])]
        route = [[
            Paragraph("#", s["head"]), Paragraph("OPERATION", s["head"]),
            Paragraph("WHERE", s["head"]), Paragraph("STATUS", s["head"]),
        ]]
        for step in steps:
            route.append([
                Paragraph(str(step.step_number), s["cell"]),
                Paragraph(step.name, s["cell"]),
                Paragraph(step.workshop_display or step.get_location_display(), s["cell"]),
                Paragraph(step.get_status_display(), s["cell"]),
            ])
        route_table = Table(route, colWidths=[8 * mm, 78 * mm, 44 * mm, 44 * mm], repeatRows=1)
        route_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, RULE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [route_table]

    doc.build(story)
    return buf.getvalue()
