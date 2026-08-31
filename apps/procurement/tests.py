from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
from apps.suppliers.models import Supplier


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def people(db):
    return {
        "finance": User.objects.create_user(username="po-fin", password="x", role="finance"),
        "ops": User.objects.create_user(username="po-ops", password="x", role="ops_manager"),
        "tech": User.objects.create_user(username="po-tech", password="x", role="technician"),
    }


@pytest.fixture
def supplier(db):
    return Supplier.objects.create(name="PO Test Supplier")


def _create_po(client, supplier, **extra):
    payload = {
        "supplier": str(supplier.id),
        "items": [
            {"description": "P6 LED module", "quantity": 2, "unit_price": "100.00"},
            {"description": "Cat6 cable", "quantity": 3, "unit_price": "10.50"},
        ],
    }
    payload.update(extra)
    r = client.post("/api/procurement/orders/", payload, format="json")
    assert r.status_code == 201, r.content
    return r.json()


@pytest.mark.django_db
def test_nested_items_create_and_auto_total(people, supplier):
    body = _create_po(_client(people["finance"]), supplier)
    assert len(body["items"]) == 2
    assert Decimal(str(body["total_amount"])) == Decimal("231.50")
    po = PurchaseOrder.objects.get(pk=body["id"])
    assert po.total_amount == Decimal("231.50")
    assert po.ordered_by == people["finance"]


@pytest.mark.django_db
def test_po_number_auto_generated(people, supplier):
    body = _create_po(_client(people["finance"]), supplier)
    assert body["po_number"], "po_number should be auto-generated on save"
    # unique per PO
    other = _create_po(_client(people["finance"]), supplier)
    assert other["po_number"] != body["po_number"]


@pytest.mark.django_db
def test_typed_line_items(people, supplier):
    from apps.assets.models import AssetType, Brand, DeviceModel, MaterialType

    asset_type = AssetType.objects.create(name="PO SMD Screen")
    model = DeviceModel.objects.create(brand=Brand.objects.create(name="POB"), name="PO-55")
    material = MaterialType.objects.create(name="PO HDMI Cable")

    c = _client(people["finance"])
    body = _create_po(
        c, supplier,
        items=[
            {"description": "Serialized line", "quantity": 1, "unit_price": "500.00",
             "asset_type": str(asset_type.id), "device_model": str(model.id)},
            {"description": "Consumable line", "quantity": 10, "unit_price": "5.00",
             "material_type": str(material.id)},
        ],
    )
    by_desc = {i["description"]: i for i in body["items"]}  # ordering is by UUID pk, not insertion
    serialized, consumable = by_desc["Serialized line"], by_desc["Consumable line"]
    assert serialized["device_model"] == str(model.id)
    assert serialized["asset_type_name"] == "PO SMD Screen"
    assert consumable["material_type"] == str(material.id)
    assert consumable["material_type_name"] == "PO HDMI Cable"
    assert Decimal(str(body["total_amount"])) == Decimal("550.00")


