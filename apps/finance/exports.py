"""The Finance tabs as files: Excel, PDF or CSV, holding exactly the rows the
filters on screen show."""
import csv
import datetime
import io
from decimal import Decimal

from django.http import HttpResponse

from common.exports import log_export

FORMATS = ("xlsx", "pdf", "csv")


def col(key, label, kind="text"):
    return {"key": key, "label": label, "kind": kind}


PAYABLE_COLUMNS = [
    col("number", "PO number"), col("party", "Vendor"), col("project", "Project"), col("status_display", "PO status"),
    col("date", "Order date", "date"), col("currency", "Currency"), col("total", "PO value", "money"),
    col("invoiced", "Invoiced", "money"), col("advance", "Advance", "money"), col("first", "1st payment", "money"),
    col("second", "2nd payment", "money"), col("third", "3rd payment", "money"), col("paid", "Paid", "money"),
    col("balance", "Balance pending", "money"), col("pay_status_display", "Payment status"),
    col("due_date", "Due date", "date"), col("overdue_days", "Overdue days", "int"), col("terms", "Terms"),
]

RECEIVABLE_COLUMNS = [
    col("number", "Invoice"), col("party", "Client"), col("quote_number", "Quotation"),
    col("client_po_number", "Client PO"), col("project", "Project"), col("description", "For"),
    col("date", "Issue date", "date"), col("currency", "Currency"), col("total", "Invoice total", "money"),
    col("advance", "Advance", "money"), col("first", "1st payment", "money"), col("second", "2nd payment", "money"),
    col("third", "3rd payment", "money"), col("paid", "Received", "money"), col("balance", "Balance pending", "money"),
    col("pay_status_display", "Payment status"), col("due_date", "Due date", "date"),
    col("overdue_days", "Overdue days", "int"),
]

INVENTORY_COLUMNS = [
    col("group", "Counted as"), col("name", "Item"), col("where", "Where / code"),
    col("quantity", "Quantity", "int"), col("value", "Value at cost", "money"),
]


def flat(rows):
    """Ledger rows ready for a file: projects as names."""
    return [r | {"project": ", ".join(p["name"] for p in r.get("projects", []))} for r in rows]


def inventory_rows(data):
    out = []
    for g in data["groups"]:
        for line in g["lines"]:
            out.append({
                "group": g["label"], "name": line["name"],
                "where": line.get("where") or line.get("asset_code") or "",
                "quantity": line.get("quantity", 1), "value": line["value"],
            })
    return out


def respond(request, fmt, key, title, columns, rows, describe):
    log_export(request.user, f"finance:{key}", len(rows), dict(request.query_params))
    stamp = datetime.date.today().isoformat()
    name = f"{key}-{stamp}"
    if fmt == "csv":
        return _csv(name, columns, rows)
    if fmt == "pdf":
        return _pdf(name, title, columns, rows, describe)
    return _xlsx(name, title, columns, rows, describe, request.user)


def _text(value, kind):
    if value is None or value == "":
        return ""
    if kind == "money":
        return f"{Decimal(value):,.2f}"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%d %b %Y")
    return str(value)


def _csv(name, columns, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    response.write("﻿")  # Excel reads the file as UTF-8
    w = csv.writer(response)
    w.writerow([c["label"] for c in columns])
    for r in rows:
        w.writerow(["" if r.get(c["key"]) is None else r.get(c["key"]) for c in columns])
    return response


def _xlsx(name, title, columns, rows, describe, user):
    from apps.reports.xlsx import ReportBook

    # A money column is summed only when every row is in the same currency.
    one_currency = len({r.get("currency") for r in rows}) <= 1
    cols = [c | ({"total": one_currency} if c["kind"] == "money" else {}) for c in columns]
    book = ReportBook(title, user)
    book.table(title[:31], title, cols, rows, [describe])
    return book.response(name)


def _pdf(name, title, columns, rows, describe):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ink = colors.HexColor("#163144")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A3), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=7.5, leading=9.5)
    head = ParagraphStyle("h", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    right = ParagraphStyle("r", parent=cell, alignment=2)
    data = [[Paragraph(c["label"], head) for c in columns]]
    for r in rows:
        data.append([
            Paragraph(_text(r.get(c["key"]), c["kind"]), right if c["kind"] in ("money", "int") else cell)
            for c in columns
        ])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6F8")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#D5DEE4")),
    ]))
    story = [
        Paragraph(title, ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=14, textColor=ink, leading=18)),
        Paragraph(describe, ParagraphStyle("d", fontName="Helvetica", fontSize=8.5, textColor=colors.grey)),
        Spacer(1, 6),
        table if rows else Paragraph("Nothing matches these filters.", cell),
    ]
    doc.build(story)
    response = HttpResponse(buf.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{name}.pdf"'
    return response
