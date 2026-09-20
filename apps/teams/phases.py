"""How far each phase of a project has got.

A phase is a body of work, so it can be measured against what it needs to
finish: parts procured and issued, operations built, installations run, assets
handed over. Every figure here is counted from the work itself — nothing is
typed in, so a bar cannot flatter a project that has not moved.
"""
from __future__ import annotations

from .costing import project_devices


def _bar(done: int, total: int, note: str, percent: int | None = None) -> dict:
    """done/total for the note, and the percent they imply.

    ``percent`` is passed where the share is not the plain ratio — a phase
    split evenly between assets, each of which is partly done.
    """
    return {
        "done": done,
        "total": total,
        "percent": percent if percent is not None else (
            round(done / total * 100) if total else 0
        ),
        "note": note,
    }


def _even_split(shares: list[float], thing: str) -> dict:
    """A phase divided equally between the assets, each part-filled.

    ``shares`` is one fraction per asset, 0 to 1. Every asset counts the same
    however much work it happens to carry, so a project of three assets moves
    a third at a time and no further until the next one catches up.
    """
    if not shares:
        return _bar(0, 0, "No assets yet")
    done = sum(1 for share in shares if share >= 1)
    return _bar(
        done, len(shares), f"{done} of {len(shares)} assets {thing}",
        percent=round(sum(shares) / len(shares) * 100),
    )


def phase_progress(project) -> dict:
    """Each phase of this project, with how much of it is finished.

    Returns {phase: {done, total, percent, note}} for the phases that are work.
    A phase with nothing to do reads 0 of 0, which is honest: there is nothing
    to finish, and nothing has been.
    """
    from apps.assets.models import Device
    from apps.sites.models import DeviceInstallation, InstallationStep

    from .models import Project, ProjectBudget

    devices = list(
        project_devices(project).prefetch_related("components", "production_steps")
    )
    device_ids = [d.pk for d in devices]

    # ── Planning: the estimate is agreed when the budget is signed off ──
    plan = ProjectBudget.objects.filter(project=project).first()
    approved = plan is not None and plan.status == ProjectBudget.Status.APPROVED
    planning = _bar(
        1 if approved else 0, 1,
        "Budget approved" if approved
        else "Budget awaiting approval" if plan is not None and plan.status == ProjectBudget.Status.SUBMITTED
        else "Budget not submitted",
    )

    # ── Procurement: every asset supplied, each worth the same ──
    shares = []
    for device in devices:
        if device.source != Device.Source.INHOUSE:
            # A whole asset bought from a vendor: it has arrived or it has not.
            shares.append(0.0 if device.status == Device.Status.PROCURED else 1.0)
            continue
        components = list(device.components.all())
        wanted = sum(c.quantity for c in components)
        if not wanted:
            # Nothing to buy for it, so nothing is holding it up.
            shares.append(1.0)
            continue
        shares.append(
            sum(min(c.issued_quantity, c.quantity) for c in components) / wanted
        )
    procurement = _even_split(shares, "supplied")

    # ── Production: every asset built, each worth the same ──
    shares = []
    for device in devices:
        if device.source != Device.Source.INHOUSE:
            # The vendor built it. It is finished the moment it turns up, so
            # its share follows the delivery, exactly as procurement's does.
            shares.append(0.0 if device.status == Device.Status.PROCURED else 1.0)
            continue
        steps = list(device.production_steps.all())
        if not steps:
            # Built here, but with no route on it: nothing to make.
            shares.append(1.0)
            continue
        finished = sum(1 for s in steps if s.status in ("completed", "skipped"))
        shares.append(finished / len(steps))
    production = _even_split(shares, "built")

    # ── Installation: the checklist the technician works through on site ──
    jobs = (
        DeviceInstallation.objects.filter(device_id__in=device_ids)
        .prefetch_related("steps")
        .order_by("device_id", "-installed_at")
    )
    # The live job per asset — the most recent one, where a job was reopened.
    latest = {}
    for job in jobs:
        latest.setdefault(job.device_id, job)
    shares = []
    for device in devices:
        job = latest.get(device.pk)
        if job is None:
            # Nobody has opened a job for it, so none of it is installed.
            shares.append(0.0)
            continue
        steps = list(job.steps.all())
        if not steps:
            shares.append(0.0)
            continue
        done_here = sum(
            1 for s in steps
            if s.status in (
                InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED,
            )
        )
        shares.append(done_here / len(steps))
    installation = _even_split(shares, "installed")
    if devices and not latest:
        installation["note"] = "No installation opened yet"

    # ── Handing over: an asset is handed over when it is running ──
    handover = _even_split(
        [
            1.0 if d.status in (Device.Status.ACTIVE, Device.Status.CLIENT_PROPERTY) else 0.0
            for d in devices
        ],
        "handed over",
    )

    return {
        Project.Phase.PLANNING: planning,
        Project.Phase.PROCUREMENT: procurement,
        Project.Phase.PRODUCTION: production,
        Project.Phase.INSTALLATION: installation,
        Project.Phase.HANDOVER: handover,
    }
