"""Put every existing corrective job onto the lifecycle, as visits.

Before this, a corrective job and its ticket each kept their own status and
their own assignee, and nothing reconciled them — a job was born "In Process"
while its ticket still said "Open", and assigning on one never reached the
other. This gives each job the Visit 1 it should always have had, brings the
job and its latest visit in step with the ticket, and settles the
disagreements.

**How conflicts are settled.** The ticket wins, because that is what the
office and the client were reading. The assignee is the ticket's where it has
one, otherwise the job's.

**Where the log goes.** Every conflict is written twice: through the
``maintenance.migration`` logger for whoever runs the migration, and as a
comment on the ticket itself, so the reconciliation is visible in the activity
feed afterwards rather than living only in a terminal that has scrolled away.

**Reversing.** That same comment carries what each value was before, in a
fixed form this file can read back, so going backwards restores the job, the
visit and the notes exactly, and removes the visits and comments it made.
"""
import logging
import re

from django.db import migrations

log = logging.getLogger("maintenance.migration")

TAG = "[migration 0019]"
MADE = f"{TAG} visit created by the corrective migration"
#: Rigid on purpose — `backwards` parses it.
#: The job is named, not just the ticket: one ticket can cover several
#: assets, and each asset's job is its own repair with its own undo.
UNDO = TAG + ' job {job_id} status was "{job}", assignee was "{who}", visit status was "{visit}"'
UNDO_RE = re.compile(
    re.escape(TAG) + r' job (\S+) status was "([^"]*)", assignee was "([^"]*)", visit status was "([^"]*)"'
)

# What a job's own status would have meant for its ticket.
JOB_READS_AS = {
    "pending": "open", "active": "open", "overdue": "assigned",
    "in_process": "in_progress", "on_hold": "assigned",
    "completed": "closed", "cancelled": "cancelled",
}
# The ticket's status, and the visit and job status that match it.
VISIT_FOR = {
    "open": "planned", "assigned": "planned", "in_progress": "in_progress",
    "pending_review": "awaiting_review", "closed": "completed",
    "cancelled": "skipped", "approved": "completed", "rejected": "awaiting_review",
}
JOB_FOR = {
    "open": "pending", "assigned": "pending", "in_progress": "in_process",
    "pending_review": "in_process", "closed": "completed",
    "cancelled": "cancelled", "approved": "completed", "rejected": "in_process",
}


def forwards(apps, schema_editor):
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    Comment = apps.get_model("tickets", "TicketComment")

    jobs = (
        Schedule.objects.filter(maintenance_type="corrective", ticket__isnull=False)
        .select_related("ticket").order_by("created_at")
    )
    seen = made = conflicts = 0

    for job in jobs:
        seen += 1
        ticket = job.ticket
        settled = ticket.status
        reads_as = JOB_READS_AS.get(job.status, "open")
        notes = []

        if reads_as != settled:
            conflicts += 1
            notes.append(
                f"the job said {job.status!r} (which reads as {reads_as!r}) "
                f"while the ticket said {settled!r}"
            )
        if (
            ticket.assigned_to_id and job.assigned_to_id
            and ticket.assigned_to_id != job.assigned_to_id
        ):
            conflicts += 1
            notes.append("the job and the ticket named different technicians")
        who = ticket.assigned_to_id or job.assigned_to_id

        # Rounds it already has keep their order; number them from one.
        existing = list(job.visits.order_by("created_at"))
        for i, v in enumerate(existing, start=1):
            if v.sequence != i:
                v.sequence = i
                v.save(update_fields=["sequence"])

        want_visit = VISIT_FOR.get(settled, "planned")
        if existing:
            visit = existing[-1]
            was_visit = visit.status
            if was_visit != want_visit:
                conflicts += 1
                notes.append(f"its latest visit said {was_visit!r}")
                visit.status = want_visit
                visit.assigned_to_id = visit.assigned_to_id or who
                visit.save(update_fields=["status", "assigned_to"])
        else:
            was_visit = ""
            Visit.objects.create(
                schedule=job, sequence=1, due_date=job.next_due,
                assigned_to_id=who, status=want_visit, notes=MADE,
            )
            made += 1

        was_job, was_who = job.status, job.assigned_to_id
        want_job = JOB_FOR.get(settled, job.status)
        if job.status != want_job or job.assigned_to_id != who:
            job.status = want_job
            job.assigned_to_id = who
            job.save(update_fields=["status", "assigned_to"])

        line = (
            f"Corrective work moved onto the ticket. "
            + ("Settled: " + "; ".join(notes) + ". " if notes else "")
            + f"The ticket's {settled!r} is what everything now reads.\n"
            + UNDO.format(job_id=job.pk, job=was_job, who=was_who or "", visit=was_visit)
        )
        Comment.objects.create(
            ticket=ticket, author=None, content=line,
            comment_type="status_change", old_status=settled, new_status=settled,
        )
        log.warning("%s / job %s: %s", ticket.ticket_number, job.pk, line.replace("\n", " "))

    log.warning(
        "Corrective migration: %d job(s), %d visit(s) created, %d conflict(s) settled.",
        seen, made, conflicts,
    )


def backwards(apps, schema_editor):
    """Put back exactly what `forwards` changed, and nothing else."""
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    Comment = apps.get_model("tickets", "TicketComment")

    restored = 0
    for comment in Comment.objects.filter(content__contains=TAG).select_related("ticket"):
        found = UNDO_RE.search(comment.content)
        if not found:
            continue
        job_id, was_job, was_who, was_visit = found.groups()
        for job in Schedule.objects.filter(pk=job_id):
            job.status = was_job
            job.assigned_to_id = was_who or None
            job.save(update_fields=["status", "assigned_to"])
            if was_visit:
                last = job.visits.order_by("sequence", "created_at").last()
                if last is not None:
                    last.status = was_visit
                    last.save(update_fields=["status"])
            restored += 1
        comment.delete()

    gone, _ = Visit.objects.filter(
        notes=MADE, schedule__maintenance_type="corrective",
    ).delete()
    log.warning(
        "Corrective migration reversed: %d job(s) restored, %d created visit(s) removed.",
        restored, gone,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0018_maintenancevisit_completed_at_and_more"),
        ("tickets", "0020_alter_ticket_status"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
