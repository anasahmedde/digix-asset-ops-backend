"""See project costs: a right of its own, starting exactly where prices were.

Project amounts were hidden under "See prices and costs", and only by field
name, so some reached people the rest was hidden from. They now answer to
"See project costs". Every role and every person who could see prices is
given it, so nobody sees more or less until somebody changes it in Roles &
Rights.
"""
from django.db import migrations


# The rights each of these cannot be used without, as they stood when this
# ran (the live map is apps.accounts.capabilities.REQUIRES).
REQUIRES = {
    "plan_budget": ("view_project_costs",),
    "approve_budget": ("view_project_costs",),
    "approve_quantity_increase": ("view_project_costs",),
    "agree_project_variance": ("view_project_costs", "view_prices"),
    "edit_prices": ("view_prices",), "view_margins": ("view_prices",),
    "raise_po": ("view_prices",), "approve_po": ("view_prices",),
    "raise_work_order": ("view_prices",), "approve_work_order": ("view_prices",),
    "agree_stock_variance": ("view_prices",), "approve_ticket_cost": ("view_prices",),
    "manage_quotations": ("view_prices",), "view_finance": ("view_prices",),
    "manage_finance": ("view_prices",),
}


def mirror_prices(apps, schema_editor):
    Role = apps.get_model("accounts", "RoleDefinition")
    Override = apps.get_model("accounts", "UserCapability")
    for role in Role.objects.all():
        caps = set(role.capabilities or ())
        if "view_prices" in caps:
            caps.add("view_project_costs")
        # And whatever a held right cannot be used without, so the grid
        # shows what the role really has.
        for key in list(caps):
            caps.update(REQUIRES.get(key, ()))
        if caps != set(role.capabilities or ()):
            role.capabilities = sorted(caps)
            role.save(update_fields=["capabilities"])
    for row in Override.objects.filter(capability="view_prices"):
        Override.objects.get_or_create(
            user_id=row.user_id, capability="view_project_costs",
            defaults={"allowed": row.allowed, "granted_by_id": row.granted_by_id,
                      "reason": row.reason or "Follows See prices and costs"},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0015_menu_order"),
    ]

    operations = [migrations.RunPython(mirror_prices, migrations.RunPython.noop)]