@pytest.mark.django_db
def test_update_replaces_items_and_recalcs(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    r = c.patch(
        f"/api/procurement/orders/{body['id']}/",
        {"items": [{"description": "Single line", "quantity": 4, "unit_price": "25.00"}]},
        format="json",
    )
    assert r.status_code == 200, r.content
    po = PurchaseOrder.objects.get(pk=body["id"])
    assert po.items.count() == 1
    assert po.total_amount == Decimal("100.00")


@pytest.mark.django_db
def test_update_upserts_items_preserving_received_quantity(people, supplier):
    """Rows carrying an id are updated in place (received_quantity kept), new rows
    created, rows missing from the payload deleted — then total recalculated."""
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    po = PurchaseOrder.objects.get(pk=body["id"])
    kept = po.items.get(description="P6 LED module")
    dropped = po.items.get(description="Cat6 cable")
    PurchaseOrderItem.objects.filter(pk=kept.pk).update(received_quantity=1)

    r = c.patch(
        f"/api/procurement/orders/{po.pk}/",
        {"items": [
            {"id": str(kept.pk), "description": "P6 LED module v2", "quantity": 5, "unit_price": "20.00"},
            {"description": "New line", "quantity": 1, "unit_price": "3.00"},
        ]},
        format="json",
    )
    assert r.status_code == 200, r.content

    kept.refresh_from_db()
    assert kept.description == "P6 LED module v2"
    assert kept.quantity == 5
    assert kept.received_quantity == 1  # preserved — row updated in place, not recreated
    assert not PurchaseOrderItem.objects.filter(pk=dropped.pk).exists()  # missing → deleted
    po.refresh_from_db()
    assert po.items.count() == 2
    assert po.total_amount == Decimal("103.00")


@pytest.mark.django_db
def test_update_rejects_foreign_item_id(people, supplier):
    c = _client(people["finance"])
    body_a = _create_po(c, supplier)
    body_b = _create_po(c, supplier)
    foreign_item_id = body_b["items"][0]["id"]
    r = c.patch(
        f"/api/procurement/orders/{body_a['id']}/",
        {"items": [{"id": foreign_item_id, "description": "Hijack", "quantity": 1, "unit_price": "1.00"}]},
        format="json",
    )
    assert r.status_code == 400
    assert PurchaseOrderItem.objects.filter(pk=foreign_item_id, purchase_order=body_b["id"]).exists()


@pytest.mark.django_db
def test_received_quantity_read_only_everywhere(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    po = PurchaseOrder.objects.get(pk=body["id"])
    item = po.items.first()

    # Nested write attempt — silently ignored (read-only).
    r = c.patch(
        f"/api/procurement/orders/{po.pk}/",
        {"items": [{"id": str(item.pk), "description": item.description,
                    "quantity": item.quantity, "unit_price": str(item.unit_price),
                    "received_quantity": 99}]},
        format="json",
    )
    assert r.status_code == 200, r.content
    item.refresh_from_db()
    assert item.received_quantity == 0

    # Standalone write attempt — also ignored.
    r = c.patch(
        f"/api/procurement/order-items/{item.pk}/",
        {"received_quantity": 99},
        format="json",
    )
    assert r.status_code == 200, r.content
    item.refresh_from_db()
    assert item.received_quantity == 0


@pytest.mark.django_db
def test_item_writes_rejected_after_approval(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    pid = body["id"]
    assert c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "pending_approval"}, format="json").status_code == 200

    # pending_approval still allows item edits
    r = c.patch(
        f"/api/procurement/orders/{pid}/",
        {"items": [{"description": "Still editable", "quantity": 1, "unit_price": "5.00"}]},
        format="json",
    )
    assert r.status_code == 200, r.content

    assert _client(people["ops"]).post(f"/api/procurement/orders/{pid}/transition/", {"status": "approved"}, format="json").status_code == 200

    # approved — nested item writes rejected with a clear error
    r = c.patch(
        f"/api/procurement/orders/{pid}/",
        {"items": [{"description": "Too late", "quantity": 1, "unit_price": "5.00"}]},
        format="json",
    )
    assert r.status_code == 400
    assert "items" in r.json()
    po = PurchaseOrder.objects.get(pk=pid)
    assert po.items.count() == 1
    assert po.items.first().description == "Still editable"

    # non-item fields remain editable
    r = c.patch(f"/api/procurement/orders/{pid}/", {"expected_delivery": "2026-09-15"}, format="json")
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_status_not_writable_via_patch(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    r = c.patch(f"/api/procurement/orders/{body['id']}/", {"status": "received"}, format="json")
    assert r.status_code == 200
    assert PurchaseOrder.objects.get(pk=body["id"]).status == "draft"  # silently ignored (read-only)


@pytest.mark.django_db
def test_invalid_transition_rejected(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    # draft cannot jump straight to received
    r = c.post(f"/api/procurement/orders/{body['id']}/transition/", {"status": "received"}, format="json")
    assert r.status_code == 400
    assert PurchaseOrder.objects.get(pk=body["id"]).status == "draft"


@pytest.mark.django_db
def test_full_transition_flow_and_approved_by_stamp(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    pid = body["id"]

    r = c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "pending_approval"}, format="json")
    assert r.status_code == 200, r.content

    c_ops = _client(people["ops"])
    r = c_ops.post(f"/api/procurement/orders/{pid}/transition/", {"status": "approved", "notes": "ok"}, format="json")
    assert r.status_code == 200, r.content
    po = PurchaseOrder.objects.get(pk=pid)
    assert po.status == "approved"
    assert po.approved_by == people["ops"]

    assert c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "ordered"}, format="json").status_code == 200
    assert c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "partially_received"}, format="json").status_code == 200
    assert c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "received"}, format="json").status_code == 200
    # received is terminal
    assert c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "cancelled"}, format="json").status_code == 400


