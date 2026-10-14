"""Put each open job in the name of whoever holds its open visit.

A job read one technician while its open visit had gone to another, so the
first saw it in their job list and could not act on it, and the second did
not see it at all. Closed jobs keep the name they finished under.
"""
from django.db import migrations

OPEN = ("planned", "in_progress", "awaiting_review")


def follow_open_visit(apps, schema_editor):
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    # Faults only: a preventive round given to a stand-in leaves the
    # arrangement with its standing technician.
    for job in Schedule.objects.filter(maintenance_type="corrective").exclude(status__in=("completed", "cancelled")):
        visit = (
            Visit.objects.filter(schedule=job, status__in=OPEN, assigned_to__isnull=False)
            .order_by("-sequence", "-created_at")
            .first()
        )
        if visit and visit.assigned_to_id != job.assigned_to_id:
            job.assigned_to_id = visit.assigned_to_id
            job.save(update_fields=["assigned_to"])


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0023_asset_verified")]

    operations = [migrations.RunPython(follow_open_visit, migrations.RunPython.noop)]
