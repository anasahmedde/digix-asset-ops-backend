"""Move each open job's Handover step to the end of its checklist.

Handover is completed by recording the signed handover, after every other
step; numbered in the middle it blocked the steps after it and the phone and
the desktop numbered the list differently. Handed-over jobs are a record and
are left as they are.
"""
from collections import defaultdict

from django.db import migrations

_PARKING = 1000


def handover_last(apps, schema_editor):
    Step = apps.get_model("sites", "InstallationStep")
    by_job = defaultdict(list)
    open_steps = (
        Step.objects.filter(installation__handover__isnull=True)
        .order_by("installation_id", "step_number", "created_at")
    )
    for step in open_steps:
        by_job[step.installation_id].append(step)
    for steps in by_job.values():
        ordered = [s for s in steps if s.step_type != "handover"] + [s for s in steps if s.step_type == "handover"]
        if ordered == steps and all(s.step_number == i for i, s in enumerate(steps, start=1)):
            continue
        # Two passes: (installation, step_number) is unique.
        for i, s in enumerate(ordered, start=1):
            s.step_number = _PARKING + i
        Step.objects.bulk_update(ordered, ["step_number"])
        for i, s in enumerate(ordered, start=1):
            s.step_number = i
        Step.objects.bulk_update(ordered, ["step_number"])


class Migration(migrations.Migration):
    dependencies = [("sites", "0015_asset_verified")]

    operations = [migrations.RunPython(handover_last, migrations.RunPython.noop)]