@pytest.mark.django_db
def test_transition_role_gated(people, supplier):
    body = _create_po(_client(people["finance"]), supplier)
    c_tech = _client(people["tech"])
    r = c_tech.post(f"/api/procurement/orders/{body['id']}/transition/", {"status": "pending_approval"}, format="json")
    assert r.status_code == 403
    # read stays open
    assert c_tech.get(f"/api/procurement/orders/{body['id']}/").status_code == 200


@pytest.mark.django_db
def test_standalone_item_endpoint_recalcs_total(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    r = c.post(
        "/api/procurement/order-items/",
        {"purchase_order": body["id"], "description": "Extra line", "quantity": 1, "unit_price": "8.50"},
        format="json",
    )
    assert r.status_code == 201, r.content
    po = PurchaseOrder.objects.get(pk=body["id"])
    assert po.total_amount == Decimal("240.00")

    item = PurchaseOrderItem.objects.get(pk=r.json()["id"])
    c.delete(f"/api/procurement/order-items/{item.pk}/")
    po.refresh_from_db()
    assert po.total_amount == Decimal("231.50")


@pytest.mark.django_db
def test_purchase_orders_alias_paths(people, supplier):
    """Web dashboard uses /purchase-orders/ + /purchase-order-items/; the mobile
    app keeps the legacy /orders/ + /order-items/ paths. Both must resolve."""
    c = _client(people["finance"])
    body = _create_po(c, supplier)
    pid = body["id"]

    for prefix in ("orders", "purchase-orders"):
        r = c.get(f"/api/procurement/{prefix}/")
        assert r.status_code == 200, (prefix, r.content)
        listing = r.json()
        results = listing.get("results", listing)
        assert any(po["id"] == pid for po in results), prefix
        assert c.get(f"/api/procurement/{prefix}/{pid}/").status_code == 200

    # transition works on the new path too
    r = c.post(f"/api/procurement/purchase-orders/{pid}/transition/", {"status": "pending_approval"}, format="json")
    assert r.status_code == 200, r.content

    item_id = body["items"][0]["id"]
    for prefix in ("order-items", "purchase-order-items"):
        assert c.get(f"/api/procurement/{prefix}/{item_id}/").status_code == 200, prefix

    # create via the new alias path
    r = c.post(
        "/api/procurement/purchase-orders/",
        {"supplier": str(supplier.id), "items": [{"description": "Alias line", "quantity": 1, "unit_price": "9.00"}]},
        format="json",
    )
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_transition_notes_appended_to_po_notes(people, supplier):
    c = _client(people["finance"])
    body = _create_po(c, supplier, notes="Initial note")
    pid = body["id"]

    r = c.post(
        f"/api/procurement/orders/{pid}/transition/",
        {"status": "pending_approval", "notes": "Please review urgently"},
        format="json",
    )
    assert r.status_code == 200, r.content
    po = PurchaseOrder.objects.get(pk=pid)
    assert po.notes.startswith("Initial note")
    last_line = po.notes.splitlines()[-1]
    assert last_line.startswith("[")  # timestamped: [YYYY-MM-DD HH:MM]
    assert "po-fin" in last_line
    assert "Draft → Pending Approval" in last_line
    assert "Please review urgently" in last_line

    # transition without notes leaves notes untouched
    notes_before = po.notes
    r = c.post(f"/api/procurement/orders/{pid}/transition/", {"status": "draft"}, format="json")
    assert r.status_code == 200, r.content
    po.refresh_from_db()
    assert po.notes == notes_before


@pytest.mark.django_db
def test_item_reparent_recalcs_both_purchase_orders(people, supplier):
    c = _client(people["finance"])
    body_a = _create_po(c, supplier)  # 231.50
    body_b = _create_po(c, supplier)  # 231.50
    item = PurchaseOrderItem.objects.get(pk=body_a["items"][0]["id"])
    moved_value = item.line_total

    r = c.patch(
        f"/api/procurement/order-items/{item.pk}/",
        {"purchase_order": body_b["id"]},
        format="json",
    )
    assert r.status_code == 200, r.content
    po_a = PurchaseOrder.objects.get(pk=body_a["id"])
    po_b = PurchaseOrder.objects.get(pk=body_b["id"])
    assert po_a.total_amount == Decimal("231.50") - moved_value  # old parent recalced too
    assert po_b.total_amount == Decimal("231.50") + moved_value


# ---------------------------------------------------------------------------
# WF-03: raise PO from project BOM shortage
# ---------------------------------------------------------------------------

@pytest.fixture
def bom_project(db):
    """Project with three BOM lines: shortages 3, 4 and 0."""
    from apps.assets.models import AssetType, Brand, DeviceModel, MaterialType
    from apps.teams.models import BOMAllocation, Project, ProjectBOMLine

    project = Project.objects.create(name="Shortage Project")
    asset_type = AssetType.objects.create(name="Shortage Screen")
    model = DeviceModel.objects.create(brand=Brand.objects.create(name="ShortB"), name="SB-55")
    material = MaterialType.objects.create(name="Shortage Cable")

    # qty 5, 2 allocated (+ a cancelled allocation that must NOT count) → shortage 3
    line_a = ProjectBOMLine.objects.create(
        project=project, description="P10 cabinet", quantity=5, unit_price=Decimal("120.00"),
        asset_type=asset_type, device_model=model,
    )
    BOMAllocation.objects.create(bom_line=line_a, quantity=2)
    BOMAllocation.objects.create(bom_line=line_a, quantity=5, status="cancelled")
    # qty 4, nothing allocated → shortage 4
    line_b = ProjectBOMLine.objects.create(
        project=project, description="HDMI cable", quantity=4, unit_price=Decimal("7.50"),
        material_type=material,
    )
    # qty 2, fully allocated → shortage 0 (must be skipped)
    line_c = ProjectBOMLine.objects.create(
        project=project, description="Mounting kit", quantity=2, unit_price=Decimal("30.00"),
    )
    BOMAllocation.objects.create(bom_line=line_c, quantity=2)

    return {"project": project, "line_a": line_a, "line_b": line_b, "line_c": line_c}


@pytest.mark.django_db
def test_from_shortage_creates_draft_po(people, supplier, bom_project):
    c = _client(people["finance"])
    r = c.post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(bom_project["project"].id), "supplier": str(supplier.id), "currency": "USD"},
        format="json",
    )
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "draft"
    assert body["supplier"] == str(supplier.id)
    assert body["currency"] == "USD"
    assert body["po_number"]

    by_desc = {i["description"]: i for i in body["items"]}
    assert set(by_desc) == {"P10 cabinet", "HDMI cable"}  # zero-shortage line skipped

    line_a, line_b = bom_project["line_a"], bom_project["line_b"]
    item_a, item_b = by_desc["P10 cabinet"], by_desc["HDMI cable"]
    assert item_a["quantity"] == 3  # shortage, not full quantity; cancelled alloc ignored
    assert Decimal(str(item_a["unit_price"])) == Decimal("120.00")
    assert item_a["bom_line"] == str(line_a.id)
    assert item_a["asset_type"] == str(line_a.asset_type_id)
    assert item_a["device_model"] == str(line_a.device_model_id)
    assert item_b["quantity"] == 4
    assert item_b["bom_line"] == str(line_b.id)
    assert item_b["material_type"] == str(line_b.material_type_id)

    po = PurchaseOrder.objects.get(pk=body["id"])
    assert po.total_amount == Decimal("390.00")  # 3*120 + 4*7.50
    assert po.ordered_by == people["finance"]
    assert po.items.filter(bom_line=line_a).exists()


