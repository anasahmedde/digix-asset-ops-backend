"""Stock write-offs are numbered SWO-<year>-<n>, like every other document."""
from django.db import migrations


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.get_or_create(
        entity="stock_write_off",
        defaults={"prefix": "SWO", "separator": "-", "include_year": True, "padding": 5,
                  "next_number": 1, "is_active": True},
    )


def unseed(apps, schema_editor):
    apps.get_model("setup", "NumberingScheme").objects.filter(entity="stock_write_off").delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0022_stock_write_off_numbering")]
    operations = [migrations.RunPython(seed, unseed)]
