"""A maintenance job's own history: who did what to it, and when.

Corrective jobs wrote a few lines onto their ticket and preventive ones wrote
nothing, so a job could be assigned, started, supplied and signed off without
leaving a trace of who did any of it. Every act on a job now writes one line
here, and the job's screen reads them back.
"""
from __future__ import annotations

from common import clock


def name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.username


def log(job, action: str, text: str, *, user=None, visit=None, at=None):
    """Record one act on the job."""
    from .models import MaintenanceActivity

    if job is None:
        return None
    actor = user if getattr(user, "is_authenticated", False) else None
    fields = {"schedule": job, "visit": visit, "action": action, "text": (text + clock.offline_note())[:500], "actor": actor}
    if at is not None:
        fields["at"] = at
    return MaintenanceActivity.objects.create(**fields)


def visit_word(visit) -> str:
    """'Visit 2' on a fault, 'Round 2' on a schedule."""
    corrective = visit.schedule.maintenance_type == "corrective"
    return f"{'Visit' if corrective else 'Round'} {visit.sequence}"


def rows(job, limit: int = 500) -> list[dict]:
    """The job's history, newest first, as the screen shows it."""
    return [
        {
            "id": str(e.pk),
            "action": e.action,
            "text": e.text,
            "actor": name(e.actor) or None,
            "at": e.at.isoformat(),
            "visit": e.visit.sequence if e.visit_id else None,
        }
        for e in job.activity.select_related("actor", "visit")[:limit]
    ]
