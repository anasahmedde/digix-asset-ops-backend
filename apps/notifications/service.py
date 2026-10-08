"""Telling the right people that something is waiting on them.

Every workflow in the system has the same shape: somebody raises a thing,
it waits with whoever may decide it, and the answer goes back. This module
is the one way all of them say so.

* ``holders_of``: who may do a thing right now - read off the roles and the
  per-person adjustments, never off a role name, so a role written on the
  Roles screen is told like any other.
* ``ask``: something is waiting on the holders of a capability. Actionable,
  so it stays pinned until ``resolve`` is called for the same ``ref``.
* ``tell``: something you raised has moved. Plain, read once.
* ``resolve``: the thing moved on; nothing is waiting any more.

Nothing here raises. A notification that cannot be sent is a log line, never
a failed order.
"""

from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)


def _ids(users):
    return {getattr(u, "pk", u) for u in (users or ()) if u is not None}


def holders_of(*capabilities, exclude=(), project=None, scope_to=None):
    """Everyone active who holds any of ``capabilities``.

    ``project``: also the project's manager, where the capability is one a
    manager decides for their own project. ``scope_to``: a worker whose own
    reporting line decides - keeps the holders who manage them or who act
    across teams, which is how a supervisor is told about their crew only.
    """
    from apps.accounts.capabilities import PROJECT_MANAGER_CAPABILITIES, RENAMED
    from apps.accounts.models import User, UserCapability
    from apps.accounts.roles import capability_map

    wanted = set(capabilities)
    # A stored key that was since renamed still means the new ones.
    stored = set(wanted)
    for old, new in RENAMED.items():
        if wanted & set(new):
            stored.add(old)

    roles = [role for role, caps in capability_map().items() if caps & wanted]
    granted = set(
        UserCapability.objects.filter(allowed=True, capability__in=stored).values_list("user_id", flat=True)
    )
    withdrawn_rows = UserCapability.objects.filter(allowed=False, capability__in=stored).values_list(
        "user_id", "capability"
    )
    withdrawn_by_user: dict = {}
    for uid, cap in withdrawn_rows:
        withdrawn_by_user.setdefault(uid, set()).update(RENAMED.get(cap, (cap,)))

    qs = User.objects.filter(is_active=True).filter(Q(role__in=roles) | Q(pk__in=granted) | Q(is_superuser=True))
    people = []
    for user in qs.distinct():
        if user.is_superuser or user.pk in granted:
            people.append(user)
            continue
        # Role grants it, unless every wanted key was withdrawn for them.
        role_caps = capability_map().get(user.role, frozenset()) & wanted
        if role_caps - withdrawn_by_user.get(user.pk, set()):
            people.append(user)

    if project is not None and wanted & PROJECT_MANAGER_CAPABILITIES and project.manager_id:
        if not any(p.pk == project.manager_id for p in people):
            manager = User.objects.filter(pk=project.manager_id, is_active=True).first()
            if manager is not None:
                people.append(manager)

    if scope_to is not None:
        people = [
            p for p in people
            if p.pk == getattr(scope_to, "pk", None)
            or p.is_superuser or "act_across_teams" in p.capabilities or p.manages(scope_to)
        ]

    skip = _ids(exclude)
    return [p for p in people if p.pk not in skip]


def _push(notification):
    from .signals import _push_ws

    try:
        _push_ws(notification)
    except Exception:  # noqa: BLE001 - a dropped socket must not fail the request
        logger.exception("push failed for notification %s", notification.pk)


def notify(recipients, *, kind, title, message="", link="", ref="", data=None,
           actionable=False, email=False, exclude=(), ticket=None, installation=None):
    """Create one notification per recipient, push it, optionally email it."""
    from . import tasks

    skip = _ids(exclude)
    seen = set()
    made = []
    for user in recipients or ():
        uid = getattr(user, "pk", user)
        if uid is None or uid in skip or uid in seen:
            continue
        seen.add(uid)
        try:
            note = Notification.objects.create(
                recipient_id=uid,
                notification_type=kind,
                title=title[:300],
                message=message,
                link=link[:300],
                ref=ref[:120],
                data=data or {},
                is_actionable=actionable,
                ticket=ticket,
                installation=installation,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not create notification for %s", uid)
            continue
        made.append(note)
        _push(note)
        if email:
            try:
                tasks.queue_notification_email(note)
            except Exception:  # noqa: BLE001
                logger.exception("could not queue email for notification %s", note.pk)
    return made


def ask(capabilities, *, title, message="", link="", ref="", data=None, exclude=(),
        project=None, scope_to=None, email=True, kind=None, **about):
    """Something is waiting on whoever may decide it.

    ``about`` carries the record it concerns (``ticket=``, ``installation=``)
    through to the notification row."""
    caps = (capabilities,) if isinstance(capabilities, str) else tuple(capabilities)
    people = holders_of(*caps, exclude=exclude, project=project, scope_to=scope_to)
    return notify(
        people, kind=kind or Notification.Type.APPROVAL_REQUESTED, title=title, message=message,
        link=link, ref=ref, data=data, actionable=True, email=email, **about,
    )


def tell(users, *, title, message="", link="", ref="", data=None, exclude=(), email=False,
         kind=None, **about):
    """Something you raised or own has moved on."""
    return notify(
        users, kind=kind or Notification.Type.WORKFLOW_UPDATE, title=title, message=message,
        link=link, ref=ref, data=data, actionable=False, email=email, exclude=exclude, **about,
    )


def resolve(ref):
    """The thing moved: nothing is waiting on it any more."""
    if not ref:
        return 0
    return Notification.objects.filter(ref=ref, is_actionable=True, resolved_at__isnull=True).update(
        resolved_at=timezone.now()
    )


def who(user) -> str:
    return (user.get_full_name() or user.username) if user else "Someone"