@pytest.mark.django_db
def test_from_shortage_line_ids_subset_honored(people, supplier, bom_project):
    c = _client(people["finance"])
    r = c.post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(bom_project["project"].id), "supplier": str(supplier.id),
         "line_ids": [str(bom_project["line_b"].id)]},
        format="json",
    )
    assert r.status_code == 201, r.content
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["description"] == "HDMI cable"
    assert body["items"][0]["bom_line"] == str(bom_project["line_b"].id)
    assert Decimal(str(body["total_amount"])) == Decimal("30.00")  # 4 * 7.50


@pytest.mark.django_db
def test_from_shortage_400_when_nothing_to_order(people, supplier, bom_project):
    c = _client(people["finance"])
    before = PurchaseOrder.objects.count()

    # only a zero-shortage line selected
    r = c.post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(bom_project["project"].id), "supplier": str(supplier.id),
         "line_ids": [str(bom_project["line_c"].id)]},
        format="json",
    )
    assert r.status_code == 400, r.content

    # project whose lines are all covered
    from apps.teams.models import BOMAllocation, Project, ProjectBOMLine
    covered = Project.objects.create(name="Covered Project")
    line = ProjectBOMLine.objects.create(
        project=covered, description="Done line", quantity=2, unit_price=Decimal("1.00")
    )
    BOMAllocation.objects.create(bom_line=line, quantity=2)
    r = c.post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(covered.id), "supplier": str(supplier.id)},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert PurchaseOrder.objects.count() == before  # nothing created


