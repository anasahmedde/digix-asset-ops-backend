"""Approved orders carry the date they were approved. Orders approved before
this rule take the date from their approval note, else the day they were last
touched."""
import re

from django.db import migrations

STAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2})[^\]]*\][^\n]*(?:→|->)\s*Approved")


def backfill(apps, schema_editor):
    PurchaseOrder = apps.get_model("procurement", "PurchaseOrder")
    for po in PurchaseOrder.objects.filter(order_date__isnull=True).exclude(status__in=("draft", "pending_approval", "cancelled")):
        m = STAMP.search(po.notes or "")
        po.order_date = m.group(1) if m else po.updated_at.date()
        po.save(update_fields=["order_date"])


class Migration(migrations.Migration):
    dependencies = [("procurement", "0008_purchaseorder_terms")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
