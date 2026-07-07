from django.db import migrations

DEFAULT_CATEGORIES = [
    "Consumables",
    "Spare",
    "PPE",
    "Safety Items",
    "Tools",
    "Stock Items",
]


def seed(apps, schema_editor):
    InventoryCategory = apps.get_model("inventory", "InventoryCategory")
    for name in DEFAULT_CATEGORIES:
        InventoryCategory.objects.get_or_create(name=name, defaults={"is_active": True})


def unseed(apps, schema_editor):
    InventoryCategory = apps.get_model("inventory", "InventoryCategory")
    InventoryCategory.objects.filter(name__in=DEFAULT_CATEGORIES).delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0002_inventorycategory_remove_inventoryitem_site_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
