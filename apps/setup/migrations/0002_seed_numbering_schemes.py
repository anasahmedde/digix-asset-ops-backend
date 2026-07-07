from django.db import migrations

# entity, prefix, include_year, padding
DEFAULT_SCHEMES = [
    ("asset", "DGX", True, 5),
    ("supplier", "SUP", False, 4),
    ("client", "CLI", False, 4),
    ("purchase_order", "PO", True, 5),
    ("invoice", "INV", True, 5),
    ("work_order", "WO", True, 5),
    ("project", "PRJ", True, 4),
    ("ticket", "TKT", True, 5),
    ("goods_receipt", "GRN", True, 5),
    ("issuance", "ISS", True, 5),
    ("inventory_item", "ITM", False, 5),
]


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    for entity, prefix, include_year, padding in DEFAULT_SCHEMES:
        NumberingScheme.objects.get_or_create(
            entity=entity,
            defaults={
                "prefix": prefix,
                "separator": "-",
                "include_year": include_year,
                "padding": padding,
                "next_number": 1,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.filter(entity__in=[e[0] for e in DEFAULT_SCHEMES]).delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