@pytest.mark.django_db
def test_from_shortage_rejects_foreign_line_ids(people, supplier, bom_project):
    from apps.teams.models import Project, ProjectBOMLine

    other = Project.objects.create(name="Other Project")
    foreign = ProjectBOMLine.objects.create(
        project=other, description="Foreign line", quantity=1, unit_price=Decimal("9.99")
    )
    c = _client(people["finance"])
    r = c.post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(bom_project["project"].id), "supplier": str(supplier.id),
         "line_ids": [str(foreign.id)]},
        format="json",
    )
    assert r.status_code == 400
    assert "line_ids" in r.json()


@pytest.mark.django_db
def test_from_shortage_role_gated(people, supplier, bom_project):
    r = _client(people["tech"]).post(
        "/api/procurement/purchase-orders/from-shortage/",
        {"project": str(bom_project["project"].id), "supplier": str(supplier.id)},
        format="json",
    )
    assert r.status_code == 403


@pytest.mark.django_db
def test_item_bom_line_writable_on_create(people, supplier, bom_project):
    """bom_line is exposed on item serializers and accepted on manual PO create."""
    line = bom_project["line_b"]
    c = _client(people["finance"])
    body = _create_po(
        c, supplier,
        items=[{"description": "Manual link", "quantity": 1, "unit_price": "5.00",
                "bom_line": str(line.id)}],
    )
    assert body["items"][0]["bom_line"] == str(line.id)
    item = PurchaseOrderItem.objects.get(pk=body["items"][0]["id"])
    assert item.bom_line_id == line.id
    assert line.po_items.filter(pk=item.pk).exists()


# ---------------------------------------------------------------------------
# WF-04: goods receipt against PO (serials + batch at the door)
# ---------------------------------------------------------------------------

@pytest.fixture
def receivable_po(db, people, supplier):
    """PO in 'ordered' status with a serialized line (3 screens) and a
    consumable line (10 cable units)."""
    from apps.assets.models import AssetType, Brand, DeviceModel, MaterialType

    asset_type = AssetType.objects.create(name="GRN Screen")
    model = DeviceModel.objects.create(brand=Brand.objects.create(name="GRNB"), name="GRN-55")
    material = MaterialType.objects.create(name="GRN Cable", unit="meter")

    po = PurchaseOrder.objects.create(
        supplier=supplier, ordered_by=people["finance"], status=PurchaseOrder.Status.ORDERED
    )
    serialized = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Serialized screens", quantity=3,
        unit_price=Decimal("250.00"), asset_type=asset_type, device_model=model,
    )
    consumable = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Cable drum", quantity=10,
        unit_price=Decimal("4.00"), material_type=material,
    )
    po.recalc_total()
    return {
        "po": po, "serialized": serialized, "consumable": consumable,
        "asset_type": asset_type, "model": model, "material": material,
    }


def _receive(client, po_id, lines, **extra):
    payload = {"lines": lines}
    payload.update(extra)
    return client.post(f"/api/procurement/purchase-orders/{po_id}/receive/", payload, format="json")


