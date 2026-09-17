"""Steps that were defaulted or copied as in-house, on assets that belong to a
project, never had a decision made on them: hand them back to the project."""
from django.db import migrations


def reopen(apps, schema_editor):
    ProductionStep = apps.get_model("assets", "ProductionStep")
    ProjectScopeItem = apps.get_model("teams", "ProjectScopeItem")
    WorkOrder = apps.get_model("workorders", "WorkOrder")

    scoped = set(ProjectScopeItem.objects.values_list("device_id", flat=True))
    busy = set(
        WorkOrder.objects.exclude(status="cancelled")
        .filter(production_step__isnull=False)
        .values_list("production_step_id", flat=True)
    )
    # In-house with no workshop, or external with no live work order: either way
    # the step has not started and nobody decided it in Execution.
    open_steps = ProductionStep.objects.select_related("device").filter(
        location__in=("in_house", "external"), status="pending",
    )
    for step in open_steps:
        if step.pk in busy:
            continue
        if step.location == "in_house" and (step.workshop_id or step.workshop_name):
            continue
        if step.device.project_id or step.device_id in scoped:
            step.location = "undecided"
            step.workshop = None
            step.workshop_name = ""
            step.save(update_fields=["location", "workshop", "workshop_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0025_productionstep_location_undecided"),
        ("teams", "0011_project_sites_alter_project_phase_and_more"),
        ("workorders", "0003_vendor_asset_procurement_and_step_work_orders"),
    ]
    operations = [migrations.RunPython(reopen, migrations.RunPython.noop)]
