"""Keeping a checklist's step numbers equal to its positions.

A step's number is where it sits in the list, not a label it carries around.
Storing it on the row is only a convenience for ordering, so whenever the list
changes the numbers are worked out again — otherwise removing the fourth step
leaves a checklist that reads 1, 2, 3, 5, 6, 7 and looks broken.

Every write here goes in two passes. (installation, step_number) is unique, so
writing the final numbers straight out makes a step land on a number another
step has not vacated yet. The first pass parks them all out of the way.
"""
from __future__ import annotations

# High enough that no real checklist reaches it, so the parking numbers cannot
# collide with the ones still in place.
_PARKING = 1000


def _write_positions(steps: list) -> None:
    """Number ``steps`` 1..n in the order given, without tripping the index."""
    from .models import InstallationStep

    if not steps:
        return
    for offset, step in enumerate(steps, start=1):
        step.step_number = _PARKING + offset
    InstallationStep.objects.bulk_update(steps, ["step_number"])

    for position, step in enumerate(steps, start=1):
        step.step_number = position
    InstallationStep.objects.bulk_update(steps, ["step_number"])


def renumber_steps(installation_id) -> int:
    """Number this installation's steps 1..n in their current order.

    Returns how many rows were out of place, so a caller can tell whether the
    list needed tidying at all.
    """
    from .models import InstallationStep

    steps = list(
        InstallationStep.objects
        .filter(installation_id=installation_id)
        .order_by("step_number", "created_at")
    )
    out_of_place = sum(
        1 for position, step in enumerate(steps, start=1)
        if step.step_number != position
    )
    if out_of_place:
        _write_positions(steps)
    return out_of_place


def apply_step_order(installation_id, step_ids) -> list:
    """Put this installation's steps in the given order, numbered 1..n.

    ``step_ids`` is the new running order. Any step the caller leaves out keeps
    its place at the end, in the order it already had, so a list from a screen
    that has not refreshed cannot quietly drop a step.
    """
    from .models import InstallationStep

    steps = {
        str(s.pk): s
        for s in InstallationStep.objects.filter(installation_id=installation_id)
    }
    ordered = [steps.pop(str(pk)) for pk in step_ids if str(pk) in steps]
    ordered += sorted(steps.values(), key=lambda s: (s.step_number, s.created_at))
    _write_positions(ordered)
    return ordered
