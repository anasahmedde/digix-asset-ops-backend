from django.db import migrations

# Who signs client invoices off, as the code's role defaults say. Stored role
# records hold their list as it was seeded, so a new right is added here.
ROLES = ("super_admin", "group_head")


def grant(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    for role in RoleDefinition.objects.filter(key__in=ROLES, is_builtin=True):
        caps = set(role.capabilities or ())
        if "approve_invoice" not in caps:
            role.capabilities = sorted(caps | {"approve_invoice"})
            role.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0020_roles_and_rights_22_oct")]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
