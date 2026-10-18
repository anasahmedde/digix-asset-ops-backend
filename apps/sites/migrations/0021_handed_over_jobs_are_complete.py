"""A job with a handover on record is complete.

Jobs handed over with every work step skipped and no handover step on the
checklist were never stamped complete, so the field app kept offering the
installer check-in on a finished job. They take the handover's date.
"""
import datetime

from django.db import migrations
from django.utils import timezone


def complete_handed_over(apps, schema_editor):
    Installation = apps.get_model("sites", "DeviceInstallation")
    for inst in Installation.objects.filter(completed_at__isnull=True, handover__isnull=False).select_related("handover"):
        when = timezone.make_aware(datetime.datetime.combine(inst.handover.handover_date, datetime.time(12, 0)))
        inst.completed_at = when
        inst.save(update_fields=["completed_at"])


class Migration(migrations.Migration):

    dependencies = [("sites", "0020_helpers")]

    operations = [migrations.RunPython(complete_handed_over, migrations.RunPython.noop)]
