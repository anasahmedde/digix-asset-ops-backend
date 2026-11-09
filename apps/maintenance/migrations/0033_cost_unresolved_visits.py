# Visits marked unresolved before their cost was recorded: file the partial
# record now, priced from what is on file (parts used and what the
# technician paid). Man-hours need a rate nobody gave, so they are left out.
from django.db import migrations


def cost_unresolved_visits(apps, schema_editor):
    # ponytail: the live models and pricing, once; this runs right after the
    # schema it needs, and a copy of the pricing rules here would drift.
    from apps.maintenance.lifecycle import _file_record, price_the_visit
    from apps.maintenance.models import MaintenanceRecord, MaintenanceVisit

    for visit in MaintenanceVisit.objects.filter(review_decision="unresolved", record__isnull=True).select_related("schedule"):
        record = _file_record(visit.schedule, visit, visit.reviewed_by, status=MaintenanceRecord.Status.PARTIAL)
        price_the_visit(record, visit)


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0032_labour_cost_lines"),
    ]

    operations = [
        migrations.RunPython(cost_unresolved_visits, migrations.RunPython.noop),
    ]