@pytest.mark.django_db
def test_receive_serialized_line_creates_devices(people, supplier, receivable_po):
    from datetime import date

    from apps.assets.models import Device, DeviceLifecycleEvent

    po, serialized = receivable_po["po"], receivable_po["serialized"]
    c = _client(people["finance"])
    r = _receive(c, po.pk, [{
        "po_item": str(serialized.pk), "quantity": 3, "batch_number": "B-77",
        "serial_numbers": ["GRN-SN-A", "GRN-SN-B", "GRN-SN-C"],
    }], reference="DN-991")
    assert r.status_code == 201, r.content
    body = r.json()

    # Response contract: id, grn_number, purchase_order, created_devices, lines
    assert body["grn_number"].startswith("GRN")
    assert body["purchase_order"] == str(po.pk)
    assert len(body["created_devices"]) == 3
    assert all(d["asset_code"] for d in body["created_devices"])
    assert {d["serial_number"] for d in body["created_devices"]} == {"GRN-SN-A", "GRN-SN-B", "GRN-SN-C"}
    assert len(body["lines"]) == 1
    assert body["lines"][0]["po_item"] == str(serialized.pk)
    assert body["lines"][0]["serial_numbers"] == ["GRN-SN-A", "GRN-SN-B", "GRN-SN-C"]

    device = Device.objects.get(serial_number="GRN-SN-A")
    assert device.device_model == receivable_po["model"]
    assert device.asset_type == receivable_po["asset_type"]
    assert device.batch_number == "B-77"
    assert device.supplier == supplier
    assert device.purchase_price == Decimal("250.00")
    assert device.purchase_date == date.today()
    assert device.invoice_reference == body["grn_number"]
    assert device.source == "third_party"
    assert device.status == "procured"
    assert device.project is None
    # journalled by the Wave-1 signals
    event = DeviceLifecycleEvent.objects.get(device=device, description="Registered")
    assert event.to_value == "procured"

    serialized.refresh_from_db()
    assert serialized.received_quantity == 3
    po.refresh_from_db()
    assert po.status == "partially_received"  # consumable line still outstanding
    assert f"[GRN {body['grn_number']}]" in po.notes


@pytest.mark.django_db
def test_receive_partial_then_full_advances_status(people, receivable_po):
    from apps.assets.models import Device

    po, serialized, consumable = (
        receivable_po["po"], receivable_po["serialized"], receivable_po["consumable"]
    )
    c = _client(people["finance"])

    r = _receive(c, po.pk, [
        {"po_item": str(serialized.pk), "quantity": 2, "serial_numbers": ["PF-1", "PF-2"]},
        {"po_item": str(consumable.pk), "quantity": 4},
    ])
    assert r.status_code == 201, r.content
    serialized.refresh_from_db(); consumable.refresh_from_db(); po.refresh_from_db()
    assert serialized.received_quantity == 2
    assert consumable.received_quantity == 4
    assert po.status == "partially_received"

    r = _receive(c, po.pk, [
        {"po_item": str(serialized.pk), "quantity": 1, "serial_numbers": ["PF-3"]},
        {"po_item": str(consumable.pk), "quantity": 6},
    ])
    assert r.status_code == 201, r.content
    serialized.refresh_from_db(); consumable.refresh_from_db(); po.refresh_from_db()
    assert serialized.received_quantity == 3
    assert consumable.received_quantity == 10
    assert po.status == "received"
    assert Device.objects.filter(serial_number__in=["PF-1", "PF-2", "PF-3"]).count() == 3


@pytest.mark.django_db
def test_receive_serial_count_mismatch_400(people, receivable_po):
    from apps.assets.models import Device
    from apps.inventory.models import GoodsReceipt

    po, serialized = receivable_po["po"], receivable_po["serialized"]
    before_receipts = GoodsReceipt.objects.count()
    r = _receive(_client(people["finance"]), po.pk, [{
        "po_item": str(serialized.pk), "quantity": 2, "serial_numbers": ["ONLY-ONE"],
    }])
    assert r.status_code == 400, r.content
    assert not Device.objects.filter(serial_number="ONLY-ONE").exists()
    assert GoodsReceipt.objects.count() == before_receipts  # nothing partially created
    serialized.refresh_from_db()
    assert serialized.received_quantity == 0
    po.refresh_from_db()
    assert po.status == "ordered"


