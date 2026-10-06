"""Lines stocked under the old flow are marked as stocked.

Receiving used to be the same act as passing inspection, so everything that
passed is already on the shelf. Without this they would all reappear at the
warehouse door asking to be received a second time.
"""

from django.db import migrations
from django.db.models import F


def mark(apps, schema_editor):
    GoodsReceiptLine = apps.get_model("inventory", "GoodsReceiptLine")
    GoodsReceiptLine.objects.filter(
        inspection_status="passed", stocked_at__isnull=True,
    ).update(stocked_at=F("inspected_at"))


def unmark(apps, schema_editor):
    """Undo: forget the stocking stamp, leaving the verdict as it was."""
    GoodsReceiptLine = apps.get_model("inventory", "GoodsReceiptLine")
    GoodsReceiptLine.objects.filter(inspection_status="passed").update(
        stocked_at=None, stocked_by=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0025_goodsreceiptline_receiving_notes_and_more"),
    ]

    operations = [
        migrations.RunPython(mark, unmark),
    ]
