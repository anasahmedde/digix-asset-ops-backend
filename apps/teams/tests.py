# Tests will be added alongside model implementations.
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import (
    AssetComponent,
    Brand,
    Device,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
)
from apps.inventory.models import InventoryItem, Issuance, StockMovement
from apps.teams.models import BOMAllocation, Project, ProjectBOMLine


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_project_scope_and_milestones():
    ops = User.objects.create_user(username="team-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="TeamBrand")
    dm = DeviceModel.objects.create(brand=brand, name="T-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-TEAM-1", serial_number="TEAM-1")
    comp = AssetComponent.objects.create(device=device, name="SMD Module", quantity=10)
    other_device = Device.objects.create(device_model=dm, asset_code="AST-TEAM-2", serial_number="TEAM-2")
    project = Project.objects.create(name="Mall rollout", phase="production")
    c = _client(ops)

    r = c.post("/api/teams/scope-items/", {
        "project": str(project.pk),
        "device": str(device.pk),
        "component": str(comp.pk),
        "quantity": 4,
        "start_date": "2026-09-01",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["component_name"] == "SMD Module"

    # component from a different asset is rejected
    r = c.post("/api/teams/scope-items/", {
        "project": str(project.pk),
        "device": str(other_device.pk),
        "component": str(comp.pk),
    }, format="json")
    assert r.status_code == 400

    r = c.post("/api/teams/milestones/", {
        "project": str(project.pk), "title": "Structures ready", "due_date": "2026-09-15", "order": 1,
    }, format="json")
    assert r.status_code == 201, r.content

    detail = c.get(f"/api/teams/projects/{project.pk}/")
    # The phase follows the work, not the value the project was created with:
    # nothing has been budgeted or built, so it reads as still being planned
    # however it was set by hand.
    assert detail.data["phase"] == "planning"
    assert detail.data["phase_display"] == "Planning"
    assert len(detail.data["scope_items"]) == 1
    assert len(detail.data["milestones"]) == 1

    # The off-ramps are the ones somebody still chooses.
    r = c.patch(f"/api/teams/projects/{project.pk}/", {"phase": "on_hold"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["phase"] == "on_hold"


@pytest.mark.django_db
def test_contract_type_round_trip_and_filter():
    ops = User.objects.create_user(username="team-ops3", password="x", role="ops_manager")
    c = _client(ops)

    # rental project with an end date round-trips through create + detail
    r = c.post("/api/teams/projects/", {
        "name": "Airport screens",
        "contract_type": "rental",
        "rental_end_date": "2027-06-30",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["contract_type"] == "rental"
    assert r.data["contract_type_display"] == "Rental"
    assert r.data["rental_end_date"] == "2027-06-30"

    detail = c.get(f"/api/teams/projects/{r.data['id']}/")
    assert detail.data["contract_type"] == "rental"
    assert detail.data["rental_end_date"] == "2027-06-30"

    # contract type is optional and defaults to blank
    sold = Project.objects.create(name="Mall facade", contract_type="sold")
    plain = Project.objects.create(name="Unclassified")
    assert plain.contract_type == ""

    # list filter narrows by contract_type
    rentals = c.get("/api/teams/projects/", {"contract_type": "rental"})
    assert rentals.status_code == 200, rentals.content
    names = {p["name"] for p in rentals.data["results"]}
    assert names == {"Airport screens"}

    solds = c.get("/api/teams/projects/", {"contract_type": "sold"})
    assert {p["name"] for p in solds.data["results"]} == {sold.name}
    assert rentals.data["results"][0]["contract_type_display"] == "Rental"


@pytest.mark.django_db
def test_progress_is_computed():
    ops = User.objects.create_user(username="team-ops2", password="x", role="ops_manager")
    from apps.teams.models import ProjectMilestone

    c = _client(ops)
    # No milestones: progress is the five phase bars averaged, and a project
    # with nothing on it has done none of the work — whatever phase somebody
    # has set it to by hand.
    p = Project.objects.create(name="Ladder", phase="production")
    assert c.get(f"/api/teams/projects/{p.pk}/").data["progress"] == 0

    # milestones override the phases: 2 of 4 done -> 50
    for i in range(4):
        ProjectMilestone.objects.create(project=p, title=f"M{i}", order=i)
    for m in list(p.milestones.all())[:2]:
        m.completed_at = timezone.now()
        m.save()
    assert c.get(f"/api/teams/projects/{p.pk}/").data["progress"] == 50

    # completed project with no milestones -> 100
    done = Project.objects.create(name="Done", status="completed", phase="handover")
    assert c.get(f"/api/teams/projects/{done.pk}/").data["progress"] == 100


# ── Wave 2: BOM allocation & issuance (WF-02) ────────────────────────

@pytest.fixture
def bom_setup(db):
    ops = User.objects.create_user(username="bom-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="BOMBrand")
    dm = DeviceModel.objects.create(brand=brand, name="BOM-1")
    material = MaterialType.objects.create(name="BOM Cable", unit="meter")
    item = InventoryItem.objects.create(material_type=material, quantity=10, unit_cost=5)
    project = Project.objects.create(name="BOM Project")
    return {"ops": ops, "dm": dm, "item": item, "project": project}


@pytest.mark.django_db
def test_allocate_device_flips_status_and_journals(bom_setup):
    dm, project, ops = bom_setup["dm"], bom_setup["project"], bom_setup["ops"]
    device = Device.objects.create(
        device_model=dm, asset_code="AST-BOM-1", serial_number="BOM-SN-1", status="in_stock",
    )
    line = ProjectBOMLine.objects.create(
        project=project, device_model=dm, description="SMD Screen", quantity=2, unit_price=100,
    )
    c = _client(ops)

    r = c.post(f"/api/teams/bom-lines/{line.pk}/allocate/", {"device": str(device.pk)}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["allocated_quantity"] == 1
    assert r.data["issued_quantity"] == 0
    assert r.data["shortage"] == 1
    assert len(r.data["allocations"]) == 1
    assert r.data["allocations"][0]["device_code"] == "AST-BOM-1"
    assert r.data["allocations"][0]["status"] == "allocated"

    device.refresh_from_db()
    assert device.status == "assigned"
    assert device.project_id == project.pk

    # The Wave-1 machine journalled the flip with reason + user.
    event = DeviceLifecycleEvent.objects.get(
        device=device, from_value="in_stock", to_value="assigned"
    )
    assert event.description == "Allocated to project BOM Project"
    assert event.performed_by == ops

    # A device that is not in stock is rejected.
    r = c.post(f"/api/teams/bom-lines/{line.pk}/allocate/", {"device": str(device.pk)}, format="json")
    assert r.status_code == 400
    assert line.allocations.count() == 1


@pytest.mark.django_db
def test_allocate_stock_guards_over_allocation_across_lines(bom_setup):
    project, item, ops = bom_setup["project"], bom_setup["item"], bom_setup["ops"]
    line1 = ProjectBOMLine.objects.create(project=project, description="Cable A", quantity=8)
    line2 = ProjectBOMLine.objects.create(project=project, description="Cable B", quantity=5)
    c = _client(ops)

    # quantity is mandatory for stock allocations
    r = c.post(f"/api/teams/bom-lines/{line1.pk}/allocate/", {"inventory_item": str(item.pk)}, format="json")
    assert r.status_code == 400

    # neither target given
    r = c.post(f"/api/teams/bom-lines/{line1.pk}/allocate/", {}, format="json")
    assert r.status_code == 400

    r = c.post(
        f"/api/teams/bom-lines/{line1.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 6}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["allocated_quantity"] == 6
    assert r.data["shortage"] == 2

    # 6 of 10 already reserved by line1 → only 4 left for ANY line
    r = c.post(
        f"/api/teams/bom-lines/{line2.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 5}, format="json",
    )
    assert r.status_code == 400

    r = c.post(
        f"/api/teams/bom-lines/{line2.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 4}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["allocated_quantity"] == 4


@pytest.mark.django_db
def test_issue_stock_allocation_decrements_and_journals(bom_setup):
    project, item, ops = bom_setup["project"], bom_setup["item"], bom_setup["ops"]
    line = ProjectBOMLine.objects.create(project=project, description="Cable", quantity=6)
    c = _client(ops)

    r = c.post(
        f"/api/teams/bom-lines/{line.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 6}, format="json",
    )
    assert r.status_code == 200, r.content
    alloc_id = r.data["allocations"][0]["id"]

    r = c.post(f"/api/teams/bom-lines/{line.pk}/issue/", {"allocation": alloc_id}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["allocated_quantity"] == 6
    assert r.data["issued_quantity"] == 6
    assert r.data["shortage"] == 0
    assert r.data["allocations"][0]["status"] == "issued"

    item.refresh_from_db()
    assert item.quantity == 4

    issuance = Issuance.objects.get(bom_line=line)
    assert issuance.issued_to_project_id == project.pk
    assert issuance.quantity == 6
    assert issuance.issued_by == ops
    movement = StockMovement.objects.get(item=item, movement_type="out")
    assert movement.quantity == 6
    assert movement.reference == issuance.issue_number

    # Issuing the same allocation twice is rejected.
    r = c.post(f"/api/teams/bom-lines/{line.pk}/issue/", {"allocation": alloc_id}, format="json")
    assert r.status_code == 400
    item.refresh_from_db()
    assert item.quantity == 4

    # Once issued, the reservation is released: the remaining 4 can be allocated.
    line2 = ProjectBOMLine.objects.create(project=project, description="More cable", quantity=4)
    r = c.post(
        f"/api/teams/bom-lines/{line2.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 4}, format="json",
    )
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_issue_device_allocation_is_rejected(bom_setup):
    dm, project, ops = bom_setup["dm"], bom_setup["project"], bom_setup["ops"]
    device = Device.objects.create(
        device_model=dm, asset_code="AST-BOM-2", serial_number="BOM-SN-2", status="in_stock",
    )
    line = ProjectBOMLine.objects.create(project=project, device_model=dm, description="Screen", quantity=1)
    c = _client(ops)

    r = c.post(f"/api/teams/bom-lines/{line.pk}/allocate/", {"device": str(device.pk)}, format="json")
    assert r.status_code == 200, r.content
    alloc_id = r.data["allocations"][0]["id"]

    r = c.post(f"/api/teams/bom-lines/{line.pk}/issue/", {"allocation": alloc_id}, format="json")
    assert r.status_code == 400
    assert "installation" in r.data["detail"]
    assert BOMAllocation.objects.get(pk=alloc_id).status == "allocated"


@pytest.mark.django_db
def test_bom_summary_totals_and_shortage_math(bom_setup):
    dm, project, item, ops = (
        bom_setup["dm"], bom_setup["project"], bom_setup["item"], bom_setup["ops"],
    )
    line1 = ProjectBOMLine.objects.create(project=project, description="Cable", quantity=5, unit_price=10)
    line2 = ProjectBOMLine.objects.create(project=project, device_model=dm, description="Screen", quantity=2, unit_price=500)
    device = Device.objects.create(
        device_model=dm, asset_code="AST-BOM-3", serial_number="BOM-SN-3", status="in_stock",
    )
    c = _client(ops)

    r = c.post(
        f"/api/teams/bom-lines/{line1.pk}/allocate/",
        {"inventory_item": str(item.pk), "quantity": 3}, format="json",
    )
    assert r.status_code == 200, r.content
    alloc_id = r.data["allocations"][0]["id"]
    assert c.post(
        f"/api/teams/bom-lines/{line1.pk}/issue/", {"allocation": alloc_id}, format="json"
    ).status_code == 200
    assert c.post(
        f"/api/teams/bom-lines/{line2.pk}/allocate/", {"device": str(device.pk)}, format="json"
    ).status_code == 200

    r = c.get(f"/api/teams/projects/{project.pk}/bom-summary/")
    assert r.status_code == 200, r.content
    by_desc = {row["description"]: row for row in r.data["lines"]}
    assert by_desc["Cable"]["quantity"] == 5
    assert by_desc["Cable"]["allocated_quantity"] == 3
    assert by_desc["Cable"]["issued_quantity"] == 3
    assert by_desc["Cable"]["shortage"] == 2
    assert by_desc["Screen"]["allocated_quantity"] == 1
    assert by_desc["Screen"]["issued_quantity"] == 0
    assert by_desc["Screen"]["shortage"] == 1
    assert r.data["totals"] == {"required": 7, "allocated": 4, "issued": 3, "shortage": 3}


@pytest.mark.django_db
def test_bom_lines_crud_and_project_filter(bom_setup):
    project, ops = bom_setup["project"], bom_setup["ops"]
    other = Project.objects.create(name="Other Project")
    ProjectBOMLine.objects.create(project=other, description="Elsewhere", quantity=1)
    c = _client(ops)

    r = c.post("/api/teams/bom-lines/", {
        "project": str(project.pk), "description": "Bracket", "quantity": 3, "unit_price": "12.50",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["shortage"] == 3

    r = c.get("/api/teams/bom-lines/", {"project": str(project.pk)})
    assert r.status_code == 200
    assert [row["description"] for row in r.data["results"]] == ["Bracket"]

    # quantity must stay positive
    r = c.post("/api/teams/bom-lines/", {
        "project": str(project.pk), "description": "Broken", "quantity": 0,
    }, format="json")
    assert r.status_code == 400


# ── Wave 2: quotation provenance (WF-01) ─────────────────────────────

@pytest.mark.django_db
def test_project_and_bom_line_link_back_to_quotation():
    from apps.clients.models import Client
    from apps.quotations.models import Quotation, QuotationItem

    customer = Client.objects.create(name="Provenance Client")
    quotation = Quotation.objects.create(title="Facade refresh", client=customer)
    q_item = QuotationItem.objects.create(
        quotation=quotation, description="Mesh screen", quantity=3, unit_price=750,
    )

    project = Project.objects.create(
        name="Project: Facade refresh", phase="planning",
        client=customer, source_quotation=quotation,
    )
    line = ProjectBOMLine.objects.create(
        project=project, description="Mesh screen", quantity=3, unit_price=750,
        source_quotation_item=q_item,
    )

    # forward + reverse accessors
    assert project.source_quotation == quotation
    assert quotation.spawned_projects.get() == project
    assert line.source_quotation_item == q_item
    assert q_item.bom_lines.get() == line

    # provenance is advisory: deleting the quotation nulls, never cascades
    quotation.delete()
    project.refresh_from_db()
    line.refresh_from_db()
    assert project.source_quotation is None
    assert line.source_quotation_item is None


# ---------------------------------------------------------------------------
# Project requirements gather assets linked EITHER way
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_requirements_include_assets_added_through_scope(db):
    """An asset added on the Scope screen must appear in the build plan."""
    from apps.accounts.models import User as _U
    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel, MaterialType
    from apps.inventory.models import InventoryItem
    from apps.teams.models import Project, ProjectScopeItem
    from rest_framework.test import APIClient

    admin = _U.objects.create_user(username="req-admin", password="x", role="super_admin")
    project = Project.objects.create(name="Scope Project")
    brand = Brand.objects.create(name="Scope Brand")
    model = DeviceModel.objects.create(brand=brand, name="SC-1")
    item = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Scope Part"), quantity=5
    )

    # One asset linked by its own project field...
    by_fk = Device.objects.create(device_model=model, serial_number="SC-FK", project=project)
    AssetComponent.objects.create(device=by_fk, name="Part A", quantity=2, inventory_item=item)

    # ...and one added through the Scope screen only.
    by_scope = Device.objects.create(device_model=model, serial_number="SC-SCOPE")
    ProjectScopeItem.objects.create(project=project, device=by_scope, quantity=1)
    AssetComponent.objects.create(device=by_scope, name="Part B", quantity=3, inventory_item=item)

    # An unrelated asset must not appear.
    Device.objects.create(device_model=model, serial_number="SC-OTHER")

    c = APIClient(); c.force_authenticate(admin)
    r = c.get(f"/api/teams/projects/{project.id}/requirements/")
    assert r.status_code == 200, r.content
    codes = {a["asset_code"] for a in r.data["assets"]}
    assert codes == {by_fk.asset_code, by_scope.asset_code}
    assert r.data["totals"]["required"] == 5


@pytest.mark.django_db
def test_requirements_do_not_duplicate_an_asset_linked_both_ways(db):
    from apps.accounts.models import User as _U
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.teams.models import Project, ProjectScopeItem
    from rest_framework.test import APIClient

    admin = _U.objects.create_user(username="req-admin2", password="x", role="super_admin")
    project = Project.objects.create(name="Both Ways Project")
    brand = Brand.objects.create(name="Both Brand")
    model = DeviceModel.objects.create(brand=brand, name="BW-1")
    device = Device.objects.create(device_model=model, serial_number="BW-1", project=project)
    # Each asset appears once per project: the link above already made its
    # scope row, so a second add is a no-op rather than a duplicate.
    ProjectScopeItem.objects.get_or_create(project=project, device=device, defaults={"quantity": 1})

    c = APIClient(); c.force_authenticate(admin)
    r = c.get(f"/api/teams/projects/{project.id}/requirements/")
    assert r.status_code == 200, r.content
    assert len(r.data["assets"]) == 1



# ---------------------------------------------------------------------------
# Planning then execution: estimate, approve, and only then draw stock
# ---------------------------------------------------------------------------
def _planned_project():
    from decimal import Decimal

    from django.utils import timezone as _tz

    from apps.assets.models import AssetComponent, Device, MaterialType
    from apps.inventory.models import InventoryItem, InventoryUnitType
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier
    from apps.teams.models import Project

    project = Project.objects.create(name="Planned Wall")
    device = Device.objects.create(asset_code="AST-PLAN-1", serial_number="PLAN-1", project=project)

    cable = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Plan Cable"), quantity=100,
        unit_cost=Decimal("100.00"),
    )
    player = InventoryUnitType.objects.create(name="Plan Player", unit_cost=Decimal("50.00"))

    # The cable has been bought before, at a different price than it was opened with.
    po = PurchaseOrder.objects.create(
        supplier=Supplier.objects.create(name="Plan Supplier"), order_date=_tz.localdate(),
    )
    PurchaseOrderItem.objects.create(
        purchase_order=po, inventory_item=cable, description="cable", quantity=10,
        unit_price=Decimal("120.00"), received_quantity=10,
    )

    AssetComponent.objects.create(device=device, name="Plan Cable", quantity=4, inventory_item=cable)
    AssetComponent.objects.create(device=device, name="Plan Player", quantity=2, inventory_unit_type=player)
    return project, device, cable, po


@pytest.mark.django_db
def test_plan_prices_materials_and_adds_overheads_and_contingency(api_client_factory=None):
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U

    project, device, cable, po = _planned_project()
    ops = _U.objects.create_user(username="plan-ops", password="x", role="ops_manager")
    c = _C()
    c.force_authenticate(ops)

    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()
    by_name = {m["name"]: m for m in plan["materials"]}
    # Bought before: priced at the last procured price, not the opening cost.
    assert Decimal(by_name["Plan Cable"]["unit_price"]) == Decimal("120.00")
    assert po.po_number in by_name["Plan Cable"]["price_source"]
    # Never bought: priced at what it was opened with.
    assert Decimal(by_name["Plan Player"]["unit_price"]) == Decimal("50.00")
    assert by_name["Plan Player"]["price_source"] == "Inventory opening cost"
    assert Decimal(plan["materials_total"]) == Decimal("580.00")  # 4x120 + 2x50

    r = c.post("/api/teams/cost-lines/", {
        "project": str(project.id), "cost_type": "Travelling", "description": "Team to site",
        "quantity": "2", "unit_cost": "1000",
    }, format="json")
    assert r.status_code == 201, r.content
    r = c.patch(f"/api/teams/projects/{project.id}/plan/", {"contingency_percent": "10"}, format="json")
    assert r.status_code == 200, r.content

    plan = r.json()
    assert Decimal(plan["overheads_total"]) == Decimal("2000.00")
    assert Decimal(plan["subtotal"]) == Decimal("2580.00")
    # Contingency covers the materials only — not priced work, not overheads.
    assert Decimal(plan["contingency_amount"]) == Decimal("58.00")
    assert Decimal(plan["total"]) == Decimal("2638.00")
    assert "Travelling" in plan["cost_types"]


@pytest.mark.django_db
def test_a_hand_set_price_beats_the_derived_one():
    """The planner knows things the record does not — a quote, a price rise."""
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U

    project, device, cable, po = _planned_project()
    c = _C()
    c.force_authenticate(_U.objects.create_user(username="price-ops", password="x", role="ops_manager"))

    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()
    by_name = {m["name"]: m for m in plan["materials"]}
    assert Decimal(by_name["Plan Cable"]["unit_price"]) == Decimal("120.00")

    component = device.components.get(name="Plan Cable")
    r = c.patch(f"/api/assets/components/{component.id}/", {"planned_unit_price": "200.00"}, format="json")
    assert r.status_code == 200, r.content

    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()
    by_name = {m["name"]: m for m in plan["materials"]}
    assert Decimal(by_name["Plan Cable"]["unit_price"]) == Decimal("200.00")
    assert by_name["Plan Cable"]["price_source"] == "Set by hand"
    # 4 x 200 + 2 x 50
    assert Decimal(plan["materials_total"]) == Decimal("900.00")


@pytest.mark.django_db
def test_production_steps_are_priced_into_the_plan():
    """An in-house build costs labour as well as parts."""
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.assets.models import ProductionStep

    project, device, cable, po = _planned_project()
    c = _C()
    c.force_authenticate(_U.objects.create_user(username="prod-ops", password="x", role="ops_manager"))

    ProductionStep.objects.create(device=device, step_number=1, name="Frame welding", planned_cost=1500)
    # An unpriced step is not a free step; it simply has no figure yet.
    ProductionStep.objects.create(device=device, step_number=2, name="Panaflex pasting")

    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()
    asset = next(a for a in plan["assets"] if a["id"] == str(device.pk))
    assert [s["name"] for s in asset["steps"]] == ["Frame welding", "Panaflex pasting"]
    assert asset["steps"][1]["planned_cost"] is None
    assert Decimal(asset["production_total"]) == Decimal("1500.00")
    # The asset's own line: what its parts cost plus what building it costs.
    assert Decimal(asset["asset_total"]) == Decimal("2080.00")
    assert Decimal(plan["production_total"]) == Decimal("1500.00")
    assert Decimal(plan["subtotal"]) == Decimal("2080.00")


@pytest.mark.django_db
def test_budget_approval_gates_execution():
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U

    project, device, cable, _ = _planned_project()
    ops = _U.objects.create_user(username="gate-ops", password="x", role="ops_manager")
    head = _U.objects.create_user(username="gate-head", password="x", role="group_head")
    c, h = _C(), _C()
    c.force_authenticate(ops)
    h.force_authenticate(head)

    # Looking at the plan does not start one — only working on it does.
    from apps.teams.costing import blocking_budget

    c.get(f"/api/teams/projects/{project.id}/plan/")
    assert blocking_budget(device) is None
    c.patch(f"/api/teams/projects/{project.id}/plan/", {"contingency_percent": "5"}, format="json")
    component = device.components.get(name="Plan Cable")

    # Planning started, not approved: nothing is drawn from stock yet.
    r = c.post(f"/api/assets/components/{component.id}/fulfil-from-stock/", {}, format="json")
    assert r.status_code == 400
    assert "execution starts once it is approved" in r.data["detail"]

    r = c.post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "submitted"

    # The approved figure is frozen, but a cost nobody planned for can still be
    # recorded — it lands in the actuals with a planned amount of zero.
    r = c.post("/api/teams/cost-lines/", {
        "project": str(project.id), "cost_type": "Labour", "unit_cost": "10",
    }, format="json")
    assert r.status_code == 201, r.content
    assert Decimal(r.data["amount"]) == Decimal("0")

    # The person who wrote it cannot sign it off; ops cannot approve at all.
    r = c.post(f"/api/teams/projects/{project.id}/approve-budget/", {}, format="json")
    assert r.status_code == 403

    # Sending it back needs a reason.
    r = h.post(f"/api/teams/projects/{project.id}/reject-budget/", {}, format="json")
    assert r.status_code == 400

    r = h.post(f"/api/teams/projects/{project.id}/approve-budget/", {"notes": "OK"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "approved"
    project.refresh_from_db()
    assert project.budget == Decimal(r.data["approved_total"])

    # Approved: execution may draw stock.
    r = c.post(f"/api/assets/components/{component.id}/fulfil-from-stock/", {}, format="json")
    assert r.status_code == 200, r.content

    # Revising reopens the plan and locks execution again.
    r = c.post(f"/api/teams/projects/{project.id}/revise-budget/", {}, format="json")
    assert r.status_code == 200, r.content
    player = device.components.get(name="Plan Player")
    r = c.post(f"/api/assets/components/{player.id}/mark-for-procurement/", {}, format="json")
    assert r.status_code == 400



# ---------------------------------------------------------------------------
# One record for "this asset is on this project": the Scope line
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_asset_form_project_link_shows_in_scope_and_follows_moves():
    from apps.assets.models import Device
    from apps.teams.models import Project, ProjectScopeItem

    a = Project.objects.create(name="Scope A")
    b = Project.objects.create(name="Scope B")
    device = Device.objects.create(asset_code="AST-SC-1", serial_number="SC-1", project=a)
    # Linked from the asset form: it now shows in the project's Scope.
    assert ProjectScopeItem.objects.filter(project=a, device=device, component__isnull=True).count() == 1

    # Moved on the asset form: the line moves with it.
    device.project = b
    device.save()
    assert not ProjectScopeItem.objects.filter(project=a, device=device).exists()
    assert ProjectScopeItem.objects.filter(project=b, device=device).exists()

    # Taken off the project's Scope: taken off the project.
    ProjectScopeItem.objects.filter(project=b, device=device).delete()
    device.refresh_from_db()
    assert device.project_id is None

    # Saving again does not duplicate the line.
    device.project = a
    device.save()
    device.save()
    assert ProjectScopeItem.objects.filter(project=a, device=device).count() == 1


@pytest.mark.django_db
def test_plan_costs_each_asset_and_ignores_zero_priced_purchases():
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone as _tz
    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.assets.models import Device
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem

    project, device, cable, po = _planned_project()
    Device.objects.create(asset_code="AST-PLAN-2", serial_number="PLAN-2", project=project)
    # A later receipt at zero — an unpriced PO — must not become "the price".
    later = PurchaseOrder.objects.create(supplier=po.supplier, order_date=_tz.localdate() + timedelta(days=1))
    PurchaseOrderItem.objects.create(
        purchase_order=later, inventory_item=cable, description="cable", quantity=5,
        unit_price=Decimal("0"), received_quantity=5,
    )

    ops = _U.objects.create_user(username="asset-cost-ops", password="x", role="ops_manager")
    c = _C()
    c.force_authenticate(ops)
    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()

    cable_line = next(m for m in plan["materials"] if m["name"] == "Plan Cable")
    assert Decimal(cable_line["unit_price"]) == Decimal("120.00")

    by_asset = {a["asset_code"]: a for a in plan["assets"]}
    assert Decimal(by_asset["AST-PLAN-1"]["materials_total"]) == Decimal("580.00")
    assert by_asset["AST-PLAN-1"]["lines"] == 2
    # An asset with nothing listed still shows, at zero.
    assert Decimal(by_asset["AST-PLAN-2"]["materials_total"]) == Decimal("0")
    assert by_asset["AST-PLAN-2"]["lines"] == 0



# ---------------------------------------------------------------------------
# Execution records what things actually cost, without moving the estimate
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_actual_costs_are_recorded_against_the_approved_budget():
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.teams.models import ProjectCostLine

    project, device, cable, po = _planned_project()
    ops = _U.objects.create_user(username="actual-ops", password="x", role="ops_manager")
    head = _U.objects.create_user(username="actual-head", password="x", role="group_head")
    c, h = _C(), _C()
    c.force_authenticate(ops)
    h.force_authenticate(head)

    c.patch(f"/api/teams/projects/{project.id}/plan/", {"contingency_percent": "0"}, format="json")
    c.post("/api/teams/cost-lines/", {
        "project": str(project.id), "cost_type": "Travelling", "quantity": "2", "unit_cost": "1000",
    }, format="json")
    c.post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json")
    h.post(f"/api/teams/projects/{project.id}/approve-budget/", {"notes": "ok"}, format="json")

    # Nothing issued yet: the actuals are zero even though the estimate is not.
    actuals = c.get(f"/api/teams/projects/{project.id}/actuals/").json()
    assert Decimal(actuals["materials_actual"]) == Decimal("0")
    assert Decimal(actuals["estimate_total"]) == Decimal("2580.00")

    # Draw the cable from stock: the store issuing it is what makes it a cost.
    from apps.inventory.models import IssuanceRequest

    component = device.components.get(name="Plan Cable")
    r = c.post(f"/api/assets/components/{component.id}/fulfil-from-stock/", {}, format="json")
    assert r.status_code == 200, r.content
    row = IssuanceRequest.objects.filter(asset_component=component).latest("created_at")
    r = c.post(f"/api/inventory/issuance-requests/{row.id}/issue/",
               {"quantity": row.quantity_requested}, format="json")
    assert r.status_code == 200, r.content

    # The planned overhead came in dearer than planned.
    line = ProjectCostLine.objects.get(project=project, cost_type="Travelling")
    r = c.patch(f"/api/teams/cost-lines/{line.id}/",
                {"actual_quantity": "3", "actual_unit_cost": "1200"}, format="json")
    assert r.status_code == 200, r.content

    # Something nobody planned for.
    r = c.post("/api/teams/cost-lines/", {
        "project": str(project.id), "cost_type": "Crane hire",
        "actual_quantity": "1", "actual_unit_cost": "5000",
    }, format="json")
    assert r.status_code == 201, r.content
    # It does not touch the approved estimate.
    assert Decimal(r.data["amount"]) == Decimal("0")

    actuals = c.get(f"/api/teams/projects/{project.id}/actuals/").json()
    assert Decimal(actuals["materials_actual"]) == Decimal("480.00")      # 4 x 120
    assert Decimal(actuals["overheads_actual_total"]) == Decimal("8600.00")  # 3x1200 + 5000
    assert Decimal(actuals["actual_total"]) == Decimal("9080.00")
    assert Decimal(actuals["variance_vs_approved"]) == Decimal("6500.00")  # over the 2580 approved

    # The planned figures are still frozen.
    r = c.patch(f"/api/teams/cost-lines/{line.id}/", {"unit_cost": "9999"}, format="json")
    assert r.status_code == 400
    assert "revise it to change the planned figures" in str(r.data)


@pytest.mark.django_db
def test_actuals_document_prints_the_complete_table(db):
    """Execution's actual-cost table prints as a PDF naming every asset, the
    vendor line, the production steps and the overheads."""
    from apps.accounts.models import User as _U
    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel, ProductionStep
    from apps.teams.models import Project, ProjectCostLine, ProjectScopeItem
    from rest_framework.test import APIClient

    admin = _U.objects.create_user(username="act-doc", password="x", role="super_admin")
    project = Project.objects.create(name="Actuals Doc Project")
    brand = Brand.objects.create(name="Doc Brand")
    model = DeviceModel.objects.create(brand=brand, name="DOC-1")
    built = Device.objects.create(device_model=model, serial_number="DOC-IH", source=Device.Source.INHOUSE)
    bought = Device.objects.create(device_model=model, serial_number="DOC-VS", source=Device.Source.VENDOR_SUPPLIED,
                                   purchase_price="120000", status=Device.Status.IN_STOCK)
    for d in (built, bought):
        ProjectScopeItem.objects.create(project=project, device=d, quantity=1)
    AssetComponent.objects.create(device=built, name="Doc Cable", quantity=3)
    ProductionStep.objects.create(device=built, step_number=1, name="Doc cutting", location="in_house",
                                  planned_cost="500", actual_cost="650")
    ProjectCostLine.objects.create(project=project, cost_type="Travelling", description="Travelling",
                                   quantity="2", unit_cost="1500", actual_quantity="2", actual_unit_cost="1700")

    c = APIClient(); c.force_authenticate(admin)
    r = c.get(f"/api/teams/projects/{project.id}/actuals/document/")
    assert r.status_code == 200, r.content[:200]
    assert r["Content-Type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - the text check needs pypdf
        return
    import io as _io
    text = " ".join(p.extract_text() for p in PdfReader(_io.BytesIO(r.content)).pages)
    for needle in ("ACTUAL COST", built.asset_code, bought.asset_code, "Complete asset from the vendor",
                   "Doc cutting", "Travelling", "ACTUAL TO DATE", "120,000.00", "650.00"):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# Deleting a project
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_project_with_no_activity_can_be_deleted():
    """Scope, milestones and requirements go with it; assets are only unlinked."""
    from apps.teams.models import ProjectMilestone, ProjectScopeItem

    ops = User.objects.create_user(username="del-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="DelBrand")
    dm = DeviceModel.objects.create(brand=brand, name="D-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-DEL-1", serial_number="DEL-1")
    project = Project.objects.create(name="Mistaken entry", phase="query")
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    ProjectMilestone.objects.create(project=project, title="Kick-off", due_date="2026-10-01", order=1)

    r = _client(ops).delete(f"/api/teams/projects/{project.pk}/")
    assert r.status_code == 204, r.content
    assert not Project.objects.filter(pk=project.pk).exists()
    assert not ProjectScopeItem.objects.filter(device=device).exists()
    device.refresh_from_db()
    assert device.project_id is None


@pytest.mark.django_db
def test_a_project_with_activity_is_kept():
    """Stock issued or a work order placed: the project stays for the record."""
    from apps.suppliers.models import Supplier
    from apps.workorders.models import WorkOrder

    ops = User.objects.create_user(username="keep-ops", password="x", role="ops_manager")
    project = Project.objects.create(name="Live rollout", phase="production")
    shop = Supplier.objects.create(name="Keep Works")
    WorkOrder.objects.create(title="Frames", supplier=shop, project=project)

    r = _client(ops).delete(f"/api/teams/projects/{project.pk}/")
    assert r.status_code == 400, r.content
    assert "1 work order(s)" in r.data["detail"] and "On Hold" in r.data["detail"]
    assert Project.objects.filter(pk=project.pk).exists()

    # A purchase order raised for a component of an asset on its scope counts too.
    from decimal import Decimal

    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.teams.models import ProjectScopeItem

    ordered = Project.objects.create(name="Ordered rollout", phase="production")
    brand = Brand.objects.create(name="KeepBrand")
    dm = DeviceModel.objects.create(brand=brand, name="K-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-KEEP-1", serial_number="KEEP-1")
    ProjectScopeItem.objects.create(project=ordered, device=device, quantity=1)
    po = PurchaseOrder.objects.create(supplier=shop, ordered_by=ops, status=PurchaseOrder.Status.DRAFT)
    line = PurchaseOrderItem.objects.create(purchase_order=po, description="Frame steel", quantity=2, unit_price=Decimal("10"))
    AssetComponent.objects.create(device=device, name="Frame steel", quantity=2, purchase_order_item=line)
    r = _client(ops).delete(f"/api/teams/projects/{ordered.pk}/")
    assert r.status_code == 400 and "1 purchase order line(s)" in r.data["detail"], r.data
    # Cancelled orders do not hold a project back.
    po.status = PurchaseOrder.Status.CANCELLED
    po.save(update_fields=["status"])
    assert _client(ops).delete(f"/api/teams/projects/{ordered.pk}/").status_code == 204

    # Only managers delete at all.
    tech = User.objects.create_user(username="keep-tech", password="x", role="technician")
    empty = Project.objects.create(name="Nothing yet", phase="query")
    assert _client(tech).delete(f"/api/teams/projects/{empty.pk}/").status_code == 403


@pytest.mark.django_db
def test_projects_are_searched_by_client_and_site_too():
    from apps.clients.models import Client

    ops = User.objects.create_user(username="search-ops", password="x", role="ops_manager")
    acme = Client.objects.create(name="Acme Retail")
    Project.objects.create(name="Window displays", client=acme, phase="query")
    Project.objects.create(name="Kiosks", phase="query")

    names = [p["name"] for p in _client(ops).get("/api/teams/projects/", {"search": "acme"}).json()["results"]]
    assert names == ["Window displays"]


@pytest.mark.django_db
def test_a_bought_asset_costs_what_its_order_charged_plus_installing_it():
    """An asset bought whole is not built here: its cost is the purchase
    order's, and the only other head is putting it in and switching it on."""
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    brand = Brand.objects.create(name="Bought Brand")
    dm = DeviceModel.objects.create(brand=brand, name="BG-1")
    project = Project.objects.create(name="Bought Rollout")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-BUY-1", serial_number="BUY-1",
        source="vendor_supplied", project=project, status="in_stock",
    )
    supplier = Supplier.objects.create(name="Screen Co")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.RECEIVED)
    item = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Screen", quantity=1, unit_price=Decimal("4800"),
    )
    device.procurement_item = item
    # A stale price on the asset must not win over what the order charged.
    device.purchase_price = Decimal("1000")
    device.planned_installation_cost = Decimal("500")
    device.actual_installation_cost = Decimal("615")
    device.save(update_fields=[
        "procurement_item", "purchase_price", "planned_installation_cost", "actual_installation_cost",
    ])

    ops = _U.objects.create_user(username="buy-ops", password="x", role="ops_manager")
    c = _C()
    c.force_authenticate(ops)

    plan = c.get(f"/api/teams/projects/{project.id}/plan/").json()
    assert Decimal(str(plan["installation_total"])) == Decimal("500")
    asset = plan["assets"][0]
    assert Decimal(str(asset["installation_cost"])) == Decimal("500")
    assert asset["steps"] == [] and asset["lines"] == 0, "nothing is built here"

    actuals = c.get(f"/api/teams/projects/{project.id}/actuals/").json()
    asset = actuals["assets"][0]
    assert Decimal(str(asset["asset_price"])) == Decimal("4800"), "the order, not the asset's own price"
    assert asset["asset_priced_from"] == f"Purchase order · {po.po_number}"
    assert asset["asset_arrived"] is True
    assert Decimal(str(asset["installation_actual"])) == Decimal("615")
    assert Decimal(str(asset["actual_total"])) == Decimal("5415")
    # Two heads only: nothing was produced here.
    assert Decimal(str(actuals["production_actual"])) == Decimal("0")
    assert Decimal(str(actuals["work_orders_actual"])) == Decimal("0")
    assert Decimal(str(actuals["installation_actual"])) == Decimal("615")
    assert Decimal(str(actuals["actual_total"])) == Decimal("5415")


@pytest.mark.django_db
def test_a_bought_asset_that_has_not_arrived_costs_nothing_yet():
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    brand = Brand.objects.create(name="Waiting Brand")
    dm = DeviceModel.objects.create(brand=brand, name="WT-1")
    project = Project.objects.create(name="Waiting Rollout")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-WAIT-1", serial_number="WAIT-1",
        source="vendor_supplied", project=project, status="procured",
    )
    supplier = Supplier.objects.create(name="Slow Co")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    device.procurement_item = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Screen", quantity=1, unit_price=Decimal("4800"),
    )
    device.save(update_fields=["procurement_item"])

    ops = _U.objects.create_user(username="wait-ops", password="x", role="ops_manager")
    c = _C()
    c.force_authenticate(ops)
    asset = c.get(f"/api/teams/projects/{project.id}/actuals/").json()["assets"][0]
    assert asset["asset_arrived"] is False and asset["outstanding"] == 1
    assert Decimal(str(asset["actual_total"])) == Decimal("0"), "nothing is spent until it arrives"


@pytest.mark.django_db
def test_the_scope_line_says_where_the_asset_goes():
    """The registry does not ask where an asset is — the project's Scope line
    does, and the asset follows it."""
    from apps.sites.models import Site
    from apps.teams.models import ProjectScopeItem

    brand = Brand.objects.create(name="Scope Site Brand")
    dm = DeviceModel.objects.create(brand=brand, name="SS-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-SITE-1", serial_number="SITE-1")
    project = Project.objects.create(name="Scope Site Rollout")
    mall = Site.objects.create(name="Mall One", address="1 Road")
    tower = Site.objects.create(name="Tower Two", address="2 Road")
    assert device.current_site_id is None

    item = ProjectScopeItem.objects.create(project=project, device=device, quantity=1, site=mall)
    device.refresh_from_db()
    assert device.current_site_id == mall.pk, "scoping it to a site puts it there"

    # Moving the line moves the asset with it.
    item.site = tower
    item.save(update_fields=["site"])
    device.refresh_from_db()
    assert device.current_site_id == tower.pk

    # Clearing the line leaves the asset where it is: paperwork does not move
    # something that is already standing somewhere.
    item.site = None
    item.save(update_fields=["site"])
    device.refresh_from_db()
    assert device.current_site_id == tower.pk


@pytest.mark.django_db
def test_a_bought_asset_names_its_vendor_only_once_an_order_does():
    """Nobody supplies an asset until the purchase order says so, so the plan
    names no vendor before one exists and reads it off the order after."""
    from decimal import Decimal

    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    brand = Brand.objects.create(name="Vendor Name Brand")
    dm = DeviceModel.objects.create(brand=brand, name="VN-1")
    project = Project.objects.create(name="Vendor Name Rollout")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-VEND-1", serial_number="VEND-1",
        source="vendor_supplied", project=project,
    )
    ops = _U.objects.create_user(username="vend-ops", password="x", role="ops_manager")
    c = _C()
    c.force_authenticate(ops)

    asset = c.get(f"/api/teams/projects/{project.id}/plan/").json()["assets"][0]
    assert asset["supply_vendor_name"] is None and asset["po_number"] is None

    # A name typed onto the asset is not a vendor either — only an order is.
    device.supply_vendor_name = "Somebody Somebody"
    device.save(update_fields=["supply_vendor_name"])
    asset = c.get(f"/api/teams/projects/{project.id}/plan/").json()["assets"][0]
    assert asset["supply_vendor_name"] is None, "a typed name is not a purchase"

    supplier = Supplier.objects.create(name="Screens Limited")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    device.procurement_item = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Screen", quantity=1, unit_price=Decimal("4800"),
    )
    device.save(update_fields=["procurement_item"])

    asset = c.get(f"/api/teams/projects/{project.id}/plan/").json()["assets"][0]
    assert asset["supply_vendor_name"] == "Screens Limited"
    assert asset["po_number"] == po.po_number


@pytest.mark.django_db
def test_each_phase_says_how_far_it_has_got():
    """A phase is work, so its bar is counted from the work: parts issued,
    operations finished, installation steps done, assets handed over."""
    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U
    from apps.assets.models import ProductionStep
    from apps.sites.models import DeviceInstallation, InstallationStep, Site
    from apps.teams.models import ProjectScopeItem
    from apps.teams.phases import phase_progress

    brand = Brand.objects.create(name="Phase Brand")
    dm = DeviceModel.objects.create(brand=brand, name="PH-1")
    project = Project.objects.create(name="Phase Rollout")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-PHASE-1", serial_number="PHASE-1", source="inhouse",
    )
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    material = MaterialType.objects.create(name="Phase Panel", unit="piece")
    item = InventoryItem.objects.create(material_type=material, quantity=10)
    AssetComponent.objects.create(device=device, name="Phase Panel", quantity=4, inventory_item=item)
    cut = ProductionStep.objects.create(device=device, step_number=1, name="Cutting")
    ProductionStep.objects.create(device=device, step_number=2, name="Fitting")

    bars = phase_progress(project)
    # Every phase is counted in assets: one asset here, none of it done.
    assert bars["procurement"]["done"] == 0 and bars["procurement"]["total"] == 1
    assert bars["production"]["total"] == 1 and bars["production"]["percent"] == 0
    assert bars["handover"]["total"] == 1 and bars["handover"]["percent"] == 0
    assert bars["planning"]["percent"] == 0, "no budget submitted yet"

    # Half the parts in, one operation done.
    component = device.components.get()
    component.issued_quantity = 2
    component.save(update_fields=["issued_quantity"])
    cut.status = ProductionStep.Status.COMPLETED
    cut.save(update_fields=["status"])
    bars = phase_progress(project)
    assert bars["procurement"]["percent"] == 50
    assert bars["production"]["percent"] == 50

    # An installation opened, half its checklist worked through.
    site = Site.objects.create(name="Phase Site", address="1 Road")
    job = DeviceInstallation(device=device, site=site, installed_at=timezone.now())
    job._skip_default_steps = True
    job.save()
    for n, status in enumerate((InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.NOT_STARTED), start=1):
        InstallationStep.objects.create(
            installation=job, step_type=InstallationStep.StepType.SURVEY, step_number=n, status=status,
        )
    bars = phase_progress(project)
    assert bars["installation"]["percent"] == 50, bars["installation"]

    # Handed over once the client has it. Running is not the same thing — see
    # test_handing_over_needs_the_client_to_have_accepted_it — so this uses the
    # state that means the client owns it outright.
    device.status = Device.Status.CLIENT_PROPERTY
    device.save(update_fields=["status"])
    assert phase_progress(project)["handover"]["percent"] == 100


@pytest.mark.django_db
def test_approving_the_budget_moves_the_project_off_planning():
    """Planning ends when the figure is agreed; the order stops reading as
    still being planned."""
    from rest_framework.test import APIClient as _C

    from apps.accounts.models import User as _U

    project, device, cable, po = _planned_project()
    assert project.phase == Project.Phase.PLANNING and project.status == Project.Status.PLANNING

    ops = _U.objects.create_user(username="phase-ops", password="x", role="ops_manager")
    head = _U.objects.create_user(username="phase-head", password="x", role="group_head")
    c, h = _C(), _C()
    c.force_authenticate(ops)
    h.force_authenticate(head)
    assert c.post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json").status_code == 200
    assert h.post(f"/api/teams/projects/{project.id}/approve-budget/", {}, format="json").status_code == 200

    project.refresh_from_db()
    assert project.phase == Project.Phase.PROCUREMENT
    assert project.status == Project.Status.ON_TRACK


@pytest.mark.django_db
def test_a_phase_is_split_evenly_between_the_assets():
    """One asset's long bill of materials cannot drown out the others.

    A project of three assets moves a third at a time, however many parts or
    operations each asset happens to carry — otherwise a big asset's screws
    would read as more progress than a whole other asset being finished.
    """
    from apps.assets.models import ProductionStep
    from apps.teams.models import ProjectScopeItem
    from apps.teams.phases import phase_progress

    brand = Brand.objects.create(name="Split Brand")
    dm = DeviceModel.objects.create(brand=brand, name="SPL-1")
    project = Project.objects.create(name="Even Split Rollout")
    material = MaterialType.objects.create(name="Split Panel", unit="piece")
    item = InventoryItem.objects.create(material_type=material, quantity=500)

    # A big asset (40 parts) and two small ones (2 parts each).
    sizes = [40, 2, 2]
    devices = []
    for n, size in enumerate(sizes, start=1):
        d = Device.objects.create(device_model=dm, asset_code=f"AST-SPLIT-{n}", source="inhouse")
        ProjectScopeItem.objects.create(project=project, device=d, quantity=1)
        AssetComponent.objects.create(
            device=d, name="Split Panel", quantity=size, inventory_item=item,
        )
        ProductionStep.objects.create(device=d, step_number=1, name="Assemble")
        devices.append(d)

    bars = phase_progress(project)
    assert bars["procurement"]["total"] == 3 and bars["procurement"]["percent"] == 0

    # Supply the big asset in full: one of three assets, so a third.
    big = devices[0].components.get()
    big.issued_quantity = 40
    big.save(update_fields=["issued_quantity"])
    bars = phase_progress(project)
    assert bars["procurement"]["percent"] == 33, bars["procurement"]
    assert bars["procurement"]["done"] == 1
    assert bars["procurement"]["note"] == "1 of 3 assets supplied"

    # Half of one small asset: half a share, so a sixth more.
    small = devices[1].components.get()
    small.issued_quantity = 1
    small.save(update_fields=["issued_quantity"])
    assert phase_progress(project)["procurement"]["percent"] == 50

    # Production splits the same way: one of three routes finished.
    step = devices[0].production_steps.get()
    step.status = ProductionStep.Status.COMPLETED
    step.save(update_fields=["status"])
    bars = phase_progress(project)
    assert bars["production"]["percent"] == 33 and bars["production"]["total"] == 3

    # And handing over: one of three now belongs to the client.
    devices[2].status = Device.Status.CLIENT_PROPERTY
    devices[2].save(update_fields=["status"])
    bars = phase_progress(project)
    assert bars["handover"]["percent"] == 33
    assert bars["handover"]["note"] == "1 of 3 assets handed over"
    # Nobody has opened an installation, so none of that phase is done.
    assert bars["installation"]["percent"] == 0


@pytest.mark.django_db
def test_a_vendor_asset_clears_procurement_and_production_when_it_arrives():
    """Nobody here builds it, so delivery is the whole of both phases.

    A complete asset bought from a vendor has no bill of materials and no
    route. Until it turns up neither phase has moved for it; once it is in
    stock both are finished, and it waits on installation like anything else.
    """
    from apps.teams.models import ProjectScopeItem
    from apps.teams.phases import phase_progress

    brand = Brand.objects.create(name="Vendor Share Brand")
    dm = DeviceModel.objects.create(brand=brand, name="VSH-1")
    project = Project.objects.create(name="Vendor Share Rollout")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-VSHARE-1", source="vendor_supplied",
        status=Device.Status.PROCURED,
    )
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)

    bars = phase_progress(project)
    assert bars["procurement"]["percent"] == 0, "on order, not delivered"
    assert bars["production"]["percent"] == 0, "the vendor has not delivered it"

    # It arrives and goes into stock.
    device.status = Device.Status.IN_STOCK
    device.save(update_fields=["status"])

    bars = phase_progress(project)
    assert bars["procurement"]["percent"] == 100
    assert bars["production"]["percent"] == 100, "nobody here builds it"
    assert bars["installation"]["percent"] == 0, "still to be put in"
    assert bars["handover"]["percent"] == 0


@pytest.mark.django_db
def test_handing_over_needs_the_client_to_have_accepted_it():
    """An asset the crew switched on has not been handed over.

    Handing over is the client accepting it on the Installation Tracker. Until
    that record exists the phase has not moved for that asset, however live it
    is — otherwise the bar would say a project was delivered while the client
    had signed nothing.
    """
    from apps.clients.models import Client
    from apps.sites.models import DeviceInstallation, HandoverRecord, Site
    from apps.teams.models import ProjectScopeItem
    from apps.teams.phases import phase_progress

    brand = Brand.objects.create(name="Handover Brand")
    dm = DeviceModel.objects.create(brand=brand, name="HO-1")
    project = Project.objects.create(name="Handover Rollout")
    site = Site.objects.create(name="Handover Site", address="1 Road")
    client = Client.objects.create(name="Handover Client")

    devices = []
    for n in range(2):
        d = Device.objects.create(
            device_model=dm, asset_code=f"AST-HO-{n}", source="vendor_supplied",
            current_site=site, status=Device.Status.ACTIVE,
        )
        ProjectScopeItem.objects.create(project=project, device=d, quantity=1)
        devices.append(d)

    # Both are live, neither has been accepted by anybody.
    assert phase_progress(project)["handover"]["percent"] == 0

    job = DeviceInstallation(device=devices[0], site=site, installed_at=timezone.now())
    job._skip_default_steps = True
    job.save()
    HandoverRecord.objects.create(
        installation=job, device=devices[0], client=client, site=site,
        handover_date=timezone.localdate(), accepted_by_name="Site Manager",
    )
    bars = phase_progress(project)
    assert bars["handover"]["percent"] == 50
    assert bars["handover"]["note"] == "1 of 2 assets handed over"

    # An asset that became the client's own property is past handing over.
    devices[1].status = Device.Status.CLIENT_PROPERTY
    devices[1].save(update_fields=["status"])
    assert phase_progress(project)["handover"]["percent"] == 100


@pytest.mark.django_db
def test_progress_ignores_the_phase_label_and_counts_the_work():
    """A project can be finished while the marker still says Planning.

    The phase is set by hand and gets forgotten. Every parts issued, every
    asset built and installed is recorded as it happens, so the overall figure
    reads those instead — otherwise a project with all its work done reported
    a fifth of it because nobody moved the label.
    """
    from apps.assets.models import ProductionStep
    from apps.teams.models import ProjectScopeItem
    from apps.teams.phases import phase_progress

    brand = Brand.objects.create(name="Progress Brand")
    dm = DeviceModel.objects.create(brand=brand, name="PRG-1")
    # The label is left where it started, deliberately.
    project = Project.objects.create(name="Progress Rollout", phase=Project.Phase.PLANNING)
    material = MaterialType.objects.create(name="Progress Panel", unit="piece")
    item = InventoryItem.objects.create(material_type=material, quantity=50)

    device = Device.objects.create(device_model=dm, asset_code="AST-PRG-1", source="inhouse")
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    component = AssetComponent.objects.create(
        device=device, name="Progress Panel", quantity=4, inventory_item=item,
    )
    step = ProductionStep.objects.create(device=device, step_number=1, name="Assemble")

    # Nothing done yet, and no budget: none of the five phases has moved.
    assert project.computed_progress() == 0

    # Parts in and the route finished: two of five phases done, so two fifths.
    component.issued_quantity = 4
    component.save(update_fields=["issued_quantity"])
    step.status = ProductionStep.Status.COMPLETED
    step.save(update_fields=["status"])
    bars = phase_progress(project)
    assert bars["procurement"]["percent"] == 100 and bars["production"]["percent"] == 100
    assert project.computed_progress() == 40

    # The label never moved, and that is the point.
    project.refresh_from_db()
    assert project.phase == Project.Phase.PLANNING


@pytest.mark.django_db
def test_the_phase_is_the_first_one_not_finished():
    """The label used to be moved by hand and got left behind.

    A phase is done when its bar reads 100%, so the project is in the first
    one that is not. Reading the project is when the stored label catches up,
    which is what keeps list filters honest.
    """
    from apps.assets.models import ProductionStep
    from apps.teams.models import ProjectBudget, ProjectScopeItem

    brand = Brand.objects.create(name="Phase Sync Brand")
    dm = DeviceModel.objects.create(brand=brand, name="PSY-1")
    project = Project.objects.create(name="Phase Sync Rollout", phase=Project.Phase.PLANNING)
    material = MaterialType.objects.create(name="Sync Panel", unit="piece")
    item = InventoryItem.objects.create(material_type=material, quantity=50)
    device = Device.objects.create(device_model=dm, asset_code="AST-PSY-1", source="inhouse")
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    component = AssetComponent.objects.create(
        device=device, name="Sync Panel", quantity=2, inventory_item=item,
    )
    step = ProductionStep.objects.create(device=device, step_number=1, name="Assemble")

    # No budget: planning is the first unfinished phase.
    assert project.sync_phase() == Project.Phase.PLANNING

    plan = ProjectBudget.objects.create(project=project, status=ProjectBudget.Status.APPROVED)
    assert plan.status == ProjectBudget.Status.APPROVED
    assert project.sync_phase() == Project.Phase.PROCUREMENT

    component.issued_quantity = 2
    component.save(update_fields=["issued_quantity"])
    assert project.sync_phase() == Project.Phase.PRODUCTION

    step.status = ProductionStep.Status.COMPLETED
    step.save(update_fields=["status"])
    assert project.sync_phase() == Project.Phase.INSTALLATION

    # The stored field really moved, so a filter on it finds the project.
    project.refresh_from_db()
    assert project.phase == Project.Phase.INSTALLATION
    assert Project.objects.filter(phase=Project.Phase.INSTALLATION, pk=project.pk).exists()


@pytest.mark.django_db
def test_an_off_ramp_is_not_overruled_by_the_work():
    """On Hold and Order Lost are somebody's decision, not the work's."""
    from apps.teams.models import ProjectScopeItem

    brand = Brand.objects.create(name="Off Ramp Brand")
    dm = DeviceModel.objects.create(brand=brand, name="OFR-1")
    project = Project.objects.create(name="Off Ramp Rollout", phase=Project.Phase.ON_HOLD)
    device = Device.objects.create(device_model=dm, asset_code="AST-OFR-1", source="inhouse")
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)

    assert project.sync_phase() == Project.Phase.ON_HOLD
    project.refresh_from_db()
    assert project.phase == Project.Phase.ON_HOLD


@pytest.mark.django_db
def test_a_project_completes_itself_when_every_phase_is_done():
    """Nothing left in any phase is the one status the work can declare."""
    from apps.clients.models import Client
    from apps.sites.models import DeviceInstallation, HandoverRecord, InstallationStep, Site
    from apps.teams.models import ProjectBudget, ProjectScopeItem

    brand = Brand.objects.create(name="Complete Brand")
    dm = DeviceModel.objects.create(brand=brand, name="CMP-1")
    site = Site.objects.create(name="Complete Site", address="1 Road")
    client = Client.objects.create(name="Complete Client")
    project = Project.objects.create(
        name="Complete Rollout", phase=Project.Phase.PLANNING, client=client,
    )
    ProjectBudget.objects.create(project=project, status=ProjectBudget.Status.APPROVED)

    # Bought whole, so procurement and production are done when it arrives.
    device = Device.objects.create(
        device_model=dm, asset_code="AST-CMP-1", source="vendor_supplied",
        current_site=site, status=Device.Status.ACTIVE,
    )
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)

    job = DeviceInstallation(device=device, site=site, installed_at=timezone.now())
    job._skip_default_steps = True
    job.save()
    job.steps.all().delete()
    InstallationStep.objects.create(
        installation=job, step_type=InstallationStep.StepType.SURVEY, step_number=1,
        status=InstallationStep.StepStatus.COMPLETED,
    )
    HandoverRecord.objects.create(
        installation=job, device=device, client=client, site=site,
        handover_date=timezone.localdate(), accepted_by_name="Site Manager",
    )

    assert project.computed_progress() == 100
    assert project.sync_phase() == Project.Phase.HANDOVER
    project.refresh_from_db()
    assert project.status == Project.Status.COMPLETED
