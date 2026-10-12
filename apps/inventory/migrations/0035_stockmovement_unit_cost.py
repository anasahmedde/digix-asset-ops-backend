import re
from decimal import Decimal

from django.db import migrations, models


def backfill(apps, schema_editor):
    """Older movements: the opening note says what was paid, a receipt line
    knows its PO price, and anything else takes the item's rate."""
    StockMovement = apps.get_model("inventory", "StockMovement")
    for m in StockMovement.objects.filter(unit_cost__isnull=True, movement_type__in=["in", "opening", "adjustment"]).select_related(
        "item", "goods_receipt_line__po_item"
    ):
        cost = None
        if m.movement_type == "opening":
            found = re.search(r"at (\d+(?:\.\d+)?) per", m.notes or "")
            cost = Decimal(found.group(1)) if found else None
        if cost is None and m.goods_receipt_line_id and m.goods_receipt_line.po_item_id:
            cost = m.goods_receipt_line.po_item.unit_price
        if cost is None:
            cost = m.item.unit_cost
        if cost is not None:
            m.unit_cost = cost
            m.save(update_fields=["unit_cost"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0034_goodsreceiptline_storage_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockmovement",
            name="unit_cost",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
