"""The installation as a job of work: paused and resumed, timed, and costed.

The same rules as a maintenance visit (attendance.worktime): the work runs
from the first step started to the installation done, less the pauses;
checking out with the work going pauses it; the installer's supervisor
records what it cost, and that is the asset's actual installation cost.
"""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from common import clock
from common.permissions import can

from . import activity
from .models import DeviceInstallation, InstallationCostLine, InstallationPause

DONE_STATUSES = ("installed", "active", "client_property")


def started_at(inst):
    """When the work began: the first step started."""
    times = [s.started_at for s in inst.steps.all() if s.started_at]
    return min(times) if times else None


def work_done(inst) -> bool:
    """Every step done, or the asset already installed and live."""
    return bool(inst.completed_at) or inst.device.status in DONE_STATUSES


def in_progress(inst) -> bool:
    return bool(started_at(inst)) and not work_done(inst) and inst.removed_at is None


def paused_since(inst):
    p = next((p for p in inst.pauses.all() if p.resumed_at is None), None)
    return p.paused_at if p else None


def _pairs(inst):
    return [(p.paused_at, p.resumed_at) for p in sorted(inst.pauses.all(), key=lambda p: p.paused_at)]


@transaction.atomic
def pause(inst, *, user, reason="", at=None, automatic=False):
    """Stop the clock on the work: overnight, or waiting on something."""
    if not in_progress(inst):
        raise ValidationError({"detail": "Only work that has started and is not done can be paused."})
    if paused_since(inst):
        raise ValidationError({"detail": "The work is already paused."})
    InstallationPause.objects.create(
        installation=inst, paused_at=at or clock.now(), paused_by=user, automatic=automatic,
        reason=(reason or "").strip()[:255],
    )
    activity.log(inst, "paused", "Work paused" + (" on check-out" if automatic else "")
                 + (f": {reason.strip()}" if reason and reason.strip() else ""), user=user)
    return inst


@transaction.atomic
def resume(inst, *, user, at=None):
    """Pick the work up again: the clock runs from here."""
    if inst.pauses.filter(resumed_at__isnull=True).update(resumed_at=at or clock.now()) == 0:
        raise ValidationError({"detail": "The work is not paused."})
    activity.log(inst, "resumed", "Work resumed", user=user)
    return inst


def require_not_paused(inst):
    if paused_since(inst):
        raise ValidationError({"detail": "The work is paused - resume it first."})


def pause_on_check_out(user, when):
    """Checking out with an installation under way pauses it, so the night
    is not counted as work if nobody remembered to press Pause."""
    open_jobs = (
        DeviceInstallation.objects.filter(installed_by=user, completed_at__isnull=True, removed_at__isnull=True,
                                          steps__started_at__isnull=False)
        .select_related("device").prefetch_related("steps", "pauses").distinct()
    )
    for inst in open_jobs:
        if in_progress(inst) and not paused_since(inst):
            pause(inst, user=user, at=when, automatic=True, reason="Checked out")


def technician_here(inst) -> bool:
    """The installer is checked in at the site: the work happens only then."""
    from apps.attendance.presence import clocked_in_at

    if not getattr(settings, "FIELD_WORK_NEEDS_CHECK_IN", True) or not inst.site_id:
        return True
    return bool(inst.installed_by_id) and clocked_in_at(inst.installed_by_id, inst.site_id)


def time_on_site(inst):
    """Man-hours for the installer and helpers (attendance.worktime.crew_hours)."""
    from apps.attendance.worktime import crew_hours

    began = started_at(inst)
    return crew_hours(
        inst.installed_by, list(inst.helpers.all()), inst.site_id,
        inst.asset_verified_at or began, began, inst.completed_at, _pairs(inst),
    )


def on_site(inst):
    """Every stay at the site over the work (attendance.worktime.stays)."""
    from apps.attendance.worktime import stays

    acts = [t for t in (inst.asset_verified_at, started_at(inst), inst.completed_at) if t]
    return stays(inst.installed_by_id, inst.site_id, min(acts) if acts else None, inst.completed_at)


def may_record_cost(user, inst) -> bool:
    """The installer's supervisor (anyone up their line), whoever closes
    installations, or the Administrator - never the installer themselves."""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or getattr(user, "role", None) == "super_admin":
        return True
    if inst.installed_by_id == user.pk:
        return False
    installer = inst.installed_by
    return can(user, "close_installation") or bool(installer and user.manages(installer))


@transaction.atomic
def record_cost(inst, *, user, extra=None, labour=None):
    """Write what the installation cost - what the crew paid, man-hours at
    each person's rate, anything else - and make it the asset's actual
    installation cost. Recording again replaces the lines."""
    from apps.assets.models import Device
    from apps.attendance.worktime import labour_rows, manual_rows

    if not may_record_cost(user, inst):
        raise PermissionDenied("The installer's supervisor records what the installation cost.")
    if not work_done(inst):
        raise ValidationError({"detail": "The installation is not done yet: its cost is recorded once the work is."})
    inst.cost_lines.all().delete()
    Line = InstallationCostLine
    lines = [
        Line(
            installation=inst, source=Line.Source.PURCHASE,
            description=(p.description if p.kind == p.Kind.PART else f"{p.get_kind_display()}: {p.description}"),
            quantity=p.quantity, unit_cost=(p.amount / p.quantity).quantize(Decimal("0.01")), amount=p.amount,
        )
        for p in inst.purchases.all()
    ]
    lines += [Line(installation=inst, source=Line.Source.MANUAL, **row) for row in manual_rows(extra)]
    lines += [Line(installation=inst, source=Line.Source.LABOUR, **row) for row in labour_rows(time_on_site(inst), labour)]
    Line.objects.bulk_create(lines)
    total = sum((x.amount for x in lines), Decimal("0.00"))
    inst.cost, inst.cost_recorded_by, inst.cost_recorded_at = total, user, clock.now()
    inst.save(update_fields=["cost", "cost_recorded_by", "cost_recorded_at", "updated_at"])
    Device.objects.filter(pk=inst.device_id).update(actual_installation_cost=total)
    activity.log(inst, "cost_recorded", f"Installation cost finalized: PKR {total:,.2f}", user=user)
    from apps.notifications import service as notices

    notices.resolve(f"inst-cost:{inst.pk}")
    return total


def ask_to_finalize(inst, actor=None):
    """The work is done: the installer's supervisor is asked to finalize the
    cost - travel, transport, anything else - once, until it is done."""
    from apps.notifications import service as notices
    from apps.notifications.models import Notification

    ref = f"inst-cost:{inst.pk}"
    if inst.cost is not None or Notification.objects.filter(ref=ref, resolved_at__isnull=True).exists():
        return
    notices.ask(
        "close_installation", scope_to=inst.installed_by, exclude=[actor] if actor else (),
        title=f"Finalize installation cost: {inst.device.asset_code}",
        message=f"{inst.site.name} · installed by {activity.name(inst.installed_by) if inst.installed_by else 'the vendor'}",
        link=f"/installation-tracker?installation={inst.pk}", ref=ref, data={"installation": str(inst.pk)},
    )
