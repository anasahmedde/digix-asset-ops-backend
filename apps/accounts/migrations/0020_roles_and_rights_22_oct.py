"""The client's Roles & Rights document (22 Oct 2026), applied to the roles.

- Warranty rights split: client cover is Marketing's, vendor cover Operations'.
  The old "See warranty cover" / "Record warranty cover" become the client and
  vendor rights each built-in role should have; a custom role keeps both.
- Budget and planning: Operations (not the Group Head).
- Inspect deliveries: the Supervisor (not the store, Operations or Group Head).
- GRN / receive into stock: the store (not Operations or Group Head).
- Client quotations: Marketing (not the Group Head).
- Attendance: own team; every team only for the Administrator.
- Prices: working quotations no longer forces "See prices and costs" on, so
  Marketing loses it; the price rights are given separately.
Every tick can still be changed in Roles & Rights afterwards.
"""
from django.db import migrations

OLD = {"view_warranties", "manage_warranties"}

# role key -> (add, remove); the old warranty keys are always removed.
PLAN = {
    "super_admin": ({"view_client_warranties", "view_vendor_warranties", "manage_client_warranties",
                     "manage_vendor_warranties", "view_all_attendance"}, set()),
    "group_head": ({"view_client_warranties", "view_vendor_warranties"},
                   {"plan_budget", "inspect_goods", "receive_goods", "manage_quotations",
                    "manage_client_warranties", "manage_vendor_warranties", "view_all_attendance"}),
    "ops_manager": ({"view_vendor_warranties", "manage_vendor_warranties", "plan_budget"},
                    {"manage_quotations", "view_client_warranties", "manage_client_warranties",
                     "record_client_warranty", "inspect_goods", "receive_goods", "view_all_attendance"}),
    "marketing_head": ({"view_client_warranties", "manage_client_warranties", "view_attendance"},
                       {"view_vendor_warranties", "manage_vendor_warranties"}),
    # Quotations no longer force every price open: price rights are the client's to give.
    "marketing": ({"view_client_warranties", "manage_client_warranties", "record_client_warranty"},
                  {"view_vendor_warranties", "manage_vendor_warranties", "view_prices"}),
    "finance": ({"view_client_warranties", "view_vendor_warranties"}, set()),
    "supervisor": ({"view_vendor_warranties", "inspect_goods"}, set()),
    "warehouse": ({"view_vendor_warranties", "receive_goods"}, {"inspect_goods"}),
    "technician": ({"view_vendor_warranties"}, set()),
    "client_viewer": ({"view_client_warranties"}, {"view_vendor_warranties"}),
}

# The map as it stood when this ran (live: apps.accounts.capabilities.REQUIRES).
REQUIRES = {
    "view_project_costs": ("view_projects",), "edit_projects": ("view_projects",),
    "plan_budget": ("view_projects", "view_project_costs"), "approve_budget": ("view_projects", "view_project_costs"),
    "decide_requirements": ("view_projects",), "approve_quantity_increase": ("view_projects", "view_project_costs"),
    "agree_project_variance": ("view_projects", "view_project_costs", "view_prices"),
    "record_client_warranty": ("view_projects",), "undo_requirement_decisions": ("view_projects",),
    "raise_po": ("view_prices", "view_procurement", "view_suppliers", "view_stock"),
    "approve_po": ("view_prices", "view_procurement"), "cancel_po": ("view_procurement",),
    "inspect_goods": ("view_stock",), "manage_suppliers": ("view_suppliers",),
    "raise_work_order": ("view_prices", "view_work_orders", "view_suppliers"),
    "approve_work_order": ("view_prices", "view_work_orders"), "inspect_work": ("view_work_orders",),
    "edit_prices": ("view_prices",), "view_margins": ("view_prices",),
    "agree_stock_variance": ("view_prices",), "approve_ticket_cost": ("view_prices",),
    "manage_quotations": ("view_quotations",),
    "manage_client_warranties": ("view_client_warranties",),
    "manage_vendor_warranties": ("view_vendor_warranties",),
    "raise_claim": ("view_vendor_warranties",), "decide_claim": ("view_vendor_warranties",),
    "view_all_attendance": ("view_attendance",),
    "view_finance": ("view_prices",), "manage_finance": ("view_prices",),
}


def close_over(caps):
    todo = list(caps)
    while todo:
        for need in REQUIRES.get(todo.pop(), ()):
            if need not in caps:
                caps.add(need)
                todo.append(need)
    return caps


def apply(apps, schema_editor):
    Role = apps.get_model("accounts", "RoleDefinition")
    Override = apps.get_model("accounts", "UserCapability")
    for role in Role.objects.all():
        caps = set(role.capabilities or ())
        had_view, had_manage = "view_warranties" in caps, "manage_warranties" in caps
        caps -= OLD
        if role.is_builtin and role.key in PLAN:
            add, remove = PLAN[role.key]
            caps = (caps | add) - remove
        else:
            # A role the client made: it keeps what it had, on both sides.
            if had_view:
                caps |= {"view_client_warranties", "view_vendor_warranties"}
            if had_manage:
                caps |= {"manage_client_warranties", "manage_vendor_warranties"}
        caps = close_over(caps)
        if caps != set(role.capabilities or ()):
            role.capabilities = sorted(caps)
            role.save(update_fields=["capabilities"])
    # A per-person adjustment on an old warranty right covers both new ones.
    for row in Override.objects.filter(capability__in=OLD):
        side = row.capability.split("_", 1)[0]  # view / manage
        for key in (f"{side}_client_warranties", f"{side}_vendor_warranties"):
            Override.objects.get_or_create(user_id=row.user_id, capability=key, defaults={"allowed": row.allowed})
        row.delete()


class Migration(migrations.Migration):

    dependencies = [("accounts", "0019_user_signature")]

    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