@pytest.mark.django_db
def test_receive_duplicate_serial_400_global_and_in_payload(people, receivable_po):
    from apps.assets.models import Device
    from apps.inventory.models import GoodsReceipt

    po, serialized, model = receivable_po["po"], receivable_po["serialized"], receivable_po["model"]
    Device.objects.create(device_model=model, serial_number="TAKEN-1")
    c = _client(people["finance"])
    devices_before = Device.objects.count()
    receipts_before = GoodsReceipt.objects.count()

    # global collision with an existing device
    r = _receive(c, po.pk, [{
        "po_item": str(serialized.pk), "quantity": 2, "serial_numbers": ["TAKEN-1", "FRESH-1"],
    }])
    assert r.status_code == 400, r.content
    assert "TAKEN-1" in str(r.json())

    # repeated within the payload
    r = _receive(c, po.pk, [{
        "po_item": str(serialized.pk), "quantity": 2, "serial_numbers": ["TWICE-1", "TWICE-1"],
    }])
    assert r.status_code == 400, r.content
    assert "TWICE-1" in str(r.json())

    assert Device.objects.count() == devices_before  # nothing created at all
    assert GoodsReceipt.objects.count() == receipts_before
    serialized.refresh_from_db()
    assert serialized.received_quantity == 0


@pytest.mark.django_db
def test_receive_consumable_updates_stock_and_movement(people, receivable_po):
    from apps.inventory.models import GoodsReceipt, InventoryItem, StockMovement

    po, consumable, material = (
        receivable_po["po"], receivable_po["consumable"], receivable_po["material"]
    )
    stock = InventoryItem.objects.create(material_type=material, quantity=5)

    r = _receive(_client(people["finance"]), po.pk, [{
        "po_item": str(consumable.pk), "quantity": 10,
    }])
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["created_devices"] == []

    stock.refresh_from_db()
    assert stock.quantity == 15  # existing item topped up, not duplicated
    assert InventoryItem.objects.filter(material_type=material).count() == 1
    movement = StockMovement.objects.get(item=stock, movement_type="in")
    assert movement.quantity == 10
    assert movement.reference == body["grn_number"]

    receipt = GoodsReceipt.objects.get(pk=body["id"])
    assert receipt.purchase_order == po
    line = receipt.lines.get()
    assert line.po_item == consumable
    assert line.inventory_item == stock

    consumable.refresh_from_db()
    assert consumable.received_quantity == 10


@pytest.mark.django_db
def test_receive_consumable_creates_inventory_item_when_missing(people, receivable_po):
    from apps.inventory.models import InventoryItem, StockMovement

    po, consumable, material = (
        receivable_po["po"], receivable_po["consumable"], receivable_po["material"]
    )
    assert not InventoryItem.objects.filter(material_type=material).exists()

    r = _receive(_client(people["finance"]), po.pk, [{
        "po_item": str(consumable.pk), "quantity": 7,
    }])
    assert r.status_code == 201, r.content
    item = InventoryItem.objects.get(material_type=material)
    assert item.quantity == 7
    assert item.sku  # auto-generated
    assert StockMovement.objects.filter(item=item, movement_type="in", quantity=7).exists()


@pytest.mark.django_db
def test_receive_mixed_po_single_call(people, receivable_po):
    from apps.assets.models import Device
    from apps.inventory.models import GoodsReceipt, InventoryItem

    po, serialized, consumable = (
        receivable_po["po"], receivable_po["serialized"], receivable_po["consumable"]
    )
    r = _receive(_client(people["finance"]), po.pk, [
        {"po_item": str(serialized.pk), "quantity": 3, "batch_number": "MX-1",
         "serial_numbers": ["MX-A", "MX-B", "MX-C"]},
        {"po_item": str(consumable.pk), "quantity": 10},
    ], notes="Full delivery")
    assert r.status_code == 201, r.content
    body = r.json()
    assert len(body["created_devices"]) == 3
    assert len(body["lines"]) == 2
    assert Device.objects.filter(serial_number__startswith="MX-").count() == 3
    assert InventoryItem.objects.get(material_type=receivable_po["material"]).quantity == 10
    po.refresh_from_db()
    assert po.status == "received"  # everything arrived in one delivery
    receipt = GoodsReceipt.objects.get(pk=body["id"])
    assert receipt.notes == "Full delivery"
    assert receipt.received_by == people["finance"]


