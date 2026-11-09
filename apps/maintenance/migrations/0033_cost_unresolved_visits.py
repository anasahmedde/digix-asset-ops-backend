# Visits marked unresolved before their cost was recorded: file the partial
# record now, priced from what is on file (parts used and what the
# technician paid). Man-hours need a rate nobody gave, so they are left out.
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)


def cost_unresolved_visits(apps, schema_editor):
    # ponytail: the live models and pricing, once; this runs right after the
    # schema it needs, and a copy of the pricing rules here would drift.
    from apps.maintenance.lifecycle import _file_record, price_the_visit
    from apps.maintenance.models import MaintenanceRecord, MaintenanceVisit

    for visit in MaintenanceVisit.objects.filter(review_decision="unresolved", record__isnull=True).select_related("schedule"):
        # Best effort, one visit at a time: a visit that cannot be priced
        # here is priced from its page (Record cost), and never holds up
        # the migrations after this one.
        try:
            with transaction.atomic():
                record = _file_record(visit.schedule, visit, visit.reviewed_by, status=MaintenanceRecord.Status.PARTIAL)
                price_the_visit(record, visit)
        except Exception:  # noqa: BLE001
            logger.exception("Could not price unresolved visit %s", visit.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0032_labour_cost_lines"),
    ]

    operations = [
        migrations.RunPython(cost_unresolved_visits, migrations.RunPython.noop),
    ]
