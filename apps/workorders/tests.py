"""Work orders are the services we take from vendors: Execution asks, Work
Orders raises, the Group Head approves, the operation follows the order."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Brand, Device, DeviceLifecycleEvent, DeviceModel, ProductionStep
from apps.suppliers.models import Supplier
from apps.teams.models import Project, ProjectScopeItem
from apps.workorders.models import WorkOrder


def _client(role, name):
    user = User.objects.create_user(username=f"wo-{name}", password="x", role=role)
    c = APIClient()
    c.force_authenticate(user)
    return c, user


@pytest.fixture
def build(db):
    """A project with one in-house asset whose route has three operations."""
    brand = Brand.objects.create(name="WO Brand")
    dm = DeviceModel.objects.create(brand=brand, name="WO-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-WO-1", serial_number="WO-1", source="inhouse", display_name="Kiosk")
    project = Project.objects.create(name="WO Rollout", phase="production")
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    steps = [
        ProductionStep.objects.create(device=device, step_number=n, name=name, planned_cost=cost)
        for n, name, cost in ((1, "Cutting", 400), (2, "Painting", 900), (3, "Assembly", None))
    ]
    vendor = Supplier.objects.create(name="Ali Paint Works")
    return {"device": device, "project": project, "steps": steps, "vendor": vendor}


@pytest.mark.django_db
def test_execution_asks_work_orders_raises_group_head_approves(build):
    ops, _ = _client("ops_manager", "ops")
    head, _ = _client("group_head", "head")
    cut, paint, assemble = build["steps"]

    # Execution: two operations go to a vendor, one stays in-house.
    for step in (cut, paint):
        r = ops.post(f"/api/assets/production-steps/{step.id}/decide/", {"location": "external"}, format="json")
        assert r.status_code == 200, r.content
        assert r.data["work_order_requested"] is True and r.data["workshop_display"] == "Work order requested"
        assert r.data["allowed_transitions"] == [] and "Requests" in r.data["hold_reason"]
    assert ops.post(f"/api/assets/production-steps/{assemble.id}/decide/", {"location": "in_house"}, format="json").status_code == 200

    # Work Orders › Requests lists exactly those two, with what the planner expected them to cost.
    rows = ops.get("/api/work-orders/requests/").json()["results"]
    assert [(r["operation"], r["asset_code"], r["project_name"]) for r in rows] == [
        ("Cutting", "AST-WO-1", "WO Rollout"), ("Painting", "AST-WO-1", "WO Rollout"),
    ]
    assert str(rows[0]["planned_cost"]).startswith("400")

    # One draft order to one vendor, a line per operation, priced from the plan unless overtyped.
    r = ops.post("/api/work-orders/raise/", {
        "steps": [str(cut.id), str(paint.id)], "supplier": str(build["vendor"].id),
        "amounts": {str(paint.id): "1000"}, "expected_delivery": "2026-10-05", "terms": "Net 15", "notes": "Collect Monday",
    }, format="json")
    assert r.status_code == 201, r.content
    order = WorkOrder.objects.get(pk=r.data["id"])
    assert order.status == "draft" and order.order_type == "services"
    assert order.project == build["project"] and order.device == build["device"]
    assert [(i.description, i.unit_price) for i in order.items.order_by("created_at")] == [
        ("Cutting on Kiosk", 400), ("Painting on Kiosk", 1000),
    ]
    assert order.total_amount == 1400 and "2 operations" in order.title and "WO Rollout" in order.title
    # The requests are answered: the operations read 'Work Order Raised' and leave the queue.
    for step in (cut, paint):
        step.refresh_from_db()
        assert step.status == "sent_out" and step.workshop == build["vendor"] and step.work_order_requested_at is None
        detail = ops.get(f"/api/assets/production-steps/{step.id}/").json()
        assert detail["work_order"]["wo_number"] == order.wo_number and detail["work_order_requested"] is False
    assert ops.get("/api/work-orders/requests/").json()["results"] == []
    assert ops.post("/api/work-orders/raise/", {"steps": [str(cut.id)], "supplier": str(build["vendor"].id)}, format="json").status_code == 400

    # The same sign-off as a purchase order.
    assert ops.post(f"/api/work-orders/{order.id}/transition/", {"status": "pending_approval"}, format="json").status_code == 200
    r = ops.post(f"/api/work-orders/{order.id}/transition/", {"status": "approved"}, format="json")
    assert r.status_code == 403 and "Group Head" in r.data["detail"]
    r = head.post(f"/api/work-orders/{order.id}/transition/", {"status": "approved"}, format="json")
    assert r.status_code == 200, r.content
    order.refresh_from_db()
    assert order.order_date is not None and order.approved_by is not None
    assert not Project.objects.filter(source_work_order=order).exists()   # services: nothing to install
    assert head.post(f"/api/work-orders/{order.id}/transition/", {"status": "issued"}, format="json").status_code == 403

    # The document prints; the vendor delivers; inspection completes both operations.
    assert ops.get(f"/api/work-orders/{order.id}/print/").status_code == 200
    for st in ("issued", "in_progress", "delivered"):
        assert ops.post(f"/api/work-orders/{order.id}/transition/", {"status": st}, format="json").status_code == 200
    for step in (cut, paint):
        step.refresh_from_db()
        assert step.status == "returned"
    r = ops.post(f"/api/work-orders/{order.id}/transition/", {"status": "completed"}, format="json")
    assert r.status_code == 400 and "Work Receiving" in r.data["detail"]
    assert [w["wo_number"] for w in ops.get("/api/work-orders/receiving/").json()["results"]] == [order.wo_number]
    r = ops.post(f"/api/work-orders/{order.id}/inspect/", {"result": "accepted", "notes": "Finish is good"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "completed" and r.data["inspected_by_name"] == "wo-ops" and r.data["inspection_result"] == "accepted"
    for step in (cut, paint):
        step.refresh_from_db()
        assert step.status == "completed"
    detail = ops.get(f"/api/assets/production-steps/{cut.id}/").json()
    assert detail["work_order"]["inspected_by_name"] == "wo-ops" and detail["work_order"]["inspection_result"] == "accepted"
    assemble.refresh_from_db()
    assert assemble.status == "pending" and assemble.location == "in_house"


@pytest.mark.django_db
def test_a_request_can_be_sent_back_to_the_project(build):
    ops, user = _client("ops_manager", "ops2")
    cut = build["steps"][0]
    assert ops.post(f"/api/assets/production-steps/{cut.id}/decide/", {"location": "external"}, format="json").status_code == 200

    r = ops.post("/api/work-orders/requests/send-back/", {"step": str(cut.id)}, format="json")
    assert r.status_code == 400 and "why" in str(r.data["reason"]).lower()
    r = ops.post("/api/work-orders/requests/send-back/", {"step": str(cut.id), "reason": "Our floor is free next week"}, format="json")
    assert r.status_code == 200, r.content
    cut.refresh_from_db()
    assert cut.location == "undecided" and cut.work_order_requested_at is None
    note = DeviceLifecycleEvent.objects.filter(device=build["device"], metadata__sent_back=True).get()
    assert "Our floor is free next week" in note.description and note.performed_by == user
    assert ops.get("/api/work-orders/requests/").json()["results"] == []

    # Once an order exists the request is gone; sending back is refused.
    paint = build["steps"][1]
    ops.post(f"/api/assets/production-steps/{paint.id}/decide/", {"location": "external"}, format="json")
    assert ops.post("/api/work-orders/raise/", {"steps": [str(paint.id)], "supplier": str(build["vendor"].id)}, format="json").status_code == 201
    r = ops.post("/api/work-orders/requests/send-back/", {"step": str(paint.id), "reason": "changed mind"}, format="json")
    assert r.status_code == 400 and "already on a work order" in str(r.data["step"])


@pytest.mark.django_db
def test_a_cancelled_order_hands_every_operation_back(build):
    ops, _ = _client("ops_manager", "ops3")
    cut, paint, _ = build["steps"]
    for step in (cut, paint):
        ops.post(f"/api/assets/production-steps/{step.id}/decide/", {"location": "external"}, format="json")
    r = ops.post("/api/work-orders/raise/", {"steps": [str(cut.id), str(paint.id)], "supplier": str(build["vendor"].id)}, format="json")
    assert r.status_code == 201, r.content
    assert ops.post(f"/api/work-orders/{r.data['id']}/transition/", {"status": "cancelled"}, format="json").status_code == 200
    for step in (cut, paint):
        step.refresh_from_db()
        assert step.location == "undecided" and step.status == "pending" and step.workshop is None
    # Both are open for a fresh decision, so a new request can be raised.
    assert ops.post(f"/api/assets/production-steps/{cut.id}/decide/", {"location": "external"}, format="json").status_code == 200
    assert len(ops.get("/api/work-orders/requests/").json()["results"]) == 1


@pytest.mark.django_db
def test_new_work_orders_are_services(db):
    ops, _ = _client("ops_manager", "ops4")
    vendor = Supplier.objects.create(name="Any Vendor")
    r = ops.post("/api/work-orders/", {"title": "Repair visit", "supplier": str(vendor.id), "items": []}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["order_type"] == "services" and r.data["order_type_display"] == "Services"


@pytest.mark.django_db
def test_delivered_work_is_inspected_before_it_completes(build):
    """Rework sends the order back to the vendor with the reason on record;
    accepting it later completes the operation."""
    ops, _ = _client("ops_manager", "ops5")
    paint = build["steps"][1]
    ops.post(f"/api/assets/production-steps/{paint.id}/decide/", {"location": "external"}, format="json")
    order_id = ops.post("/api/work-orders/raise/", {"steps": [str(paint.id)], "supplier": str(build["vendor"].id)}, format="json").data["id"]
    assert ops.post(f"/api/work-orders/{order_id}/inspect/", {"result": "accepted"}, format="json").status_code == 400
    for st in ("pending_approval",):
        ops.post(f"/api/work-orders/{order_id}/transition/", {"status": st}, format="json")
    head, _ = _client("group_head", "head5")
    head.post(f"/api/work-orders/{order_id}/transition/", {"status": "approved"}, format="json")
    for st in ("issued", "in_progress", "delivered"):
        assert ops.post(f"/api/work-orders/{order_id}/transition/", {"status": st}, format="json").status_code == 200
    r = ops.post(f"/api/work-orders/{order_id}/inspect/", {"result": "rework"}, format="json")
    assert r.status_code == 400 and "redone" in str(r.data["notes"])
    r = ops.post(f"/api/work-orders/{order_id}/inspect/", {"result": "rework", "notes": "Paint runs on two panels"}, format="json")
    assert r.status_code == 200 and r.data["status"] == "in_progress" and "Paint runs" in r.data["inspection_notes"]
    paint.refresh_from_db()
    assert paint.status == "sent_out"
    assert ops.post(f"/api/work-orders/{order_id}/transition/", {"status": "delivered"}, format="json").status_code == 200
    r = ops.post(f"/api/work-orders/{order_id}/inspect/", {"result": "accepted", "notes": "Redone, good"}, format="json")
    assert r.status_code == 200 and r.data["status"] == "completed" and r.data["delivered_at"]
    paint.refresh_from_db()
    assert paint.status == "completed"
