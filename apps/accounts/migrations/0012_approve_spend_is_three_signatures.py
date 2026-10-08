"""One "approve spending" right becomes three: the budget, the purchase
order and the work order each have their own signature now, so a role or a
person that held the old key holds all three."""
from django.db import migrations

OLD = "approve_spend"
NEW = ("approve_po", "approve_work_order", "approve_budget")


def split(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    UserCapability = apps.get_model("accounts", "UserCapability")
    for role in RoleDefinition.objects.all():
        caps = list(role.capabilities or [])
        if OLD in caps:
            caps = sorted((set(caps) - {OLD}) | set(NEW))
            role.capabilities = caps
            role.save(update_fields=["capabilities"])
    for row in UserCapability.objects.filter(capability=OLD):
        for key in NEW:
            UserCapability.objects.get_or_create(
                user=row.user, capability=key,
                defaults={"allowed": row.allowed, "granted_by": row.granted_by, "reason": row.reason},
            )
        row.delete()


def join(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    for role in RoleDefinition.objects.all():
        caps = set(role.capabilities or [])
        if caps & set(NEW):
            role.capabilities = sorted((caps - set(NEW)) | {OLD})
            role.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_user_client")]
    operations = [migrations.RunPython(split, join)]