@pytest.mark.django_db
def test_receive_over_receive_400(people, receivable_po):
    po, serialized, consumable = (
        receivable_po["po"], receivable_po["serialized"], receivable_po["consumable"]
    )
    c = _client(people["finance"])
    # more than ordered
    r = _receive(c, po.pk, [{
        "po_item": str(serialized.pk), "quantity": 4,
        "serial_numbers": ["OV-1", "OV-2", "OV-3", "OV-4"],
    }])
    assert r.status_code == 400, r.content

    # more than remaining after a partial receipt
    assert _receive(c, po.pk, [{"po_item": str(consumable.pk), "quantity": 8}]).status_code == 201
    r = _receive(c, po.pk, [{"po_item": str(consumable.pk), "quantity": 3}])
    assert r.status_code == 400, r.content
    consumable.refresh_from_db()
    assert consumable.received_quantity == 8


@pytest.mark.django_db
def test_receive_rejected_for_wrong_po_status(people, supplier, receivable_po):
    c = _client(people["finance"])
    for bad_status in ("draft", "pending_approval", "approved", "received", "cancelled"):
        po = PurchaseOrder.objects.create(supplier=supplier, status=bad_status)
        item = PurchaseOrderItem.objects.create(
            purchase_order=po, description="Line", quantity=1, unit_price=Decimal("1.00"),
            material_type=receivable_po["material"],
        )
        r = _receive(c, po.pk, [{"po_item": str(item.pk), "quantity": 1}])
        assert r.status_code == 400, (bad_status, r.content)


@pytest.mark.django_db
def test_receive_line_without_type_400(people, supplier):
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    untyped = PurchaseOrderItem.objects.create(
        purchase_order=po, description="Untyped line", quantity=2, unit_price=Decimal("9.00"),
    )
    r = _receive(_client(people["finance"]), po.pk, [{"po_item": str(untyped.pk), "quantity": 2}])
    assert r.status_code == 400, r.content
    assert "no device model or material type" in str(r.json())


@pytest.mark.django_db
def test_receive_foreign_po_item_400(people, supplier, receivable_po):
    other = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    foreign = PurchaseOrderItem.objects.create(
        purchase_order=other, description="Foreign", quantity=1, unit_price=Decimal("2.00"),
        material_type=receivable_po["material"],
    )
    r = _receive(_client(people["finance"]), receivable_po["po"].pk,
                 [{"po_item": str(foreign.pk), "quantity": 1}])
    assert r.status_code == 400, r.content
    foreign.refresh_from_db()
    assert foreign.received_quantity == 0


@pytest.mark.django_db
def test_receive_bom_line_sets_project_and_allocation(people, receivable_po):
    from apps.assets.models import Device
    from apps.teams.models import BOMAllocation, Project, ProjectBOMLine

    po, serialized = receivable_po["po"], receivable_po["serialized"]
    project = Project.objects.create(name="GRN Project")
    bom_line = ProjectBOMLine.objects.create(
        project=project, description="Screens", quantity=3, unit_price=Decimal("250.00"),
        asset_type=receivable_po["asset_type"], device_model=receivable_po["model"],
    )
    serialized.bom_line = bom_line
    serialized.save(update_fields=["bom_line", "updated_at"])

    r = _receive(_client(people["finance"]), po.pk, [{
        "po_item": str(serialized.pk), "quantity": 2, "serial_numbers": ["BOM-1", "BOM-2"],
    }])
    assert r.status_code == 201, r.content

    devices = Device.objects.filter(serial_number__in=["BOM-1", "BOM-2"])
    assert devices.count() == 2
    assert all(d.project == project for d in devices)

    allocations = BOMAllocation.objects.filter(bom_line=bom_line)
    assert allocations.count() == 2
    assert all(a.status == "allocated" and a.quantity == 1 and a.device is not None
               for a in allocations)
    assert {a.device.serial_number for a in allocations} == {"BOM-1", "BOM-2"}
    assert all(a.allocated_by == people["finance"] for a in allocations)

    bom_line.refresh_from_db()
    assert bom_line.allocated_quantity == 2
    assert bom_line.shortage == 1


@pytest.mark.django_db
def test_receive_role_gated(people, receivable_po):
    r = _receive(_client(people["tech"]), receivable_po["po"].pk, [{
        "po_item": str(receivable_po["consumable"].pk), "quantity": 1,
    }])
    assert r.status_code == 403
