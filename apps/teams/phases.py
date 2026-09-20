"""How far each phase of a project has got.

A phase is a body of work, so it can be measured against what it needs to
finish: parts procured and issued, operations built, installations run, assets
handed over. Every figure here is counted from the work itself — nothing is
typed in, so a bar cannot flatter a project that has not moved.
"""
from __future__ import annotations

from .costing import project_devices


def _bar(done: int, total: int, note: str) -> dict:
    return {
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
        "note": note,
    }


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

    # ── Procurement: every part in hand, every bought asset received ──
    needed = 0
    got = 0
    for device in devices:
        if device.source != Device.Source.INHOUSE:
            # A whole asset bought from a vendor: one line, in or not.
            needed += 1
            got += 1 if device.status != Device.Status.PROCURED else 0
            continue
        for component in device.components.all():
            needed += component.quantity
            got += min(component.issued_quantity, component.quantity)
    procurement = _bar(
        got, needed,
        f"{got} of {needed} issued or received" if needed else "Nothing to procure",
    )

    # ── Production: every operation on every in-house route ──
    ops_done = 0
    ops_total = 0
    for device in devices:
        for step in device.production_steps.all():
            ops_total += 1
            if step.status in ("completed", "skipped"):
                ops_done += 1
    production = _bar(
        ops_done, ops_total,
        f"{ops_done} of {ops_total} operations finished" if ops_total else "Nothing to build",
    )

    # ── Installation: the checklist the technician works through on site ──
    jobs = (
        DeviceInstallation.objects.filter(device_id__in=device_ids)
        .prefetch_related("steps")
        .order_by("device_id", "-installed_at")
    )
    seen = set()
    steps_done = 0
    steps_total = 0
    for job in jobs:
        if job.device_id in seen:
            continue
        seen.add(job.device_id)
        for step in job.steps.all():
            steps_total += 1
            if step.status in (
                InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED,
            ):
                steps_done += 1
    if steps_total:
        note = f"{steps_done} of {steps_total} steps done across {len(seen)} installation(s)"
    else:
        note = "No installation opened yet" if device_ids else "No assets yet"
    installation = _bar(steps_done, steps_total, note)

    # ── Handing over: an asset is handed over when it is running ──
    handed = sum(
        1 for d in devices
        if d.status in (Device.Status.ACTIVE, Device.Status.CLIENT_PROPERTY)
    )
    handover = _bar(
        handed, len(devices),
        f"{handed} of {len(devices)} assets active" if devices else "No assets yet",
    )

    return {
        Project.Phase.PLANNING: planning,
        Project.Phase.PROCUREMENT: procurement,
        Project.Phase.PRODUCTION: production,
        Project.Phase.INSTALLATION: installation,
        Project.Phase.HANDOVER: handover,
    }
