"""Every warranty carries a reference of our own, handed out from its series.

What was typed into the reference until now was, on a vendor's cover, the
vendor's certificate number: it moves to its own field. On a client warranty
a typed number is kept in the notes. Then every warranty is numbered, oldest
first, and the number is made unique so it cannot be reused again.
"""
from datetime import datetime

from django.db import migrations, models


def _next(Scheme, entity):
    scheme = Scheme.objects.get(entity=entity)
    parts = [scheme.prefix]
    if scheme.include_year:
        parts.append(str(datetime.now().year))
    parts.append(str(scheme.next_number).zfill(scheme.padding))
    scheme.next_number += 1
    scheme.save(update_fields=["next_number"])
    return scheme.separator.join(p for p in parts if p)


def number_them(apps, schema_editor):
    Warranty = apps.get_model("warranties", "Warranty")
    Scheme = apps.get_model("setup", "NumberingScheme")
    for w in Warranty.objects.order_by("created_at"):
        typed = (w.reference_number or "").strip()
        if typed:
            if w.warranty_type == "client":
                w.notes = f"{w.notes}\nEarlier reference: {typed}" if w.notes else f"Earlier reference: {typed}"
            else:
                w.vendor_reference = typed
        series = (
            "client_warranty" if w.warranty_type == "client"
            else "component_warranty" if w.component_id else "vendor_warranty"
        )
        w.reference_number = _next(Scheme, series)
        w.save(update_fields=["reference_number", "vendor_reference", "notes"])


class Migration(migrations.Migration):
    dependencies = [
        ("warranties", "0008_customer_warranty_is_its_name"),
        ("setup", "0019_seed_warranty_numbering"),
    ]

    operations = [
        migrations.AddField(
            model_name="warranty",
            name="vendor_reference",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.RunPython(number_them, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="warranty",
            name="reference_number",
            field=models.CharField(blank=True, max_length=200, unique=True),
        ),
    ]
