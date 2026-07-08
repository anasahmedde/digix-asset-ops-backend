from django.db import migrations

DEFAULT_CATEGORIES = [
    "Supply",
    "Installation",
    "Fabrication",
    "Electrical",
    "Maintenance",
    "Transport",
]


def seed(apps, schema_editor):
    SupplierServiceCategory = apps.get_model("suppliers", "SupplierServiceCategory")
    for name in DEFAULT_CATEGORIES:
        SupplierServiceCategory.objects.get_or_create(name=name, defaults={"is_active": True})


def unseed(apps, schema_editor):
    SupplierServiceCategory = apps.get_model("suppliers", "SupplierServiceCategory")
    SupplierServiceCategory.objects.filter(name__in=DEFAULT_CATEGORIES).delete()


class Migration(migrations.Migration):
    dependencies = [("suppliers", "0002_supplierservicecategory_alter_supplier_code_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
