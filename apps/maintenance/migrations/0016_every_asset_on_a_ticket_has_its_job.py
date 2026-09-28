from django.db import migrations
from django.utils import timezone

OPEN_STATUSES = ("active", "pending", "in_process", "on_hold", "overdue")
DONE_TICKETS = ("closed", "approved", "rejected")


def a_job_for_every_asset_on_an_open_ticket(apps, schema_editor):
    """Tickets over several assets had a job for the first asset only.

    Each asset is its own repair, so the others get theirs now, and go out of
    service the way the first one did.
    """
    Ticket = apps.get_model("tickets", "Ticket")
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    Device = apps.get_model("assets", "Device")
    today = timezone.localdate()

    for ticket in Ticket.objects.exclude(status__in=DONE_TICKETS).prefetch_related("devices"):
        for device in ticket.devices.all():
            if Schedule.objects.filter(ticket=ticket, device=device).exists():
                continue
            existing = Schedule.objects.filter(
                device=device, maintenance_type="corrective", status__in=OPEN_STATUSES,
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
                device=device,
                site_id=device.current_site_id,
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
            Device.objects.filter(pk=device.pk, status__in=("active", "installed")).update(
                status="under_maintenance"
            )


def leave_them(apps, schema_editor):
    """Nothing to undo: which jobs this opened is not kept apart from the rest."""


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0015_assets_with_open_faults_are_out_of_service"),
        ("tickets", "0018_a_category_is_the_work_needed"),
    ]

    operations = [
        migrations.RunPython(a_job_for_every_asset_on_an_open_ticket, leave_them),
    ]
