"""Order journals that said "GRN GRN-2026-00055" now say it once.

The receipt number already begins with GRN, and the stamp put the word in
front of it as well. Entries written since the fix are right; these are
the ones written before it.
"""

from django.db import migrations


def say_it_once(apps, schema_editor):
    PurchaseOrder = apps.get_model("procurement", "PurchaseOrder")
    for order in PurchaseOrder.objects.filter(notes__contains="[GRN GRN-"):
        order.notes = order.notes.replace("[GRN GRN-", "[GRN-")
        order.save(update_fields=["notes"])


def leave_it(apps, schema_editor):
    """Undo: nothing to put back — a doubled word was never information."""


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0013_purchaseorder_payment_terms"),
    ]

    operations = [
        migrations.RunPython(say_it_once, leave_it),
    ]
