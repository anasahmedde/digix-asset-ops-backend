from django.db import migrations
from django.utils import timezone

OPEN_STATUSES = ("active", "pending", "in_process", "on_hold", "overdue")
DONE_TICKETS = ("closed", "approved", "rejected")


def open_jobs_for_faults_already_reported(apps, schema_editor):
    """Tickets raised before faults reached the register still get their job.

    Otherwise the asset has a fault open on one screen and nothing on the
    other, which is the state this was meant to end.
    """
    Ticket = apps.get_model("tickets", "Ticket")
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    today = timezone.localdate()

    for ticket in Ticket.objects.exclude(status__in=DONE_TICKETS).exclude(device=None).select_related("device"):
        existing = Schedule.objects.filter(
            device=ticket.device, maintenance_type="corrective", status__in=OPEN_STATUSES,
        ).first()
        if existing is not None:
            if existing.ticket_id is None:
                existing.ticket = ticket
                existing.save(update_fields=["ticket", "updated_at"])
            continue
        job = Schedule.objects.create(
            title=ticket.title[:300],
            priority="high" if ticket.priority in ("high", "critical") else (ticket.priority or "medium"),
            maintenance_type="corrective",
            frequency="one_time",
            device=ticket.device,
            site_id=ticket.device.current_site_id,
            assigned_to_id=ticket.assigned_to_id,
            next_due=ticket.due_date or today,
            start_date=ticket.due_date or today,
            status="in_process",
            instructions=ticket.description or "",
            ticket=ticket,
        )
        Visit.objects.create(
            schedule=job, due_date=job.next_due, assigned_to_id=job.assigned_to_id, status="planned",
        )


def close_them(apps, schema_editor):
    apps.get_model("maintenance", "MaintenanceSchedule").objects.exclude(ticket=None).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0013_a_job_can_name_its_ticket"),
        ("tickets", "0017_predictive_not_preventive"),
    ]

    operations = [
        migrations.RunPython(open_jobs_for_faults_already_reported, close_them),
    ]
