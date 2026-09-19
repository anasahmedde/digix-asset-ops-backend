"""A production step given to a vendor follows its work order: raised when the
order exists, completed when the order is delivered or completed, and back to
the project's decision if the order is cancelled.

An order names its operations on its lines (one order, several operations for
one vendor); older orders named a single step on the order itself. Both are
driven here."""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import WorkOrder, WorkOrderItem


def _steps_of(order: WorkOrder):
    steps = {}
    if order.production_step_id:
        steps[order.production_step_id] = order.production_step
    for item in order.items.select_related("production_step").filter(production_step__isnull=False):
        steps[item.production_step_id] = item.production_step
    return list(steps.values())


def drive_step(step, order: WorkOrder):
    """Bring one operation in line with the order that covers it."""
    from apps.assets.models import ProductionStep

    now = timezone.now()
    fields = []
    if order.status == WorkOrder.Status.CANCELLED:
        # No other live order for it: the decision goes back to the project.
        if not step.live_work_orders().exclude(pk=order.pk).exists():
            step.location = ProductionStep.Location.UNDECIDED
            step.workshop = None
            step.workshop_name = ""
            step.work_order_requested_at = None
            fields += ["location", "workshop", "workshop_name", "work_order_requested_at"]
            if step.status == ProductionStep.Status.SENT_OUT:
                step.status = ProductionStep.Status.PENDING
                fields.append("status")
    elif order.status == WorkOrder.Status.COMPLETED:
        # Inspected and accepted: the operation is done.
        if step.status != ProductionStep.Status.COMPLETED:
            step.status = ProductionStep.Status.COMPLETED
            fields.append("status")
            if step.returned_at is None:
                step.returned_at = now
                fields.append("returned_at")
            if step.completed_at is None:
                step.completed_at = now
                fields.append("completed_at")
    elif order.status in (WorkOrder.Status.DELIVERED, WorkOrder.Status.PARTIALLY_DELIVERED):
        # Back from the workshop, waiting for inspection.
        if step.status == ProductionStep.Status.SENT_OUT:
            step.status = ProductionStep.Status.RETURNED
            fields.append("status")
            if step.returned_at is None:
                step.returned_at = now
                fields.append("returned_at")
    else:
        if step.location != ProductionStep.Location.EXTERNAL or step.workshop_id != order.supplier_id:
            step.location = ProductionStep.Location.EXTERNAL
            step.workshop = order.supplier
            step.workshop_name = ""
            fields += ["location", "workshop", "workshop_name"]
        if step.work_order_requested_at is not None:
            # The request has been answered: the order is what stands now.
            step.work_order_requested_at = None
            fields.append("work_order_requested_at")
        if step.status in (ProductionStep.Status.PENDING, ProductionStep.Status.IN_PROGRESS, ProductionStep.Status.RETURNED):
            step.status = ProductionStep.Status.SENT_OUT
            fields.append("status")
            if step.sent_at is None:
                step.sent_at = now
                fields.append("sent_at")
    if fields:
        step.save(update_fields=list(dict.fromkeys(fields + ["updated_at"])))


@receiver(post_save, sender=WorkOrder)
def drive_production_steps(sender, instance: WorkOrder, created: bool, **kwargs):
    for step in _steps_of(instance):
        drive_step(step, instance)


@receiver(post_save, sender=WorkOrderItem)
def drive_line_step(sender, instance: WorkOrderItem, created: bool, **kwargs):
    if instance.production_step_id:
        drive_step(instance.production_step, instance.work_order)
