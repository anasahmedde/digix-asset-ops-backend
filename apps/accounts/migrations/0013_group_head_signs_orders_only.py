"""The Group Head signs purchase and work orders; Operations raise them.
The role row seeded from the old defaults still said the head could raise
both, so the matrix is applied to it."""
from django.db import migrations

GONE = {"raise_po", "raise_work_order"}


def apply_matrix(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    for role in RoleDefinition.objects.filter(key="group_head"):
        role.capabilities = sorted(set(role.capabilities or []) - GONE)
        role.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_approve_spend_is_three_signatures")]
    operations = [migrations.RunPython(apply_matrix, migrations.RunPython.noop)]
