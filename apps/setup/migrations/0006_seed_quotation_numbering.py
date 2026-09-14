from django.db import migrations


def seed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.get_or_create(
        entity="quotation",
        defaults={
            "prefix": "QT",
            "separator": "-",
            "include_year": True,
            "padding": 5,
            "next_number": 1,
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    NumberingScheme = apps.get_model("setup", "NumberingScheme")
    NumberingScheme.objects.filter(entity="quotation").delete()


class Migration(migrations.Migration):
    dependencies = [("setup", "0005_alter_numberingscheme_entity")]
    operations = [migrations.RunPython(seed, unseed)]
