"""Fill in the installation date for assets installed before it was recorded.

The date was only ever written at client handover, so an asset that was
installed but not yet handed over showed an empty Installation Date even
though the tracker knew exactly when the job finished. Going forward the
signal records it; this catches everything already installed.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Device = apps.get_model("assets", "Device")
    DeviceInstallation = apps.get_model("sites", "DeviceInstallation")

    for installation in (
        DeviceInstallation.objects.filter(device__installation_date__isnull=True)
        .select_related("device")
        .order_by("installed_at")
    ):
        device = installation.device
        if device.installation_date is not None:
            continue

        # Same precedence the signal uses: the agreed handover date, else the
        # day the checklist was finished, else the day the job was booked for.
        handover = getattr(installation, "handover", None)
        installed_on = getattr(handover, "handover_date", None)
        if installed_on is None:
            stamp = installation.completed_at or installation.installed_at
            installed_on = stamp.date() if stamp else None

        if installed_on is not None:
            device.installation_date = installed_on
            device.save(update_fields=["installation_date"])


def noop(apps, schema_editor):
    """Nothing to undo: the dates are facts, not a schema change."""


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0010_installationroutetemplate_and_more"),
        ("assets", "0022_assetcomponent_increase_notes_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
