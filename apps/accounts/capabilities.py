"""What a person may do, as a list of named capabilities.

A role is a starting point, not a straitjacket. Every role comes with a set
of capabilities, and any one of them can be granted or withdrawn for a
particular person - so the Store Supervisor who also prices deliveries gets
``view_prices`` without being made a manager, and the new technician can be
kept off ``close_ticket`` for their first month.

The rules that hold the design together:

* A capability answers "may this person do this thing?", never "who are
  they?". Reporting lines and job titles live on the user; rights live here.
* Every gate in the system is a capability. Nothing is decided by a role
  name, so a role written from scratch on the Roles screen is as real as a
  built-in one the moment its boxes are ticked.
* Each module reads the same way: see it, work it, decide it. Deciding -
  approving spend, signing budgets, agreeing a price - is marked
  ``sensitive`` and is never a default for the junior tiers.
* Two scopes sit beside the capabilities, because the organogram already
  answers them: a project's manager may decide the project's own matters
  (``PROJECT_MANAGER_CAPABILITIES``), and a reviewer decides for their own
  reports unless they also hold ``act_across_teams``.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    module: str
    description: str
    # See / work / approve / admin: how the screen groups a module's rows.
    kind: str = "work"
    # A capability that hands over real authority - money, deletion, other
    # people's accounts. Shown with a warning and never a role default for
    # the junior tiers.
    sensitive: bool = False


def _c(key, label, module, description, kind="work", sensitive=False):
    return Capability(key, label, module, description, kind, sensitive)


CAPABILITIES: tuple[Capability, ...] = (
    # --- Money ---------------------------------------------------------------
    _c("view_prices", "See prices and costs", "Money",
       "Unit costs, line totals, order values and stock valuations. Without this, "
       "figures are hidden wherever they appear.", "view"),
    _c("edit_prices", "Set prices", "Money",
       "Type the price on a purchase order, work order, quotation or cost plan.", "work", True),
    _c("view_margins", "See margins and profitability", "Money",
       "Quoted price against actual cost, project profitability.", "view", True),

    # --- Projects ------------------------------------------------------------
    _c("view_projects", "See projects", "Projects",
       "Project list, scope, progress, sites and costing.", "view"),
    _c("edit_projects", "Run projects", "Projects",
       "Create projects, set scope and sites, assign the manager and target dates."),
    _c("plan_budget", "Plan and submit budgets", "Projects",
       "Build the cost plan, add overheads, and send the budget up for approval."),
    _c("approve_budget", "Approve budgets", "Projects",
       "Sign a project's budget off, or send it back. Never your own submission.", "approve", True),
    _c("decide_requirements", "Decide how requirements are met", "Projects",
       "Take parts from stock, send them to procurement, choose in-house or vendor "
       "for each operation, and request more than was planned."),
    _c("approve_quantity_increase", "Approve quantity increases", "Projects",
       "Allow a project to need more of a part than the approved plan said.", "approve", True),
    _c("agree_project_variance", "Agree prices over the project plan", "Projects",
       "Accept a purchase or work-order price above what the project budgeted for.", "approve", True),
    _c("record_client_warranty", "Record the client warranty", "Projects",
       "Set the warranty term the project gives the client, per asset or for the order."),

    # --- Procurement ---------------------------------------------------------
    _c("view_procurement", "See purchase orders", "Procurement",
       "Purchase orders, procurement requests and goods receipts.", "view"),
    _c("raise_po", "Raise purchase orders", "Procurement",
       "Draft an order, send it for approval, record the order placed, cancel a draft."),
    _c("approve_po", "Approve purchase orders", "Procurement",
       "Sign a purchase order off - the signature that commits the company.", "approve", True),
    _c("cancel_po", "Cancel approved orders", "Procurement",
       "Cancel an order after it was approved, reopening what it was buying.", "approve", True),
    _c("inspect_goods", "Inspect deliveries", "Procurement",
       "Check a delivery against the order, accept or reject it, and write the GRN."),
    _c("view_suppliers", "See vendors", "Procurement",
       "Vendor records, contacts and terms.", "view"),
    _c("manage_suppliers", "Manage vendors", "Procurement",
       "Add and edit vendor records and their terms."),

    # --- Work orders ---------------------------------------------------------
    _c("view_work_orders", "See work orders", "Work Orders",
       "Work orders, the operations requested from vendors and work receiving.", "view"),
    _c("raise_work_order", "Raise work orders", "Work Orders",
       "Draft a work order for requested operations and send it for approval."),
    _c("approve_work_order", "Approve work orders", "Work Orders",
       "Sign a work order off.", "approve", True),
    _c("inspect_work", "Inspect delivered work", "Work Orders",
       "Accept delivered work or send it back for rework."),

    # --- Inventory -----------------------------------------------------------
    _c("view_stock", "See stock levels", "Inventory",
       "Quantities on hand, by component and by store.", "view"),
    _c("manage_stock", "Maintain the stock catalogue", "Inventory",
       "Open generic and unique components, set reorder levels, raise reorder requests."),
    _c("receive_goods", "Receive into stock", "Inventory",
       "Take inspected goods onto the shelf and record where they are kept."),
    _c("request_material", "Request material", "Inventory",
       "Ask the store for parts against a job or a project."),
    _c("issue_stock", "Issue material", "Inventory",
       "Hand material out against a request, naming who took it."),
    _c("cancel_material_request", "Cancel material requests", "Inventory",
       "Withdraw a request somebody else raised.", "approve", True),
    _c("agree_stock_variance", "Agree prices over the last paid", "Inventory",
       "Accept a stock purchase priced above the last price paid.", "approve", True),
    _c("adjust_stock", "Adjust stock", "Inventory",
       "Correct a quantity outside the normal movements.", "approve", True),

    # --- Assets --------------------------------------------------------------
    _c("view_assets", "See the asset registry", "Assets",
       "The register, each asset's detail and its history.", "view"),
    _c("edit_assets", "Register and edit assets", "Assets",
       "Add an asset, change its details, define its components and route."),
    _c("work_production", "Work the production floor", "Assets",
       "Start and finish in-house operations on an asset's route."),
    _c("move_asset_stage", "Move an asset's lifecycle", "Assets",
       "Hand an asset to the client, decommission it, take it out of service.", "approve"),
    _c("delete_assets", "Delete assets", "Assets",
       "Remove an asset from the registry for good.", "approve", True),

    # --- Installation --------------------------------------------------------
    _c("view_installations", "See installations", "Installation",
       "The installation tracker and each job's checklist.", "view"),
    _c("assign_installation", "Assign installations", "Installation",
       "Open the installation job and say who puts the asset in and by when."),
    _c("edit_installation", "Work installations", "Installation",
       "Update installation steps, flag delays, add photos, complete a checklist."),
    _c("activate_asset", "Activate assets", "Installation",
       "Mark an installed asset live, with the photo that proves it."),
    _c("close_installation", "Hand over an installation", "Installation",
       "Record the client's signed acceptance.", "approve"),

    # --- Tickets -------------------------------------------------------------
    _c("view_tickets", "See tickets", "Tickets",
       "The ticket queue and each ticket's history.", "view"),
    _c("raise_ticket", "Raise tickets", "Tickets",
       "Report a fault or a request against an asset or site."),
    _c("assign_ticket", "Assign tickets", "Tickets",
       "Give a ticket to a technician or a vendor, and reopen a closed one."),
    _c("work_tickets", "Work tickets", "Tickets",
       "Pick up a ticket, update it, submit it for review."),
    _c("review_tickets", "Review completed work", "Tickets",
       "Accept or send back work a technician submitted. For your own reports, "
       "unless you act across teams.", "approve"),
    _c("approve_ticket_cost", "Approve ticket costs", "Tickets",
       "Decide a ticket waiting on Operations' approval of the cost.", "approve", True),
    _c("relay_client_decision", "Relay the client's decision", "Tickets",
       "Record whether the client agreed to billable work.", "approve"),
    _c("close_ticket", "Close tickets", "Tickets",
       "Resolve and close a ticket somebody else worked.", "approve"),

    # --- Maintenance ---------------------------------------------------------
    _c("view_maintenance", "See maintenance", "Maintenance",
       "Schedules, rounds and visits.", "view"),
    _c("manage_maintenance", "Schedule maintenance", "Maintenance",
       "Create and edit schedules and their rounds."),
    _c("assign_maintenance", "Assign maintenance", "Maintenance",
       "Give a round or a corrective job to a technician, with a date."),
    _c("work_maintenance", "Work maintenance", "Maintenance",
       "Start a visit, record what was done, ask for parts."),
    _c("review_maintenance", "Review maintenance", "Maintenance",
       "Accept a round or send it back, and decide the parts a technician asked for. "
       "For your own reports, unless you act across teams.", "approve"),

    # --- Warranty ------------------------------------------------------------
    _c("view_warranties", "See warranty cover", "Warranty",
       "Client, vendor and component warranties, and claims.", "view"),
    _c("manage_warranties", "Record warranty cover", "Warranty",
       "Add vendor and component cover, extend it, reissue it."),
    _c("raise_claim", "Raise warranty claims", "Warranty",
       "Claim on a vendor's cover for a failed asset or part."),
    _c("decide_claim", "Work warranty claims", "Warranty",
       "Send a claim to the vendor, record their decision and settle it.", "approve"),

    # --- Commercial ----------------------------------------------------------
    _c("view_clients", "See clients and sites", "Commercial",
       "Client records and the sites that belong to them.", "view"),
    _c("manage_clients", "Manage clients", "Commercial",
       "Add and edit client records."),
    _c("manage_sites", "Manage sites", "Commercial",
       "Add and edit sites, zones and site contacts."),
    _c("view_quotations", "See quotations", "Commercial",
       "Quotations and what became of them.", "view"),
    _c("manage_quotations", "Work quotations", "Commercial",
       "Draft, revise, send and accept quotations."),

    # --- Finance -------------------------------------------------------------
    _c("view_finance", "See invoices and payments", "Finance",
       "Invoices raised and received, and what has been paid.", "view"),
    _c("manage_finance", "Record invoices and payments", "Finance",
       "Raise invoices and record payments against them."),

    # --- People --------------------------------------------------------------
    _c("view_attendance", "See attendance", "People",
       "Registers, who is in, and corrections.", "view"),
    _c("manage_attendance", "Correct attendance", "People",
       "Add or correct attendance records for other people."),
    _c("view_team", "See the team", "People",
       "The employee list and the organogram.", "view"),
    _c("manage_team", "Manage people", "People",
       "Change a person's role, job details and reporting line.", "admin", True),
    _c("manage_logins", "Create and remove logins", "People",
       "Add accounts, remove them, and reset passwords.", "admin", True),
    _c("manage_permissions", "Change what people may do", "People",
       "Grant and withdraw the capabilities on this list, and edit roles.", "admin", True),
    _c("act_across_teams", "Act across teams", "People",
       "Review, decide and adjust for everybody, not only for the people who "
       "report to you.", "admin", True),

    # --- System --------------------------------------------------------------
    _c("manage_setup", "Configure the system", "System",
       "Reference data, numbering, asset types, routes and templates.", "admin", True),
    _c("view_reports", "See reports", "System",
       "Dashboards, analytics and exports.", "view"),
    _c("export_data", "Export to Excel", "System",
       "Download lists and reports."),
    _c("receive_alerts", "Receive system alerts", "System",
       "Be told about overdue tickets, low stock, lapsing warranties and escalations."),
    _c("delete_records", "Delete records", "System",
       "Remove tickets, orders, quotations and other records you may edit.", "admin", True),
)

BY_KEY = {c.key: c for c in CAPABILITIES}
ALL_KEYS = frozenset(BY_KEY)

# The order modules appear in, so the screen reads the same as this file.
MODULES = tuple(dict.fromkeys(c.module for c in CAPABILITIES))
KINDS = ("view", "work", "approve", "admin")

# Capabilities that were split or renamed. An old key stored on a role or a
# person is read as the new ones, and the data migration rewrites it.
RENAMED: dict[str, tuple[str, ...]] = {
    "approve_spend": ("approve_po", "approve_work_order", "approve_budget"),
}


def _keys(*names: str) -> frozenset[str]:
    unknown = set(names) - ALL_KEYS
    assert not unknown, f"unknown capability: {sorted(unknown)}"
    return frozenset(names)


_VIEW_ALL = _keys(*(c.key for c in CAPABILITIES if c.kind == "view" and not c.sensitive))

# What each role can do before anybody adjusts it. These follow the signed
# authority matrix: the Group Head has everything, Operations runs the work
# but the Group Head signs the money off, the field has its own work and
# nothing else, and prices are a management concern by default.
ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    "super_admin": ALL_KEYS,
    # Signs the money off; Operations raise the orders. Logins are the
    # Super Admin's (authority matrix, gate 9).
    "group_head": ALL_KEYS - _keys("manage_logins", "raise_po", "raise_work_order"),
    # Runs assets, projects, sites, procurement and maintenance; the three
    # signatures (budget, PO, WO) and logins stay above.
    "ops_manager": ALL_KEYS - _keys(
        "approve_budget", "approve_po", "approve_work_order", "manage_logins",
    ),
    "marketing_head": _VIEW_ALL | _keys(
        "edit_prices", "raise_ticket", "work_tickets", "relay_client_decision", "close_ticket",
        "manage_clients", "manage_sites", "manage_quotations", "manage_warranties",
        "record_client_warranty", "manage_permissions", "act_across_teams", "export_data",
        "receive_alerts",
    ),
    "marketing": _keys(
        "view_assets", "view_projects", "view_installations", "view_tickets", "raise_ticket",
        "work_tickets", "relay_client_decision", "view_clients", "view_quotations",
        "manage_quotations", "view_team", "view_warranties",
    ),
    "finance": _VIEW_ALL | _keys(
        "edit_prices", "view_margins", "raise_po", "view_finance", "manage_finance", "export_data",
    ),
    "supervisor": _keys(
        "view_stock", "request_material", "inspect_goods", "view_procurement", "view_work_orders",
        "view_assets", "edit_assets", "work_production", "view_projects", "decide_requirements",
        "agree_project_variance", "view_installations", "edit_installation", "activate_asset",
        "close_installation", "view_tickets", "raise_ticket", "work_tickets", "review_tickets",
        "close_ticket", "view_maintenance", "manage_maintenance", "assign_maintenance",
        "work_maintenance", "review_maintenance", "view_warranties", "raise_claim",
        "view_team", "manage_permissions", "view_attendance", "view_clients",
    ),
    "warehouse": _keys(
        "view_stock", "manage_stock", "receive_goods", "inspect_goods", "request_material",
        "issue_stock", "agree_stock_variance", "view_procurement", "view_suppliers",
        # The store moves assets into stock, dispatches them and handles RMAs.
        "view_assets", "move_asset_stage", "view_tickets", "view_warranties", "raise_claim", "view_team",
    ),
    "technician": _keys(
        # Asking for a part means naming one, so the person asking has to be
        # able to see what the store carries. It is a list of materials, not
        # of money - prices are `view_prices`, which they do not have.
        "view_stock", "request_material",
        "view_assets", "work_production", "view_installations", "edit_installation",
        "activate_asset", "view_tickets", "raise_ticket", "work_tickets",
        "view_maintenance", "work_maintenance", "view_team", "view_attendance",
        # Whether the part being replaced is still under the vendor's warranty.
        "view_warranties",
    ),
    # A client portal login is scoped to its own client by `for_client`,
    # so `view_clients` here means "your own record", not "the client list".
    "client_viewer": _keys(
        "view_assets", "view_projects", "view_installations", "view_tickets", "raise_ticket",
        "view_clients", "view_warranties",
    ),
    # Scoped by the viewsets to the tickets and installations given to their
    # own supplier, so this is "your own work", not the register.
    "vendor": _keys("view_tickets", "work_tickets", "view_assets", "view_installations", "edit_installation"),
}

# What a project's manager may do on their own project without holding the
# capability outright: the organogram made them answerable for it.
PROJECT_MANAGER_CAPABILITIES = _keys(
    "view_projects", "edit_projects", "plan_budget", "decide_requirements",
    "approve_quantity_increase", "agree_project_variance", "record_client_warranty",
    "assign_installation", "view_procurement", "view_work_orders", "view_installations",
)


def defaults_for(role: str) -> frozenset[str]:
    return ROLE_DEFAULTS.get(role, frozenset())


def expand(keys) -> frozenset[str]:
    """Read a stored list, translating any key that has since been renamed."""
    out = set()
    for key in keys or ():
        out.update(RENAMED.get(key, (key,)))
    return frozenset(out & ALL_KEYS)


def catalogue() -> list[dict]:
    """The list as the settings screen needs it."""
    return [
        {
            "key": c.key,
            "label": c.label,
            "module": c.module,
            "kind": c.kind,
            "description": c.description,
            "sensitive": c.sensitive,
        }
        for c in CAPABILITIES
    ]
