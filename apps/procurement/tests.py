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
