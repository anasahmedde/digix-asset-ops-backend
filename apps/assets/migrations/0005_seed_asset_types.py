from django.db import migrations

# name, has_dimensions (L×W), has_diagonal (inches)
DEFAULT_ASSET_TYPES = [
    ("SMD Screen", True, False),
    ("Digital Display", False, True),
    ("Standee", True, False),
    ("Talker", False, False),
    ("Tokenomo", False, False),
]


def seed(apps, schema_editor):
    AssetType = apps.get_model("assets", "AssetType")
    for name, has_dimensions, has_diagonal in DEFAULT_ASSET_TYPES:
        AssetType.objects.get_or_create(
            name=name,
            defaults={
                "has_dimensions": has_dimensions,
                "has_diagonal": has_diagonal,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    AssetType = apps.get_model("assets", "AssetType")
    AssetType.objects.filter(name__in=[t[0] for t in DEFAULT_ASSET_TYPES]).delete()


class Migration(migrations.Migration):
    dependencies = [("assets", "0004_assettype_device_diagonal_inches_device_display_name_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
