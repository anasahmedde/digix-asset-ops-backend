"""A production step given to a workshop follows its work order: raised when
the order exists, completed when the order is delivered or completed, and
back to the project's decision if the order is cancelled."""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import WorkOrder


@receiver(post_save, sender=WorkOrder)
def drive_production_step(sender, instance: WorkOrder, created: bool, **kwargs):
    step = instance.production_step
    if step is None:
        return
    from apps.assets.models import ProductionStep

    now = timezone.now()
    fields = []
    if instance.status == WorkOrder.Status.CANCELLED:
        if not step.work_orders.exclude(status=WorkOrder.Status.CANCELLED).exclude(pk=instance.pk).exists():
            step.location = ProductionStep.Location.UNDECIDED
            step.workshop = None
            step.workshop_name = ""
            fields += ["location", "workshop", "workshop_name"]
            if step.status == ProductionStep.Status.SENT_OUT:
                step.status = ProductionStep.Status.PENDING
                fields.append("status")
    elif instance.status in (WorkOrder.Status.DELIVERED, WorkOrder.Status.COMPLETED):
        if step.status != ProductionStep.Status.COMPLETED:
            step.status = ProductionStep.Status.COMPLETED
            fields.append("status")
            if step.returned_at is None:
                step.returned_at = now
                fields.append("returned_at")
            if step.completed_at is None:
                step.completed_at = now
                fields.append("completed_at")
    else:
        if step.location != ProductionStep.Location.EXTERNAL or step.workshop_id != instance.supplier_id:
            step.location = ProductionStep.Location.EXTERNAL
            step.workshop = instance.supplier
            step.workshop_name = ""
            fields += ["location", "workshop", "workshop_name"]
        if step.status in (ProductionStep.Status.PENDING, ProductionStep.Status.IN_PROGRESS, ProductionStep.Status.RETURNED):
            step.status = ProductionStep.Status.SENT_OUT
            fields.append("status")
            if step.sent_at is None:
                step.sent_at = now
                fields.append("sent_at")
    if fields:
        step.save(update_fields=list(dict.fromkeys(fields + ["updated_at"])))
