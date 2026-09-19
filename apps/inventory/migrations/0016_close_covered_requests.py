"""Open store requests whose requirement was already fully covered from stock
(issued straight to the asset) have nothing left to hand over: close them."""
from django.db import migrations
from django.db.models import F


def close_covered(apps, schema_editor):
    IssuanceRequest = apps.get_model("inventory", "IssuanceRequest")
    AssetComponent = apps.get_model("assets", "AssetComponent")
    covered = AssetComponent.objects.filter(issued_quantity__gte=F("quantity")).values_list("pk", flat=True)
    stale = IssuanceRequest.objects.filter(asset_component_id__in=covered).exclude(status="cancelled").filter(
        quantity_issued__lt=F("quantity_requested")
    )
    for req in stale:
        req.status = "cancelled"
        req.notes = (req.notes + "\n" if req.notes else "") + "Closed — the requirement was already covered from stock."
        req.save(update_fields=["status", "notes"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0015_stockmovement_opening"),
        ("assets", "0026_reopen_copied_route_decisions"),
    ]
    operations = [migrations.RunPython(close_covered, migrations.RunPython.noop)]
