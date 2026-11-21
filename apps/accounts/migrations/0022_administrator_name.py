from django.db import migrations

# The top role was seeded as "Super Admin" and later renamed "Administrator"
# in the code, the matrix and the field app. The stored record kept the old
# name, so the header and the Employees list still said "Super Admin".
# A name somebody chose on Roles & Rights is left alone.


def rename(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    RoleDefinition.objects.filter(key="super_admin", label="Super Admin").update(label="Administrator")


def restore(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    RoleDefinition.objects.filter(key="super_admin", label="Administrator").update(label="Super Admin")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0021_grant_approve_invoice")]
    operations = [migrations.RunPython(rename, restore)]
