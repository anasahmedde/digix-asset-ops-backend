"""What a person may do, as a list of named capabilities.

A role is a starting point, not a straitjacket. Every role comes with a set
of capabilities, and any one of them can be granted or withdrawn for a
particular person — so the Store Supervisor who also prices deliveries gets
``view_prices`` without being made a manager, and the new technician can be
kept off ``close_ticket`` for their first month.

Two rules hold the design together:

* A capability answers "may this person do this thing?", never "who are
  they?". Reporting lines and job titles live on the user; rights live here.
* The role default is always visible next to the override, so nobody has to
  guess why somebody can do something.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    module: str
    description: str
    # A capability that hands over real authority — money, deletion, other
    # people's accounts. Shown with a warning and never a role default for
    # the junior tiers.
    sensitive: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    # --- Money -------------------------------------------------------------
    Capability("view_prices", "See prices and costs", "Money",
               "Unit costs, line totals, order values and stock valuations. Without this, "
               "figures are hidden wherever they appear."),
    Capability("edit_prices", "Set prices", "Money",
               "Type the price on a purchase order, quotation or cost plan.", sensitive=True),
    Capability("approve_spend", "Approve spending", "Money",
               "Sign off purchase orders, budgets and payments.", sensitive=True),
    Capability("view_margins", "See margins and profitability", "Money",
               "Quoted price against actual cost, project profitability.", sensitive=True),

    # --- Procurement -------------------------------------------------------
    Capability("raise_po", "Raise purchase orders", "Procurement",
               "Create a draft order and send it for approval."),
    Capability("manage_suppliers", "Manage vendors", "Procurement",
               "Add and edit vendor records and their terms."),
    Capability("receive_goods", "Receive deliveries", "Procurement",
               "Book goods in against an order and record what arrived."),
    Capability("inspect_goods", "Inspect deliveries", "Procurement",
               "Pass or reject what was received, and route it into stock."),

    # --- Inventory ---------------------------------------------------------
    Capability("view_stock", "See stock levels", "Inventory",
               "Quantities on hand, by component and by store."),
    Capability("issue_stock", "Issue material", "Inventory",
               "Hand material out against a request."),
    Capability("adjust_stock", "Adjust stock", "Inventory",
               "Correct a quantity outside the normal movements.", sensitive=True),

    # --- Assets ------------------------------------------------------------
    Capability("view_assets", "See the asset registry", "Assets",
               "The register, each asset's detail and its history."),
    Capability("edit_assets", "Register and edit assets", "Assets",
               "Add an asset, change its details, define its components."),
    Capability("move_asset_stage", "Move an asset's lifecycle", "Assets",
               "Hand an asset to the client, decommission it, take it out of service."),
    Capability("delete_assets", "Delete assets", "Assets",
               "Remove an asset from the registry for good.", sensitive=True),

    # --- Projects & installation -------------------------------------------
    Capability("view_projects", "See projects", "Projects",
               "Project list, scope, progress and sites."),
    Capability("edit_projects", "Run projects", "Projects",
               "Create projects, set scope, assign managers and target dates."),
    Capability("edit_installation", "Work installations", "Installation",
               "Update installation steps, flag delays, complete a checklist."),
    Capability("close_installation", "Hand over an installation", "Installation",
               "Record the client's acceptance and take the asset live."),

    # --- Maintenance & tickets ---------------------------------------------
    Capability("view_tickets", "See tickets", "Tickets",
               "The ticket queue and each ticket's history."),
    Capability("work_tickets", "Work tickets", "Tickets",
               "Pick up a ticket, update it, submit it for review."),
    Capability("close_ticket", "Close tickets", "Tickets",
               "Resolve and close a ticket somebody else worked."),
    Capability("manage_maintenance", "Schedule maintenance", "Maintenance",
               "Create schedules, assign visits, complete rounds."),

    # --- Warranty ------------------------------------------------------------
    Capability("view_warranties", "See warranty cover", "Warranty",
               "Vendor and client warranties, what is covered and when it runs out."),
    Capability("manage_warranties", "Record warranty cover", "Warranty",
               "Add cover, extend it, reissue it and work claims."),

    # --- Commercial ---------------------------------------------------------
    Capability("view_clients", "See clients", "Commercial",
               "Client records and the sites that belong to them."),
    Capability("manage_quotations", "Work quotations", "Commercial",
               "Draft, revise and send quotations."),

    # --- People and the system ---------------------------------------------
    Capability("view_attendance", "See attendance", "People",
               "Registers, who is in, and corrections."),
    Capability("view_team", "See the team", "People",
               "The employee list and the organogram."),
    Capability("manage_team", "Manage people", "People",
               "Add people, change their role, set their reporting line.", sensitive=True),
    Capability("manage_permissions", "Change what people may do", "People",
               "Grant and withdraw the capabilities on this list.", sensitive=True),
    Capability("manage_setup", "Configure the system", "System",
               "Reference data, asset types, routes and templates.", sensitive=True),
    Capability("view_reports", "See reports", "System",
               "Dashboards, analytics and exports."),
)

BY_KEY = {c.key: c for c in CAPABILITIES}
ALL_KEYS = frozenset(BY_KEY)

# The order modules appear in, so the screen reads the same as this file.
MODULES = tuple(dict.fromkeys(c.module for c in CAPABILITIES))


def _keys(*names: str) -> frozenset[str]:
    unknown = set(names) - ALL_KEYS
    assert not unknown, f"unknown capability: {sorted(unknown)}"
    return frozenset(names)


# What each role can do before anybody adjusts it. These mirror the signed
# authority matrix: the Group Head has everything, the field has its own work
# and nothing else, and prices are a management concern by default.
ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    "super_admin": ALL_KEYS,
    "group_head": ALL_KEYS,
    "ops_manager": _keys(
        "view_prices", "edit_prices", "raise_po", "manage_suppliers", "receive_goods",
        "inspect_goods", "view_stock", "issue_stock", "view_assets", "edit_assets",
        "move_asset_stage", "view_projects", "edit_projects", "edit_installation",
        "close_installation", "view_tickets", "work_tickets", "close_ticket",
        "manage_maintenance", "view_clients", "view_team", "view_reports",
        "manage_setup", "manage_team", "manage_permissions",
        "view_warranties", "manage_warranties", "view_attendance",
    ),
    "marketing_head": _keys(
        "view_prices", "edit_prices", "view_assets", "view_projects", "view_tickets",
        "work_tickets", "close_ticket", "view_clients", "manage_quotations",
        "view_team", "view_reports", "manage_permissions",
        "view_warranties", "manage_warranties",
    ),
    "finance": _keys(
        "view_prices", "edit_prices", "view_margins", "view_stock", "view_assets",
        "view_projects", "view_clients", "view_reports", "view_team",
        "view_warranties",
    ),
    "supervisor": _keys(
        "view_stock", "inspect_goods", "view_assets", "edit_assets",
        "view_projects", "edit_installation", "view_tickets", "work_tickets",
        "close_ticket", "manage_maintenance", "view_team", "manage_permissions",
        "view_attendance",
    ),
    "warehouse": _keys(
        "receive_goods", "inspect_goods", "view_stock", "issue_stock",
        "view_assets", "view_tickets", "view_team",
    ),
    "marketing": _keys(
        "view_assets", "view_projects", "view_tickets", "work_tickets",
        "view_clients", "manage_quotations", "view_team", "view_warranties",
    ),
    "technician": _keys(
        "view_assets", "edit_installation", "view_tickets", "work_tickets",
        "view_team",
    ),
    # A client portal login is scoped to its own client by `for_client`,
    # so `view_clients` here means "your own record", not "the client
    # list". Without it the Clients entry in their menu leads to a 403.
    "client_viewer": _keys("view_assets", "view_tickets", "view_clients"),
    # Scoped by `DeviceViewSet.get_queryset` to devices on their own
    # tickets and installations, so this is "your own work", not the
    # register.
    "vendor": _keys("view_tickets", "view_assets"),
}


def defaults_for(role: str) -> frozenset[str]:
    return ROLE_DEFAULTS.get(role, frozenset())


def catalogue() -> list[dict]:
    """The list as the settings screen needs it."""
    return [
        {
            "key": c.key,
            "label": c.label,
            "module": c.module,
            "description": c.description,
            "sensitive": c.sensitive,
        }
        for c in CAPABILITIES
    ]
