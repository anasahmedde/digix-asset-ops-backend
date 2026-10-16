"""An installation's own history: who did what to it, and when.

The tracker pieced a timeline together from step timestamps, so assignment,
edits, scans, holds, step changes and photos left no trace of who did them.
Every act on an installation now writes one line here.
"""
from __future__ import annotations


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
    fields = {"installation": installation, "step": step, "action": action, "text": text[:500], "actor": actor}
    if at is not None:
        fields["at"] = at
    return InstallationActivity.objects.create(**fields)


def rows(installation, limit: int = 500) -> list[dict]:
    """The installation's history, newest first, as the screen shows it."""
    return [
        {
            "id": str(e.pk),
            "action": e.action,
            "text": e.text,
            "actor": name(e.actor) or None,
            "at": e.at.isoformat(),
        }
        for e in installation.activity.select_related("actor")[:limit]
    ]
