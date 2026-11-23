"""A Suite quotation's value includes its GST.

Quotations made from Suite before this held the value before GST and a note
("Suite GST 18% (gross 236000.00) is not in the lines; set the tax on the
invoice."). Each gets its GST as a line, so its value matches the client's PO,
and the invoice drafted for the whole of it, while still a draft, follows.
"""
import re
from decimal import Decimal

from django.db import migrations

NOTE = re.compile(r"Suite GST ([\d.]+)% \(gross ([\d.]+)\) is not in the lines; set the tax on the invoice\.")


def add_gst(apps, schema_editor):
    Quotation = apps.get_model("quotations", "Quotation")
    QuotationItem = apps.get_model("quotations", "QuotationItem")
    Invoice = apps.get_model("finance", "Invoice")
    for q in Quotation.objects.filter(suite_record_id__isnull=False):
        m = NOTE.search(q.notes or "")
        if not m:
            continue
        before = q.total_amount
        gst = Decimal(m.group(2)) - before
        q.notes = NOTE.sub("", q.notes).strip()
        if gst > 0:
            QuotationItem.objects.create(quotation=q, description=f"GST {m.group(1)}% (Suite)", quantity=1, unit_price=gst)
            q.total_amount = before + gst
            Invoice.objects.filter(quotation=q, status="draft", amount=before, tax_amount=0).update(
                amount=q.total_amount, total_amount=q.total_amount, description="Full quotation value incl. GST",
            )
        q.save(update_fields=["total_amount", "notes"])


class Migration(migrations.Migration):
    dependencies = [
        ("quotations", "0003_suite_link"),
        ("finance", "0009_invoice_discount_amount"),
    ]

    operations = [migrations.RunPython(add_gst, migrations.RunPython.noop)]
