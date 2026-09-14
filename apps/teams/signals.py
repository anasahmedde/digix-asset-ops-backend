"""Keep an asset's project link and the project's Scope saying the same thing.

An asset can be put on a project two ways: the Project field on the asset
itself, or a line in the project's Scope. Both counted towards the cost plan
and the build requirements, but only Scope lines showed in the Scope list — so
an asset linked from the asset form was costed without ever appearing in the
project's scope. The Scope line is the record; the asset's field is a shortcut
that writes one.
"""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.assets.models import Device

from .models import ProjectScopeItem


@receiver(pre_save, sender=Device)
def remember_previous_project(sender, instance: Device, **kwargs):
    instance._previous_project_id = (
        Device.objects.filter(pk=instance.pk).values_list("project_id", flat=True).first()
        if instance.pk else None
    )


@receiver(post_save, sender=Device)
def mirror_project_field_into_scope(sender, instance: Device, created: bool, **kwargs):
    previous = getattr(instance, "_previous_project_id", None)
    current = instance.project_id
    if previous == current and not created:
        return

    # Moved to another project from the asset form: the whole-asset line on
    # the old project goes with it. Component-level lines are left alone.
    if previous and previous != current:
        ProjectScopeItem.objects.filter(
            project_id=previous, device=instance, component__isnull=True,
        ).delete()

    if current and not ProjectScopeItem.objects.filter(project_id=current, device=instance).exists():
        ProjectScopeItem.objects.create(
            project_id=current,
            device=instance,
            quantity=1,
            site=instance.current_site,
            notes="Linked from the asset's Project field",
        )


@receiver(post_delete, sender=ProjectScopeItem)
def release_project_field(sender, instance: ProjectScopeItem, **kwargs):
    """Taking an asset's last line off a project's Scope takes it off the project.

    Updated through the queryset so the Device signals above do not fire and
    put the line straight back.
    """
    if ProjectScopeItem.objects.filter(
        project_id=instance.project_id, device_id=instance.device_id,
    ).exists():
        return
    Device.objects.filter(pk=instance.device_id, project_id=instance.project_id).update(project=None)
