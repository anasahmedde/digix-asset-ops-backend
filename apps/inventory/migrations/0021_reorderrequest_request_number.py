"""Reorder requests get a purchase requisition number (PR-YYYY-00001). Rows
that exist already are numbered first, then the column becomes unique."""
from datetime import datetime

from django.db import migrations, models


def number_existing(apps, schema_editor):
    ReorderRequest = apps.get_model("inventory", "ReorderRequest")
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    scheme = NumberingScheme.objects.filter(entity="purchase_requisition").first()
    n = scheme.next_number if scheme else 1
    year = datetime.now().year
    for req in ReorderRequest.objects.order_by("created_at"):
        req.request_number = f"PR-{year}-{str(n).zfill(5)}"
        req.save(update_fields=["request_number"])
        n += 1
    if scheme:
        scheme.next_number = n
        scheme.save(update_fields=["next_number"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0020_backfill_handovers"), ("setup", "0016_seed_request_numbering")]
    operations = [
        # Added plain first: the unique index (with its _like twin on
        # PostgreSQL) is created once, by the AlterField below.
        migrations.AddField(
            model_name="reorderrequest", name="request_number",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.RunPython(number_existing, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="reorderrequest", name="request_number",
            field=models.CharField(blank=True, db_index=True, max_length=50, unique=True),
        ),
    ]
