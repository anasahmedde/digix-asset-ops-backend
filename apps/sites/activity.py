"""An installation's own history: who did what to it, and when.

The tracker pieced a timeline together from step timestamps, so assignment,
edits, scans, holds, step changes and photos left no trace of who did them.
Every act on an installation now writes one line here.
"""
from __future__ import annotations

from common import clock


def name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.username


def step_name(step) -> str:
    return step.custom_label or step.get_step_type_display()


def log(installation, action: str, text: str, *, user=None, step=None, at=None):
    """Record one act on the installation."""
    from .models import InstallationActivity

    if installation is None:
        return None
    actor = user if getattr(user, "is_authenticated", False) else None
    fields = {"installation": installation, "step": step, "action": action, "text": (text + clock.offline_note())[:500], "actor": actor}
    if at is not None:
        fields["at"] = at
    return InstallationActivity.objects.create(**fields)


def rows(installation, limit: int = 500) -> list[dict]:
    """The installation's history, newest first, as the screen shows it -
    with the installer's and helpers' check-ins and check-outs at its site."""
    out = [
        {
            "id": str(e.pk),
            "action": e.action,
            "text": e.text,
            "actor": name(e.actor) or None,
            "at": e.at.isoformat(),
            "_at": e.at,
        }
        for e in installation.activity.select_related("actor")[:limit]
    ]
    out += presence_rows(installation)
    out.sort(key=lambda r: r["_at"], reverse=True)
    for r in out:
        del r["_at"]
    return out[:limit]


def presence_rows(installation) -> list[dict]:
    """When the crew reached the site and left it, while the job was open.

    Read from attendance, not copied into the log: a check-in recorded on a
    phone without signal lands later with its own time and still shows here.
    """
    from datetime import timedelta

    from apps.attendance.models import AttendanceRecord

    crew = {}
    if installation.installed_by_id:
        crew[installation.installed_by_id] = "installer"
    for helper_id in installation.helpers.values_list("pk", flat=True):
        crew.setdefault(helper_id, "helper")
    if not crew:
        return []
    records = AttendanceRecord.objects.filter(
        site_id=installation.site_id, user_id__in=crew, created_at__gte=installation.created_at,
    ).select_related("user", "site")
    if installation.completed_at:
        # Leaving the site after the last step still belongs to the job.
        records = records.filter(created_at__lte=installation.completed_at + timedelta(days=1))
    out = []
    for r in records:
        came = r.check_type == AttendanceRecord.CheckType.CHECK_IN
        where = r.site.name if r.site_id else "the site"
        out.append({
            "id": f"presence-{r.pk}",
            "action": "checked_in" if came else "checked_out",
            "text": (f"Checked in at {where}" if came else f"Checked out of {where}")
                    + f" ({crew[r.user_id]})" + (" - recorded offline" if r.recorded_offline else ""),
            "actor": name(r.user) or None,
            "at": r.created_at.isoformat(),
            "_at": r.created_at,
        })
    return out
