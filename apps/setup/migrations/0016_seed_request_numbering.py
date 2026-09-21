"""Material requests (MR) and purchase requisitions (PR) are numbered in
sequence like every other document."""
from django.db import migrations

SCHEMES = [("material_request", "MR"), ("purchase_requisition", "PR")]


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    for entity, prefix in SCHEMES:
        NumberingScheme.objects.get_or_create(
            entity=entity,
            defaults={"prefix": prefix, "separator": "-", "include_year": True, "padding": 5, "next_number": 1, "is_active": True},
        )


def unseed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.filter(entity__in=[e for e, _ in SCHEMES]).delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0015_request_numbering_entities")]
    operations = [migrations.RunPython(seed, unseed)]
