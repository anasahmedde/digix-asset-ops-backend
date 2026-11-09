# A corrective job born with the asset's own technician, or handed to
# somebody through its visit, carried a technician while its ticket still
# read Open - and Open is not a state the technician can start from. The
# ticket reads Assigned from here on; this brings the ones already like
# that into line.
from django.db import migrations, transaction


def tickets_read_assigned(apps, schema_editor):
    MaintenanceSchedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Ticket = apps.get_model("tickets", "Ticket")
    TicketComment = apps.get_model("tickets", "TicketComment")
    from django.utils import timezone

    jobs = (
        MaintenanceSchedule.objects.filter(
            maintenance_type="corrective", ticket__status="open", is_active=True,
        )
        .exclude(assigned_to=None)
        .exclude(status__in=("completed", "cancelled"))
        .select_related("ticket", "assigned_to")
    )
    for job in jobs:
        # One ticket at a time, so one odd row never holds up the rest.
        try:
            with transaction.atomic():
                ticket = job.ticket
                ticket.status = "assigned"
                if not ticket.assigned_to_id:
                    ticket.assigned_to_id = job.assigned_to_id
                if not ticket.assigned_at:
                    ticket.assigned_at = timezone.now()
                ticket.save(update_fields=["status", "assigned_to", "assigned_at", "updated_at"])
                who = " ".join(filter(None, [job.assigned_to.first_name, job.assigned_to.last_name])) or job.assigned_to.username
                TicketComment.objects.create(
                    ticket=ticket, author=None, comment_type="status_change",
                    old_status="open", new_status="assigned",
                    content=f"Assigned to {who} on the maintenance job; the ticket now says so.",
                )
        except Exception:  # noqa: BLE001
            pass


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0030_visit_cost_kinds"),
        ("tickets", "0021_pending_review_reads_in_review"),
    ]

    operations = [
        migrations.RunPython(tickets_read_assigned, migrations.RunPython.noop),
    ]
