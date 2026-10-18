"""Every project right brings See projects; the money ones See project costs.

Roles are saved with the rights their rights cannot be used without, so the
Roles & Rights grid shows what each role really has.
"""
from django.db import migrations

# The map as it stood when this ran (live: apps.accounts.capabilities.REQUIRES).
REQUIRES = {
    "view_project_costs": ("view_projects",),
    "edit_projects": ("view_projects",),
    "plan_budget": ("view_projects", "view_project_costs"),
    "approve_budget": ("view_projects", "view_project_costs"),
    "decide_requirements": ("view_projects",),
    "approve_quantity_increase": ("view_projects", "view_project_costs"),
    "agree_project_variance": ("view_projects", "view_project_costs", "view_prices"),
    "record_client_warranty": ("view_projects",),
    "edit_prices": ("view_prices",), "view_margins": ("view_prices",),
    "raise_po": ("view_prices",), "approve_po": ("view_prices",),
    "raise_work_order": ("view_prices",), "approve_work_order": ("view_prices",),
    "agree_stock_variance": ("view_prices",), "approve_ticket_cost": ("view_prices",),
    "manage_quotations": ("view_prices",), "view_finance": ("view_prices",),
    "manage_finance": ("view_prices",),
}


def close_over(apps, schema_editor):
    Role = apps.get_model("accounts", "RoleDefinition")
    for role in Role.objects.all():
        caps = set(role.capabilities or ())
        todo = list(caps)
        while todo:
            for need in REQUIRES.get(todo.pop(), ()):
                if need not in caps:
                    caps.add(need)
                    todo.append(need)
        if caps != set(role.capabilities or ()):
            role.capabilities = sorted(caps)
            role.save(update_fields=["capabilities"])


class Migration(migrations.Migration):

    dependencies = [("accounts", "0016_see_project_costs")]

    operations = [migrations.RunPython(close_over, migrations.RunPython.noop)]
