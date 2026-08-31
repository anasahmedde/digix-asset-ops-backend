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
    assert detail.data["phase"] == "production"
    assert detail.data["phase_display"] == "Production"
    assert len(detail.data["scope_items"]) == 1
    assert len(detail.data["milestones"]) == 1

    # phase is writable through the normal update flow
    r = c.patch(f"/api/teams/projects/{project.pk}/", {"phase": "delivery"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["phase"] == "delivery"


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
    # no milestones -> phase position along the 11-step ladder
    p = Project.objects.create(name="Ladder", phase="production")  # index 4 of 10
    assert c.get(f"/api/teams/projects/{p.pk}/").data["progress"] == 40

    # milestones override the ladder: 2 of 4 done -> 50
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
