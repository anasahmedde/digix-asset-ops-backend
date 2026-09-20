# Tests will be added alongside model implementations.
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import MaterialType
from apps.inventory.models import InventoryItem


@pytest.fixture
def ops(db):
    return User.objects.create_user(username="inv-ops", password="x", role="ops_manager")


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def items(db):
    cable = MaterialType.objects.create(name="Inv Cable", unit="meter")
    mount = MaterialType.objects.create(name="Inv Mount", unit="piece")
    screw = MaterialType.objects.create(name="Inv Screw", unit="box")
    return [
        InventoryItem.objects.create(material_type=cable, quantity=10, unit_cost=100),  # value 1000
        InventoryItem.objects.create(material_type=mount, quantity=50, unit_cost=5),    # value 250
        InventoryItem.objects.create(material_type=screw, quantity=999, unit_cost=None),  # unpriced
    ]


@pytest.mark.django_db
def test_items_expose_per_row_total_value(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"page_size": 100})
    assert r.status_code == 200, r.content
    by_name = {row["material_name"]: row for row in r.data["results"]}
    assert float(by_name["Inv Cable"]["total_value"]) == 1000
    assert float(by_name["Inv Mount"]["total_value"]) == 250
    assert by_name["Inv Screw"]["total_value"] is None


@pytest.mark.django_db
def test_items_order_by_total_value_desc_puts_unpriced_last(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"ordering": "-total_value", "page_size": 100})
    assert r.status_code == 200, r.content
    names = [row["material_name"] for row in r.data["results"]]
    assert names == ["Inv Cable", "Inv Mount", "Inv Screw"]


@pytest.mark.django_db
def test_items_order_by_total_value_asc(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"ordering": "total_value", "page_size": 100})
    names = [row["material_name"] for row in r.data["results"]]
    assert names == ["Inv Screw", "Inv Mount", "Inv Cable"]


# ── Wave 2: Issuance project fields + legacy flow regression ─────────

@pytest.mark.django_db
def test_legacy_direct_issuance_still_decrements_and_journals(ops, items):
    from apps.inventory.models import Issuance, StockMovement

    cable = items[0]  # quantity 10
    c = _client(ops)
    r = c.post("/api/inventory/issuances/", {
        "item": str(cable.pk), "quantity": 4, "reason": "Site consumption",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["issued_to_project"] is None
    assert r.data["bom_line"] is None
    assert r.data["project_name"] is None

    cable.refresh_from_db()
    assert cable.quantity == 6
    issuance = Issuance.objects.get(pk=r.data["id"])
    movement = StockMovement.objects.get(item=cable, movement_type="out")
    assert movement.quantity == 4
    assert movement.reference == issuance.issue_number

    # over-issue is still rejected
    r = c.post("/api/inventory/issuances/", {
        "item": str(cable.pk), "quantity": 999,
    }, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_direct_issuance_accepts_project_and_bom_line(ops, items):
    from apps.teams.models import Project, ProjectBOMLine

    project = Project.objects.create(name="Issuance Project")
    line = ProjectBOMLine.objects.create(project=project, description="Cable", quantity=3)
    cable = items[0]
    c = _client(ops)
    r = c.post("/api/inventory/issuances/", {
        "item": str(cable.pk), "quantity": 2,
        "issued_to_project": str(project.pk), "bom_line": str(line.pk),
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["issued_to_project"] == project.pk
    assert r.data["bom_line"] == line.pk
    assert r.data["project_name"] == "Issuance Project"
    cable.refresh_from_db()
    assert cable.quantity == 8


# ── Wave 2: goods receipt model gains PO linkage; legacy flow must survive ──

@pytest.mark.django_db
def test_legacy_goods_receipt_still_requires_item_and_increments_stock(ops, items):
    from apps.inventory.models import GoodsReceipt, StockMovement

    cable = items[0]  # quantity 10
    c = _client(ops)
    r = c.post("/api/inventory/receipts/", {
        "item": str(cable.pk), "quantity": 5, "reference": "Legacy DN",
    }, format="json")
    assert r.status_code == 201, r.content
    body = r.data
    assert body["grn_number"]
    assert body["purchase_order"] is None
    assert body["lines"] == []

    cable.refresh_from_db()
    assert cable.quantity == 15
    receipt = GoodsReceipt.objects.get(pk=body["id"])
    assert receipt.received_by == ops
    movement = StockMovement.objects.get(item=cable, movement_type="in")
    assert movement.quantity == 5
    assert movement.reference == receipt.grn_number

    # item/quantity stay mandatory on the legacy endpoint even though the
    # model now allows null for PO-level receipts
    assert c.post("/api/inventory/receipts/", {"quantity": 5}, format="json").status_code == 400
    assert c.post("/api/inventory/receipts/", {"item": str(cable.pk)}, format="json").status_code == 400


# ── Excel export (XC-01) ──────────────────────────────────────────────

import io as _io

from openpyxl import load_workbook as _load_workbook

from apps.accounts.models import AuditLog as _AuditLog


def _sheet_rows(resp):
    wb = _load_workbook(_io.BytesIO(resp.content), read_only=True)
    return [list(row) for row in wb.active.iter_rows(values_only=True)]


@pytest.mark.django_db
def test_inventory_export_happy_path(ops, items):
    resp = _client(ops).get("/api/inventory/items/export/")
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    rows = _sheet_rows(resp)
    assert rows[0] == [
        "SKU", "Material", "Category", "Quantity", "Min Stock Level",
        "Location", "Unit Cost", "Low Stock",
    ]
    assert len(rows) == 4  # header + 3 items
    by_material = {r[1]: r for r in rows[1:]}
    assert by_material["Inv Cable"][3] == 10
    assert by_material["Inv Cable"][7] == "No"

    log = _AuditLog.objects.filter(action="export", resource_type="inventory_item").latest("created_at")
    assert log.detail["count"] == 3
    assert log.user_id == ops.id


@pytest.mark.django_db
def test_inventory_export_applies_filters_and_low_stock_flag(ops, items):
    transit_type = MaterialType.objects.create(name="Inv Transit Panel", unit="piece")
    InventoryItem.objects.create(
        material_type=transit_type, quantity=2, min_stock_level=5, location="in_transit"
    )

    resp = _client(ops).get("/api/inventory/items/export/", {"location": "in_transit"})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert len(rows) == 2
    assert rows[1][1] == "Inv Transit Panel"
    assert rows[1][5] == "In Transit"
    assert rows[1][7] == "Yes"  # 2 <= min_stock_level 5

    log = _AuditLog.objects.filter(action="export", resource_type="inventory_item").latest("created_at")
    assert log.detail == {"count": 1, "params": {"location": "in_transit"}}


# ── Wave 5: ?low_stock= drill-down + category search parity ──────────


@pytest.fixture
def stocked_items(db):
    low_type = MaterialType.objects.create(name="Inv Low Panel", unit="piece")
    ok_type = MaterialType.objects.create(name="Inv OK Panel", unit="piece")
    low = InventoryItem.objects.create(material_type=low_type, quantity=3, min_stock_level=5)
    ok = InventoryItem.objects.create(material_type=ok_type, quantity=50, min_stock_level=5)
    return low, ok


@pytest.mark.django_db
def test_items_low_stock_filter(ops, stocked_items):
    c = _client(ops)
    r = c.get("/api/inventory/items/", {"low_stock": "true", "page_size": 100})
    assert r.status_code == 200, r.content
    names = {row["material_name"] for row in r.data["results"]}
    assert names == {"Inv Low Panel"}

    r = c.get("/api/inventory/items/", {"low_stock": "false", "page_size": 100})
    names = {row["material_name"] for row in r.data["results"]}
    assert names == {"Inv OK Panel"}

    # Unknown values are ignored — both rows come back.
    r = c.get("/api/inventory/items/", {"low_stock": "maybe", "page_size": 100})
    assert len(r.data["results"]) == 2


@pytest.mark.django_db
def test_items_low_stock_boundary_is_inclusive(ops, db):
    edge_type = MaterialType.objects.create(name="Inv Edge Panel", unit="piece")
    InventoryItem.objects.create(material_type=edge_type, quantity=5, min_stock_level=5)

    r = _client(ops).get("/api/inventory/items/", {"low_stock": "true", "page_size": 100})
    assert "Inv Edge Panel" in {row["material_name"] for row in r.data["results"]}


@pytest.mark.django_db
def test_items_low_stock_applies_to_export(ops, stocked_items):
    resp = _client(ops).get("/api/inventory/items/export/", {"low_stock": "true"})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert len(rows) == 2  # header + the one low item
    assert rows[1][1] == "Inv Low Panel"
    assert rows[1][7] == "Yes"

    log = _AuditLog.objects.filter(action="export", resource_type="inventory_item").latest("created_at")
    assert log.detail == {"count": 1, "params": {"low_stock": "true"}}


@pytest.mark.django_db
def test_items_search_by_category_name(ops, db):
    from apps.inventory.models import InventoryCategory

    cat = InventoryCategory.objects.create(name="Fasteners")
    bolt = MaterialType.objects.create(name="Inv Bolt", unit="box")
    InventoryItem.objects.create(material_type=bolt, category=cat, quantity=10)
    other = MaterialType.objects.create(name="Inv Other", unit="box")
    InventoryItem.objects.create(material_type=other, quantity=10)

    r = _client(ops).get("/api/inventory/items/", {"search": "Fasteners", "page_size": 100})
    assert r.status_code == 200, r.content
    names = {row["material_name"] for row in r.data["results"]}
    assert names == {"Inv Bolt"}


# ---------------------------------------------------------------------------
# Unique (serialized) inventory items — InventoryUnit
# ---------------------------------------------------------------------------
from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.assets.models import Brand  # noqa: E402
from apps.inventory.models import InventoryUnit  # noqa: E402


@pytest.fixture
def unit_refs(db):
    return {
        "material": MaterialType.objects.create(name="Inv Media Player", unit="piece"),
        "brand": Brand.objects.create(name="Inv Brand"),
    }


def _unit_payload(refs, **overrides):
    payload = {
        "serial_number": "SN-UNIQ-001",
        "material_type": str(refs["material"].id),
        "brand": str(refs["brand"].id),
        "model_name": "MP-500",
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_unique_item_created_with_generated_code_and_no_warranty(ops, unit_refs):
    r = _client(ops).post("/api/inventory/units/", _unit_payload(unit_refs), format="json")
    assert r.status_code == 201, r.content
    assert r.data["unit_code"].startswith("IVU-")
    assert r.data["serial_number"] == "SN-UNIQ-001"
    assert r.data["status"] == InventoryUnit.Status.IN_STOCK
    assert r.data["has_warranty"] is False
    assert r.data["warranty_state"] == "none"


@pytest.mark.django_db
def test_serial_number_is_unique_across_units(ops, unit_refs):
    c = _client(ops)
    assert c.post("/api/inventory/units/", _unit_payload(unit_refs), format="json").status_code == 201
    dup = c.post("/api/inventory/units/", _unit_payload(unit_refs), format="json")
    assert dup.status_code == 400, dup.content
    assert "serial_number" in dup.data


@pytest.mark.django_db
def test_warranty_end_derived_from_months(ops, unit_refs):
    payload = _unit_payload(
        unit_refs,
        has_warranty=True,
        warranty_type="manufacturer",
        warranty_start="2026-01-15",
        warranty_months=12,
    )
    r = _client(ops).post("/api/inventory/units/", payload, format="json")
    assert r.status_code == 201, r.content
    assert r.data["warranty_end"] == "2027-01-15"
    assert r.data["warranty_state"] == "active"
    assert r.data["is_under_warranty"] is True


@pytest.mark.django_db
def test_warranty_requires_start_when_flagged(ops, unit_refs):
    """A unit's cover is always its supplier's, so only the start date is asked for."""
    r = _client(ops).post(
        "/api/inventory/units/", _unit_payload(unit_refs, has_warranty=True), format="json"
    )
    assert r.status_code == 400, r.content
    assert "warranty_start" in r.data and "warranty_type" not in r.data


@pytest.mark.django_db
def test_warranty_needs_end_date_or_months(ops, unit_refs):
    payload = _unit_payload(
        unit_refs, has_warranty=True, warranty_type="supplier", warranty_start="2026-01-15"
    )
    r = _client(ops).post("/api/inventory/units/", payload, format="json")
    assert r.status_code == 400, r.content
    assert "warranty_end" in r.data


@pytest.mark.django_db
def test_expired_warranty_reports_expired_state(ops, unit_refs):
    unit = InventoryUnit.objects.create(
        serial_number="SN-EXPIRED",
        material_type=unit_refs["material"],
        has_warranty=True,
        warranty_type="manufacturer",
        warranty_start=timezone.now().date() - timedelta(days=800),
        warranty_end=timezone.now().date() - timedelta(days=1),
    )
    assert unit.warranty_state == "expired"
    assert unit.is_under_warranty is False


@pytest.mark.django_db
def test_warranty_filter_splits_active_expired_and_none(ops, unit_refs):
    m = unit_refs["material"]
    InventoryUnit.objects.create(
        serial_number="SN-A", material_type=m, has_warranty=True, warranty_type="manufacturer",
        warranty_start=timezone.now().date(), warranty_end=timezone.now().date() + timedelta(days=30),
    )
    InventoryUnit.objects.create(
        serial_number="SN-B", material_type=m, has_warranty=True, warranty_type="supplier",
        warranty_start=timezone.now().date() - timedelta(days=60), warranty_end=timezone.now().date() - timedelta(days=1),
    )
    InventoryUnit.objects.create(serial_number="SN-C", material_type=m)

    c = _client(ops)
    for value, expected in (("active", "SN-A"), ("expired", "SN-B"), ("none", "SN-C")):
        r = c.get("/api/inventory/units/", {"warranty": value, "page_size": 100})
        assert r.status_code == 200, r.content
        serials = [row["serial_number"] for row in r.data["results"]]
        assert serials == [expected], f"warranty={value} returned {serials}"


@pytest.mark.django_db
def test_status_transition_is_guarded(ops, unit_refs):
    unit = InventoryUnit.objects.create(serial_number="SN-T", material_type=unit_refs["material"])
    c = _client(ops)

    ok = c.post(f"/api/inventory/units/{unit.id}/transition/", {"status": "reserved"}, format="json")
    assert ok.status_code == 200, ok.content
    assert ok.data["status"] == "reserved"

    # reserved -> returned is not in VALID_TRANSITIONS
    bad = c.post(f"/api/inventory/units/{unit.id}/transition/", {"status": "returned"}, format="json")
    assert bad.status_code == 400, bad.content
    assert "Cannot move" in bad.data["detail"]


@pytest.mark.django_db
def test_scrapped_is_terminal(ops, unit_refs):
    unit = InventoryUnit.objects.create(
        serial_number="SN-S", material_type=unit_refs["material"],
        status=InventoryUnit.Status.SCRAPPED,
    )
    r = _client(ops).post(
        f"/api/inventory/units/{unit.id}/transition/", {"status": "in_stock"}, format="json"
    )
    assert r.status_code == 400, r.content
    assert "none" in r.data["detail"]


@pytest.mark.django_db
def test_clearing_has_warranty_wipes_warranty_fields(ops, unit_refs):
    unit = InventoryUnit.objects.create(
        serial_number="SN-W", material_type=unit_refs["material"], has_warranty=True,
        warranty_type="manufacturer", warranty_start=timezone.now().date(),
        warranty_end=timezone.now().date() + timedelta(days=365),
    )
    unit.has_warranty = False
    unit.save()
    unit.refresh_from_db()
    assert unit.warranty_type == ""
    assert unit.warranty_start is None
    assert unit.warranty_end is None
    assert unit.warranty_state == "none"


@pytest.mark.django_db
def test_units_summary_counts(ops, unit_refs):
    m = unit_refs["material"]
    InventoryUnit.objects.create(
        serial_number="SN-1", material_type=m, has_warranty=True, warranty_type="manufacturer",
        warranty_start=timezone.now().date(), warranty_end=timezone.now().date() + timedelta(days=10),
    )
    InventoryUnit.objects.create(serial_number="SN-2", material_type=m)
    InventoryUnit.objects.create(
        serial_number="SN-3", material_type=m, status=InventoryUnit.Status.ISSUED
    )

    r = _client(ops).get("/api/inventory/units/summary/")
    assert r.status_code == 200, r.content
    assert r.data["units"] == 3
    assert r.data["in_stock"] == 2
    assert r.data["issued"] == 1
    assert r.data["under_warranty"] == 1
    assert r.data["no_warranty"] == 2


@pytest.mark.django_db
def test_generic_items_endpoint_unaffected_by_units(ops, items, unit_refs):
    InventoryUnit.objects.create(serial_number="SN-X", material_type=unit_refs["material"])
    r = _client(ops).get("/api/inventory/items/", {"page_size": 100})
    assert r.status_code == 200, r.content
    assert r.data["count"] == 3


# ---------------------------------------------------------------------------
# Bulk registration of unique items (quantity at data-entry time)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_bulk_registers_n_units_with_suffixed_serials(ops, unit_refs):
    payload = _unit_payload(unit_refs, serial_number="SN-BULK", quantity=4)
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 201, r.content
    assert r.data["created"] == 4
    serials = sorted(u["serial_number"] for u in r.data["units"])
    assert serials == ["SN-BULK-1", "SN-BULK-2", "SN-BULK-3", "SN-BULK-4"]
    # Every unit gets its own generated code.
    codes = {u["unit_code"] for u in r.data["units"]}
    assert len(codes) == 4


@pytest.mark.django_db
def test_bulk_quantity_one_keeps_the_plain_serial(ops, unit_refs):
    payload = _unit_payload(unit_refs, serial_number="SN-SOLO", quantity=1)
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 201, r.content
    assert r.data["units"][0]["serial_number"] == "SN-SOLO"


@pytest.mark.django_db
def test_bulk_accepts_an_explicit_serial_list(ops, unit_refs):
    payload = _unit_payload(unit_refs, quantity=3, serial_numbers=["AA-1", "BB-2", "CC-3"])
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 201, r.content
    assert sorted(u["serial_number"] for u in r.data["units"]) == ["AA-1", "BB-2", "CC-3"]


@pytest.mark.django_db
def test_bulk_rejects_serial_list_length_mismatch(ops, unit_refs):
    payload = _unit_payload(unit_refs, quantity=3, serial_numbers=["AA-1", "BB-2"])
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 400, r.content
    assert "serial_numbers" in r.data


@pytest.mark.django_db
def test_bulk_rejects_clashing_serials(ops, unit_refs):
    InventoryUnit.objects.create(serial_number="SN-BULK-1", material_type=unit_refs["material"])
    payload = _unit_payload(unit_refs, serial_number="SN-BULK", quantity=2)
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 400, r.content
    assert "SN-BULK-1" in str(r.data["serial_numbers"])


@pytest.mark.django_db
def test_bulk_applies_warranty_to_every_unit(ops, unit_refs):
    payload = _unit_payload(
        unit_refs, serial_number="SN-WAR", quantity=3,
        has_warranty=True, warranty_type="manufacturer",
        warranty_start="2026-01-10", warranty_months=12,
    )
    r = _client(ops).post("/api/inventory/units/bulk/", payload, format="json")
    assert r.status_code == 201, r.content
    assert all(u["warranty_end"] == "2027-01-10" for u in r.data["units"])
    assert all(u["warranty_state"] == "active" for u in r.data["units"])


# ---------------------------------------------------------------------------
# Goods-receipt inspection gate (procurement -> inspection -> inventory)
# ---------------------------------------------------------------------------
@pytest.fixture
def received_line(db):
    """A delivered PO line sitting in the inspection queue."""
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    supplier = Supplier.objects.create(name="Insp Supplier")
    material = MaterialType.objects.create(name="Insp Cable", unit="meter")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    po_item = PurchaseOrderItem.objects.create(
        purchase_order=po, material_type=material, description="Cable drum",
        quantity=10, unit_price=25,
    )
    receipt = GoodsReceipt.objects.create(purchase_order=po, reference="DN-1")
    line = GoodsReceiptLine.objects.create(
        receipt=receipt, po_item=po_item, quantity=10, batch_number="BATCH-9",
    )
    return {"line": line, "po": po, "material": material, "supplier": supplier}


@pytest.fixture
def inspector(db):
    """Deliveries are checked by a supervisor (or the store); technicians read."""
    return User.objects.create_user(username="insp-supervisor", password="x", role="supervisor")


@pytest.mark.django_db
def test_received_line_starts_pending_and_stocks_nothing(ops, received_line):
    from apps.inventory.models import InventoryItem

    line = received_line["line"]
    assert line.inspection_status == "pending"
    assert line.is_pending_inspection
    assert not InventoryItem.objects.filter(material_type=received_line["material"]).exists()

    r = _client(ops).get("/api/inventory/receipt-lines/pending/")
    assert r.status_code == 200, r.content
    ids = [row["id"] for row in (r.data.get("results", r.data))]
    assert str(line.id) in ids


@pytest.mark.django_db
def test_supervisor_inspects_into_generic_stock_with_batch(inspector, received_line):
    from apps.inventory.models import InventoryItem, StockMovement

    line = received_line["line"]
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {"route": "generic", "accepted_quantity": 10, "notes": "All good"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["line"]["inspection_status"] == "passed"
    assert r.data["line"]["routed_to"] == "generic"

    item = InventoryItem.objects.get(material_type=received_line["material"])
    assert item.quantity == 10
    movement = StockMovement.objects.get(item=item, movement_type="in")
    # Batch + receipt link are what trace the stock back to the PO.
    assert movement.batch_number == "BATCH-9"
    assert movement.goods_receipt_line_id == line.id

    line.refresh_from_db()
    assert line.inspected_by == inspector
    assert line.inspected_at is not None
    assert line.inspection_notes == "All good"


@pytest.mark.django_db
def test_supervisor_inspects_into_unique_units_carrying_batch(inspector, received_line):
    line = received_line["line"]
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {
            "route": "unique", "accepted_quantity": 2, "rejected_quantity": 8,
            "units": [
                {"serial_number": "INS-1", "model_name": "Drum-A"},
                {"serial_number": "INS-2", "model_name": "Drum-B", "has_warranty": True,
                 "warranty_type": "supplier", "warranty_start": "2026-01-01", "warranty_months": 12},
            ],
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    units = r.data["stocked_units"]
    assert len(units) == 2
    assert all(u["batch_number"] == "BATCH-9" for u in units)
    assert all(u["po_number"] == received_line["po"].po_number for u in units)
    assert all(u["status"] == "in_stock" for u in units)
    # Supplier and price flow through from the purchase order.
    assert all(u["supplier_name"] == "Insp Supplier" for u in units)

    warranted = next(u for u in units if u["serial_number"] == "INS-2")
    assert warranted["warranty_end"] == "2027-01-01"
    assert warranted["warranty_state"] == "active"

    line.refresh_from_db()
    assert line.accepted_quantity == 2 and line.rejected_quantity == 8


@pytest.mark.django_db
def test_accepted_plus_rejected_must_equal_received(inspector, received_line):
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{received_line['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 3, "rejected_quantity": 3},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "accepted_quantity" in r.data


@pytest.mark.django_db
def test_full_rejection_stocks_nothing(inspector, received_line):
    from apps.inventory.models import InventoryItem

    line = received_line["line"]
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {"accepted_quantity": 0, "rejected_quantity": 10, "notes": "Damaged in transit"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["line"]["inspection_status"] == "rejected"
    assert not InventoryItem.objects.filter(material_type=received_line["material"]).exists()


@pytest.mark.django_db
def test_unique_route_needs_one_entry_per_accepted_unit(inspector, received_line):
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{received_line['line'].id}/inspect/",
        {"route": "unique", "accepted_quantity": 3, "rejected_quantity": 7,
         "units": [{"serial_number": "ONLY-1"}]},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "units" in r.data


@pytest.mark.django_db
def test_a_line_cannot_be_inspected_twice(inspector, received_line):
    line = received_line["line"]
    c = _client(inspector)
    first = c.post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {"route": "generic", "accepted_quantity": 10}, format="json",
    )
    assert first.status_code == 200, first.content
    second = c.post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {"route": "generic", "accepted_quantity": 10}, format="json",
    )
    assert second.status_code == 400, second.content


@pytest.mark.django_db
def test_accepted_quantity_requires_a_route(inspector, received_line):
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{received_line['line'].id}/inspect/",
        {"accepted_quantity": 10}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "route" in r.data


@pytest.mark.django_db
def test_client_viewer_cannot_inspect(db, received_line):
    viewer = User.objects.create_user(username="insp-viewer", password="x", role="client_viewer")
    r = _client(viewer).post(
        f"/api/inventory/receipt-lines/{received_line['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 10}, format="json",
    )
    assert r.status_code == 403, r.content


# ---------------------------------------------------------------------------
# Opening a unique product in inventory (details now, serials later)
# ---------------------------------------------------------------------------
@pytest.fixture
def product_refs(db):
    from apps.assets.models import Brand

    return {
        "material": MaterialType.objects.create(name="Prod Media Player", unit="piece"),
        "brand": Brand.objects.create(name="Prod Brand"),
    }


@pytest.mark.django_db
def test_open_a_unique_product_at_zero_stock(ops, product_refs):
    r = _client(ops).post("/api/inventory/products/", {
        "name": "55in Media Player",
        "material_type": str(product_refs["material"].id),
        "brand": str(product_refs["brand"].id),
        "model_name": "MP-900",
        "specifications": {"ports": "HDMI x2", "power": "45W"},
        "unit_cost": "4500.00",
        "default_has_warranty": True,
        "default_warranty_type": "manufacturer",
        "default_warranty_months": 24,
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["type_code"].startswith("IVT-")
    # Opened with nothing on hand — stock arrives later.
    assert r.data["in_stock_count"] == 0
    assert r.data["specifications"]["ports"] == "HDMI x2"


@pytest.mark.django_db
def test_warranty_default_needs_a_term(ops, product_refs):
    r = _client(ops).post("/api/inventory/products/", {
        "name": "No term", "default_has_warranty": True,
    }, format="json")
    assert r.status_code == 400, r.content
    assert "default_warranty_months" in r.data


@pytest.mark.django_db
def test_a_unit_inherits_everything_from_its_product(ops, product_refs):
    from apps.inventory.models import InventoryUnit, InventoryUnitType

    product = InventoryUnitType.objects.create(
        name="Inheriting Player",
        material_type=product_refs["material"],
        brand=product_refs["brand"],
        model_name="MP-900",
        unit_cost=4500,
        default_has_warranty=True,
        default_warranty_type="manufacturer",
        default_warranty_months=24,
    )
    # Only the serial is supplied — everything else comes from the product.
    unit = InventoryUnit.objects.create(serial_number="INHERIT-1", unit_type=product)

    assert unit.material_type == product_refs["material"]
    assert unit.brand == product_refs["brand"]
    assert unit.model_name == "MP-900"
    assert unit.purchase_price == 4500
    assert unit.has_warranty is True
    assert unit.warranty_months == 24
    assert unit.warranty_end is not None, "term derived from the inherited start"


@pytest.mark.django_db
def test_in_stock_count_tracks_registered_units(ops, product_refs):
    from apps.inventory.models import InventoryUnit, InventoryUnitType

    product = InventoryUnitType.objects.create(name="Counted Product")
    InventoryUnit.objects.create(serial_number="CNT-1", unit_type=product)
    InventoryUnit.objects.create(serial_number="CNT-2", unit_type=product)
    InventoryUnit.objects.create(
        serial_number="CNT-3", unit_type=product, status=InventoryUnit.Status.ISSUED
    )

    r = _client(ops).get(f"/api/inventory/products/{product.id}/")
    assert r.status_code == 200, r.content
    assert r.data["in_stock_count"] == 2, "issued units are not on hand"

    units = _client(ops).get(f"/api/inventory/products/{product.id}/units/")
    assert units.status_code == 200, units.content
    assert len(units.data.get("results", units.data)) == 3


@pytest.mark.django_db
def test_inspection_only_needs_serials_for_an_opened_product(inspector, received_line, product_refs):
    from apps.inventory.models import InventoryUnitType

    product = InventoryUnitType.objects.create(
        name="Opened Player", brand=product_refs["brand"], model_name="MP-900",
        default_has_warranty=True, default_warranty_type="supplier", default_warranty_months=12,
    )
    line = received_line["line"]
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {
            "route": "unique", "accepted_quantity": 2, "rejected_quantity": 8,
            # Serial only — the rest is inherited from the opened product.
            "units": [
                {"serial_number": "OPEN-1", "unit_type": str(product.id)},
                {"serial_number": "OPEN-2", "unit_type": str(product.id)},
            ],
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    units = r.data["stocked_units"]
    assert len(units) == 2
    assert all(u["model_name"] == "MP-900" for u in units)
    assert all(u["warranty_state"] == "active" for u in units)
    assert all(u["unit_type_code"] == product.type_code for u in units)


# ---------------------------------------------------------------------------
# One stock record per material, and goods land on the row that was ordered
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_cannot_open_a_second_stock_row_for_the_same_material(ops, db):
    from apps.inventory.models import InventoryItem

    material = MaterialType.objects.create(name="Dup Cable")
    first = InventoryItem.objects.create(material_type=material, quantity=100)

    r = _client(ops).post(
        "/api/inventory/items/", {"material_type": str(material.id), "quantity": 0}, format="json",
    )
    assert r.status_code == 400, r.content
    assert first.sku in str(r.data["material_type"])
    assert "already in inventory" in str(r.data["material_type"])


@pytest.mark.django_db
def test_editing_the_existing_stock_row_is_still_allowed(ops, db):
    from apps.inventory.models import InventoryItem

    material = MaterialType.objects.create(name="Editable Cable")
    item = InventoryItem.objects.create(material_type=material, quantity=5)
    r = _client(ops).patch(
        f"/api/inventory/items/{item.id}/",
        {"material_type": str(material.id), "min_stock_level": 9}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["min_stock_level"] == 9


@pytest.mark.django_db
def test_receipt_stocks_the_row_the_po_line_named(inspector, db):
    """Goods must land on the row the requirement is watching."""
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, InventoryItem
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    material = MaterialType.objects.create(name="Targeted Cable")
    # Two rows exist historically; the PO names the second one.
    older = InventoryItem.objects.create(material_type=material, quantity=100)
    target = InventoryItem.objects.create(material_type=material, quantity=0)

    supplier = Supplier.objects.create(name="Targeted Supplier")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    po_item = PurchaseOrderItem.objects.create(
        purchase_order=po, material_type=material, inventory_item=target,
        description="Targeted Cable", quantity=7, unit_price=10,
    )
    receipt = GoodsReceipt.objects.create(purchase_order=po)
    line = GoodsReceiptLine.objects.create(receipt=receipt, po_item=po_item, quantity=7)

    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{line.id}/inspect/",
        {"route": "generic", "accepted_quantity": 7}, format="json",
    )
    assert r.status_code == 200, r.content

    target.refresh_from_db(); older.refresh_from_db()
    assert target.quantity == 7, "stock must land on the row the PO line named"
    assert older.quantity == 100, "the other row for the same material is untouched"


# ---------------------------------------------------------------------------
# Goods bought for a requirement land on that requirement automatically
# ---------------------------------------------------------------------------
@pytest.fixture
def ordered_requirement(db):
    """A requirement with a PO raised against it, delivered and awaiting inspection."""
    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, InventoryItem, IssuanceRequest
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier
    from apps.teams.models import Project

    material = MaterialType.objects.create(name="Auto Cable")
    item = InventoryItem.objects.create(material_type=material, quantity=0)
    project = Project.objects.create(name="Auto Project")
    brand = Brand.objects.create(name="Auto Brand")
    model = DeviceModel.objects.create(brand=brand, name="AU-1")
    device = Device.objects.create(
        device_model=model, serial_number="AUTO-SN-1", project=project,
    )
    component = AssetComponent.objects.create(
        device=device, name="Auto Cable", quantity=7, inventory_item=item,
        fulfilment=AssetComponent.Fulfilment.PROCUREMENT,
    )
    supplier = Supplier.objects.create(name="Auto Supplier")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    po_item = PurchaseOrderItem.objects.create(
        purchase_order=po, material_type=material, inventory_item=item,
        description="Auto Cable", quantity=7, unit_price=10,
    )
    component.purchase_order_item = po_item
    component.save(update_fields=["purchase_order_item"])
    # The Procure decision queued the issue, waiting on the delivery.
    request_row = IssuanceRequest.objects.create(
        item=item, quantity_requested=7, source="project", asset_component=component,
        project=project, awaiting_procurement=True, purpose="Auto Cable — procurement in progress",
    )

    receipt = GoodsReceipt.objects.create(purchase_order=po)
    line = GoodsReceiptLine.objects.create(receipt=receipt, po_item=po_item, quantity=7)
    return {"line": line, "component": component, "item": item, "device": device, "request": request_row}


@pytest.mark.django_db
def test_bought_goods_are_stocked_then_issued_against_the_request(inspector, ops, ordered_requirement):
    """A Procure decision opens a material request as well as the To Procure
    line. The delivery goes into stock; the store issues it against that
    request, and only that issue covers the line on the project."""
    component, item, req = ordered_requirement["component"], ordered_requirement["item"], ordered_requirement["request"]

    # Before the delivery the store cannot issue: the request is on order.
    row = _client(ops).get(f"/api/inventory/issuance-requests/{req.id}/").json()
    assert row["awaiting_procurement"] is True and row["procured"] is True and row["po_received_quantity"] == 0
    r = _client(ops).post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 7}, format="json")
    assert r.status_code == 400 and "nothing has been received into stock" in r.data["detail"], r.content

    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{ordered_requirement['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 7}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["ready_requests"] == [req.request_number]

    # In stock, not on the asset: the line is still to be issued.
    item.refresh_from_db()
    component.refresh_from_db()
    assert item.quantity == 7
    assert component.issued_quantity == 0 and component.fulfilment == "procurement"
    detail = _client(ops).get(f"/api/assets/components/{component.id}/").json()
    assert detail["po_stocked_quantity"] == 7 and detail["procure_requests"] == [req.request_number]
    row = _client(ops).get(f"/api/inventory/issuance-requests/{req.id}/").json()
    assert row["awaiting_procurement"] is False and row["po_received_quantity"] == 7 and row["status"] == "pending"
    assert "ready to issue" in row["notes"]

    # The store hands it over against the request — that covers the line.
    r = _client(ops).post(
        f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 7, "received_by": "Site team"}, format="json",
    )
    assert r.status_code == 200, r.content
    component.refresh_from_db()
    item.refresh_from_db()
    req.refresh_from_db()
    assert component.issued_quantity == 7 and component.fulfilment == "fulfilled"
    assert item.quantity == 0
    assert req.status == "fulfilled" and req.received_by == "Site team"


@pytest.mark.django_db
def test_the_asset_starts_building_once_its_parts_are_issued(inspector, ops, ordered_requirement):
    from apps.assets.models import Device

    device, req = ordered_requirement["device"], ordered_requirement["request"]
    assert device.status == Device.Status.PROCURED

    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{ordered_requirement['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 7}, format="json",
    )
    assert r.status_code == 200, r.content
    device.refresh_from_db()
    assert device.status == Device.Status.PROCURED, "in stock is not yet in hand"

    assert _client(ops).post(
        f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 7}, format="json",
    ).status_code == 200
    device.refresh_from_db()
    assert device.status == Device.Status.IN_PRODUCTION


@pytest.mark.django_db
def test_a_short_delivery_only_lets_the_store_issue_what_arrived(inspector, ops, ordered_requirement):
    component, req = ordered_requirement["component"], ordered_requirement["request"]
    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{ordered_requirement['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 4, "rejected_quantity": 3}, format="json",
    )
    assert r.status_code == 200, r.content

    r = _client(ops).post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 7}, format="json")
    assert r.status_code == 400, r.content
    r = _client(ops).post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 4}, format="json")
    assert r.status_code == 200, r.content

    component.refresh_from_db()
    req.refresh_from_db()
    assert component.issued_quantity == 4
    assert component.outstanding_quantity == 3
    assert component.fulfilment == "procurement", "the rest is still to be procured"
    assert req.status == "partial" and req.outstanding_quantity == 3


@pytest.mark.django_db
def test_stock_bought_without_a_requirement_stays_in_the_warehouse(inspector, received_line):
    """Ordinary replenishment is not claimed by anything."""
    from apps.inventory.models import InventoryItem

    r = _client(inspector).post(
        f"/api/inventory/receipt-lines/{received_line['line'].id}/inspect/",
        {"route": "generic", "accepted_quantity": 10}, format="json",
    )
    assert r.status_code == 200, r.content
    item = InventoryItem.objects.get(material_type=received_line["material"])
    assert item.quantity == 10


@pytest.mark.django_db
def test_unique_product_can_be_opened_with_stock_already_on_the_shelf():
    """Opening stock: a product that already has units when it is first set up."""
    from apps.accounts.models import User
    from rest_framework.test import APIClient

    from apps.inventory.models import InventoryUnitType

    user = User.objects.create_user(username="open-wh", password="x", role="warehouse")
    c = APIClient()
    c.force_authenticate(user)

    # Stock on the shelf means a serial for every unit — none typed, no item.
    r = c.post("/api/inventory/products/", {
        "name": "Opening Media Player", "opening_quantity": 3,
    }, format="json")
    assert r.status_code == 400 and "each of the 3" in str(r.data["opening_serials"])
    r = c.post("/api/inventory/products/", {
        "name": "Opening Media Player", "opening_quantity": 3, "opening_serials": ["OMP-1", "OMP-2"],
    }, format="json")
    assert r.status_code == 400 and "opening_serials" in r.data
    r = c.post("/api/inventory/products/", {
        "name": "Opening Media Player", "opening_quantity": 3, "opening_serials": ["OMP-1", "omp-1", "OMP-3"],
    }, format="json")
    assert r.status_code == 400 and "repeated" in str(r.data["opening_serials"])

    r = c.post("/api/inventory/products/", {
        "name": "Opening Media Player", "opening_quantity": 3, "opening_serials": [" OMP-1 ", "OMP-2", "OMP-3"],
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["in_stock_count"] == 3

    unit_type = InventoryUnitType.objects.get(pk=r.data["id"])
    units = list(unit_type.units.order_by("serial_number"))
    assert [u.serial_number for u in units] == ["OMP-1", "OMP-2", "OMP-3"]
    assert len({u.unit_code for u in units}) == 3
    assert all(u.unit_code for u in units)

    # A serial already in inventory cannot be opened a second time.
    r = c.post("/api/inventory/products/", {
        "name": "Second Player", "opening_quantity": 1, "opening_serials": ["OMP-2"],
    }, format="json")
    assert r.status_code == 400 and "OMP-2" in str(r.data["opening_serials"])

    # Opening at zero is the normal case and creates nothing.
    r = c.post("/api/inventory/products/", {"name": "Empty Product"}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["in_stock_count"] == 0


@pytest.mark.django_db
def test_opening_stock_units_can_be_corrected_on_a_bare_unit():
    """A serial typed at opening can still be corrected later.

    A product opened with nothing but a name raises units carrying neither a
    material type nor a model name, so the identity rule is asked at
    registration only — otherwise those units could never be touched again.
    """
    from apps.inventory.models import InventoryUnitType

    user = User.objects.create_user(username="serial-wh", password="x", role="warehouse")
    c = APIClient()
    c.force_authenticate(user)

    r = c.post("/api/inventory/products/", {
        "name": "Unidentified Player", "opening_quantity": 2, "opening_serials": ["UP-A", "UP-B"],
    }, format="json")
    assert r.status_code == 201, r.content
    unit_type = InventoryUnitType.objects.get(pk=r.data["id"])
    unit = unit_type.units.order_by("serial_number").first()
    assert unit.material_type is None and not unit.model_name

    r = c.patch(f"/api/inventory/units/{unit.id}/", {"serial_number": "REAL-SN-0001"}, format="json")
    assert r.status_code == 200, r.content
    unit.refresh_from_db()
    assert unit.serial_number == "REAL-SN-0001"

    # Registering a new unit still has to say what the thing is.
    r = c.post("/api/inventory/units/", {"serial_number": "NO-IDENTITY-1"}, format="json")
    assert r.status_code == 400, r.content
    assert "material_type" in r.data


# ---------------------------------------------------------------------------
# The store's queue: everything leaves the warehouse from one desk
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_request_can_be_issued_in_part_and_the_balance_stays_owed(ops, items):
    from apps.inventory.models import IssuanceRequest

    item = items[0]
    item.quantity = 10
    item.save(update_fields=["quantity"])

    asker = User.objects.create_user(username="req-tech", password="x", role="technician")
    tech_client = APIClient()
    tech_client.force_authenticate(asker)

    # Anyone running work may ask the store for material.
    r = tech_client.post("/api/inventory/issuance-requests/", {
        "item": str(item.id), "quantity_requested": 6,
        "source": "maintenance", "purpose": "Screen repair at site",
    }, format="json")
    assert r.status_code == 201, r.content
    request_id = r.data["id"]
    assert r.data["status"] == "pending"
    assert r.data["requested_by_name"]
    assert r.data["outstanding_quantity"] == 6

    # ...but only the warehouse hands it over.
    denied = tech_client.post(f"/api/inventory/issuance-requests/{request_id}/issue/",
                              {"quantity": 6}, format="json")
    assert denied.status_code == 403
    item.refresh_from_db()
    assert item.quantity == 10, "a refused issue must not move stock"

    store = _client(ops)
    r = store.post(f"/api/inventory/issuance-requests/{request_id}/issue/",
                   {"quantity": 4, "received_by": "Bilal (site inspector)"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["issued"] == 4
    assert r.data["request"]["status"] == "partial"
    assert r.data["request"]["outstanding_quantity"] == 2, "the balance stays on the queue"

    item.refresh_from_db()
    assert item.quantity == 6

    # The rest, once it can be covered.
    r = store.post(f"/api/inventory/issuance-requests/{request_id}/issue/",
                   {"quantity": 2}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["request"]["status"] == "fulfilled"
    assert r.data["request"]["outstanding_quantity"] == 0

    # Nothing more can be drawn against a request already met in full.
    over = store.post(f"/api/inventory/issuance-requests/{request_id}/issue/",
                      {"quantity": 1}, format="json")
    assert over.status_code == 400
    assert "outstanding" in str(over.data)

    row = IssuanceRequest.objects.get(pk=request_id)
    assert row.received_by == "Bilal (site inspector)"


@pytest.mark.django_db
def test_issue_slip_prints_what_went_out(ops, items):
    item = items[0]
    item.quantity = 5
    item.save(update_fields=["quantity"])

    store = _client(ops)
    r = store.post("/api/inventory/issuance-requests/", {
        "item": str(item.id), "quantity_requested": 2,
        "source": "other", "purpose": "Workshop spares",
    }, format="json")
    assert r.status_code == 201, r.content
    request_id = r.data["id"]
    store.post(f"/api/inventory/issuance-requests/{request_id}/issue/",
               {"quantity": 2, "received_by": "Workshop"}, format="json")

    r = store.get(f"/api/inventory/issuance-requests/{request_id}/slip/")
    assert r.status_code == 200, r.content
    assert r["Content-Type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


@pytest.mark.django_db
def test_a_request_names_one_thing_to_issue(ops, items):
    r = _client(ops).post("/api/inventory/issuance-requests/", {
        "quantity_requested": 1, "source": "other",
    }, format="json")
    assert r.status_code == 400
    assert "item" in r.data


@pytest.mark.django_db
def test_a_new_component_opens_its_ledger_with_the_opening_stock(ops):
    """'Add Component' asks for opening stock and a rate: the stock typed there
    is journalled as the component's opening movement, priced at that rate."""
    from apps.inventory.models import StockMovement

    rope = MaterialType.objects.create(name="Opening Rope", unit="meter")
    r = _client(ops).post("/api/inventory/items/", {
        "material_type": str(rope.id), "quantity": 40, "min_stock_level": 5, "unit_cost": "12.50",
    }, format="json")
    assert r.status_code == 201, r.content
    moves = list(StockMovement.objects.filter(item_id=r.data["id"]))
    assert len(moves) == 1
    assert moves[0].movement_type == "opening" and moves[0].quantity == 40
    assert moves[0].reference == "Opening stock" and "12.50 per meter" in moves[0].notes
    assert moves[0].performed_by == ops
    listed = _client(ops).get("/api/inventory/movements/", {"item": r.data["id"]}).json()
    rows = listed.get("results", listed)
    assert [m["movement_type"] for m in rows] == ["opening"]

    # Opened empty: nothing to journal yet.
    tape = MaterialType.objects.create(name="Opening Tape", unit="roll")
    r = _client(ops).post("/api/inventory/items/", {"material_type": str(tape.id), "quantity": 0}, format="json")
    assert r.status_code == 201, r.content
    assert not StockMovement.objects.filter(item_id=r.data["id"]).exists()


@pytest.mark.django_db
def test_inspection_knows_the_kind_from_the_order(ops):
    """The component was opened in inventory before it was ordered, so the
    delivered line says whether it is generic or unique — and cannot be filed
    the other way."""
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, InventoryUnitType
    from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
    from apps.suppliers.models import Supplier

    c = _client(ops)
    supplier = Supplier.objects.create(name="Kind Supplier")
    cable_mt = MaterialType.objects.create(name="Kind Cable", unit="meter")
    cable = InventoryItem.objects.create(material_type=cable_mt, quantity=3)
    player = InventoryUnitType.objects.create(name="Kind Player 55in", unit="piece")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED)
    cable_po = PurchaseOrderItem.objects.create(
        purchase_order=po, inventory_item=cable, material_type=cable_mt, description="Cable", quantity=20, unit_price=5,
    )
    player_po = PurchaseOrderItem.objects.create(
        purchase_order=po, inventory_unit_type=player, description="Player", quantity=2, unit_price=900,
    )
    receipt = GoodsReceipt.objects.create(purchase_order=po, reference="DN-K")
    cable_line = GoodsReceiptLine.objects.create(receipt=receipt, po_item=cable_po, quantity=20)
    player_line = GoodsReceiptLine.objects.create(receipt=receipt, po_item=player_po, quantity=2)

    listed = {r["id"]: r for r in c.get("/api/inventory/receipt-lines/", {"page_size": 100}).json()["results"]}
    assert listed[str(cable_line.id)]["kind"] == "generic" and listed[str(cable_line.id)]["known_component"].startswith("Kind Cable")
    assert listed[str(player_line.id)]["kind"] == "unique" and listed[str(player_line.id)]["known_component"].startswith("Kind Player 55in")

    # Filing a unique product as generic stock is refused, and vice versa.
    r = c.post(f"/api/inventory/receipt-lines/{player_line.id}/inspect/", {
        "accepted_quantity": 2, "rejected_quantity": 0, "route": "generic", "generic": {},
    }, format="json")
    assert r.status_code == 400 and "Kind Player 55in" in str(r.data["route"]), r.content
    r = c.post(f"/api/inventory/receipt-lines/{cable_line.id}/inspect/", {
        "accepted_quantity": 20, "rejected_quantity": 0, "route": "unique",
        "units": [{"serial_number": f"K-{n}"} for n in range(20)],
    }, format="json")
    assert r.status_code == 400 and "Kind Cable" in str(r.data["route"]), r.content

    # Filed the way the order says, without naming the component again.
    r = c.post(f"/api/inventory/receipt-lines/{cable_line.id}/inspect/", {
        "accepted_quantity": 20, "rejected_quantity": 0, "route": "generic", "generic": {"storage_location": "Rack K"},
    }, format="json")
    assert r.status_code == 200, r.content
    cable.refresh_from_db()
    assert cable.quantity == 23 and cable.storage_location == "Rack K"
    r = c.post(f"/api/inventory/receipt-lines/{player_line.id}/inspect/", {
        "accepted_quantity": 2, "rejected_quantity": 0, "route": "unique",
        "units": [{"serial_number": "KP-1"}, {"serial_number": "KP-2"}],
    }, format="json")
    assert r.status_code == 200, r.content
    assert player.units.count() == 2 and set(player.units.values_list("serial_number", flat=True)) == {"KP-1", "KP-2"}


@pytest.mark.django_db
def test_requests_follow_the_requirement_they_cover(ops):
    """Stock issued straight to a requirement settles the request raised for
    it; a request whose requirement is already covered closes instead of
    failing."""
    from django.db import transaction

    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel
    from apps.inventory.models import IssuanceRequest
    from apps.inventory.services import issue_stock_for_component

    brand = Brand.objects.create(name="ReqBrand")
    dm = DeviceModel.objects.create(brand=brand, name="R-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-REQ-1", serial_number="REQ-1")
    stand_mt = MaterialType.objects.create(name="Req Stand", unit="piece")
    stand = InventoryItem.objects.create(material_type=stand_mt, quantity=13)

    # 1. Issued straight to the requirement (a direct issue): the request settles.
    comp = AssetComponent.objects.create(device=device, name="Req Stand", quantity=1, inventory_item=stand)
    req = IssuanceRequest.objects.create(item=stand, quantity_requested=1, asset_component=comp, source="project")
    with transaction.atomic():
        issue_stock_for_component(comp, ops, 1)
    req.refresh_from_db()
    assert req.quantity_issued == 1 and req.status == "fulfilled" and "straight to the requirement" in req.notes

    # 2. A request left open on a requirement that is already covered: the
    #    store's attempt closes it with a reason, rather than an error.
    device2 = Device.objects.create(device_model=dm, asset_code="AST-REQ-2", serial_number="REQ-2")
    comp2 = AssetComponent.objects.create(device=device2, name="Req Stand 2", quantity=1, inventory_item=stand, issued_quantity=1)
    req2 = IssuanceRequest.objects.create(item=stand, quantity_requested=1, asset_component=comp2, source="project")
    r = _client(ops).post(f"/api/inventory/issuance-requests/{req2.id}/issue/", {"quantity": 1, "received_by": "Ali"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["issued"] == 0 and r.data["closed"] is True and "already fully covered" in r.data["reason"]
    req2.refresh_from_db()
    assert req2.status == "cancelled"
    stand.refresh_from_db()
    assert stand.quantity == 12   # only the first issue moved stock

    # 3. Asking for more than the requirement still needs is refused plainly.
    device3 = Device.objects.create(device_model=dm, asset_code="AST-REQ-3", serial_number="REQ-3")
    comp3 = AssetComponent.objects.create(device=device3, name="Req Stand 3", quantity=2, inventory_item=stand)
    req3 = IssuanceRequest.objects.create(item=stand, quantity_requested=2, asset_component=comp3, source="project")
    comp3.issued_quantity = 1
    comp3.save(update_fields=["issued_quantity"])
    r = _client(ops).post(f"/api/inventory/issuance-requests/{req3.id}/issue/", {"quantity": 2}, format="json")
    assert r.status_code == 400 and "Only 1" in str(r.data["quantity"]), r.content
    r = _client(ops).post(f"/api/inventory/issuance-requests/{req3.id}/issue/", {"quantity": 1, "received_by": "Site team"}, format="json")
    assert r.status_code == 200, r.content
    req3.refresh_from_db()
    assert req3.quantity_issued == 1 and req3.received_by == "Site team"
    comp3.refresh_from_db()
    assert comp3.issued_quantity == 2


@pytest.mark.django_db
def test_the_log_reads_a_request_by_its_own_number(ops):
    """A hand-over keeps the request's MR number, records when it happened,
    and lists every serial that went out with where each unit stands."""
    from apps.inventory.models import InventoryUnit, InventoryUnitType, IssuanceRequest

    player = InventoryUnitType.objects.create(name="Log Player", unit="piece")
    for sn in ("LP-1", "LP-2", "LP-3"):
        InventoryUnit.objects.create(unit_type=player, serial_number=sn, model_name="LP")
    req = IssuanceRequest.objects.create(unit_type=player, quantity_requested=3, source="project", purpose="Log test")
    c = _client(ops)

    r = c.post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 2, "received_by": "Site team"}, format="json")
    assert r.status_code == 200, r.content
    body = r.data["request"]
    assert body["request_number"] == req.request_number and body["request_number"].startswith("MR-")
    assert body["last_issued_at"] is not None and body["quantity_issued"] == 2
    assert [u["serial_number"] for u in body["issued_units"]] == ["LP-1", "LP-2"]
    assert all(u["status"] == "issued" and u["unit_code"] for u in body["issued_units"])

    # The balance goes out later under the same number; the log shows all three.
    r = c.post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 1}, format="json")
    assert r.status_code == 200, r.content
    assert [u["serial_number"] for u in r.data["request"]["issued_units"]] == ["LP-1", "LP-2", "LP-3"]
    assert r.data["request"]["status"] == "fulfilled"


@pytest.mark.django_db
def test_low_stock_is_listed_and_bought_through_a_reorder_request(ops):
    """Stock at or below its reorder level shows under Low Stock; a reorder
    request goes to To Procure, on a purchase order, and closes when the goods
    are received. One open request per component; Procurement can send it back."""
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, InventoryUnitType, ReorderRequest
    from apps.procurement.models import PurchaseOrder
    from apps.suppliers.models import Supplier

    c = _client(ops)
    tape_mt = MaterialType.objects.create(name="Low Tape", unit="roll")
    tape = InventoryItem.objects.create(material_type=tape_mt, quantity=2, min_stock_level=5, unit_cost=80)
    plenty_mt = MaterialType.objects.create(name="Plenty Bolt", unit="piece")
    InventoryItem.objects.create(material_type=plenty_mt, quantity=50, min_stock_level=5)
    player = InventoryUnitType.objects.create(name="Low Player", unit="piece", min_stock_level=2, unit_cost=900)

    low = c.get("/api/inventory/low-stock/").json()
    by_name = {r["name"]: r for r in low["results"]}
    assert "Low Tape" in by_name and "Plenty Bolt" not in by_name and "Low Player" in by_name
    assert by_name["Low Tape"]["on_hand"] == 2 and by_name["Low Tape"]["reorder_level"] == 5 and by_name["Low Tape"]["shortfall"] == 3
    assert by_name["Low Player"]["kind"] == "unique" and by_name["Low Player"]["open_request"] is None

    r = c.post("/api/inventory/reorder-requests/", {"item": str(tape.id), "quantity": 10, "reason": "Below reorder level"}, format="json")
    assert r.status_code == 201, r.content
    rr = ReorderRequest.objects.get(pk=r.data["id"])
    assert rr.status == "open" and rr.requested_by == ops and r.data["name"] == "Low Tape" and r.data["kind"] == "generic"
    r2 = c.post("/api/inventory/reorder-requests/", {"item": str(tape.id), "quantity": 4}, format="json")
    assert r2.status_code == 400 and "already open" in str(r2.data)
    assert c.get("/api/inventory/low-stock/").json()["results"][0]["open_request"]["status"] == "open"

    # To Procure lists it; a purchase order takes it.
    rows = c.get("/api/procurement/purchase-orders/requisitions/").json()["results"]
    line = next(x for x in rows if x.get("kind") == "reorder")
    assert line["reorder"] == str(rr.id) and line["asset_code"] == "Stock" and line["outstanding_quantity"] == 10 and line["unit"] == "roll"
    supplier = Supplier.objects.create(name="Tape Supplier")
    r = c.post("/api/procurement/purchase-orders/raise-po/", {"supplier": str(supplier.id), "reorders": [str(rr.id)], "prices": {str(rr.id): "75"}}, format="json")
    assert r.status_code == 201, r.content
    po = PurchaseOrder.objects.get(pk=r.data["id"])
    po_item = po.items.get()
    assert po_item.inventory_item == tape and po_item.quantity == 10 and po_item.unit_price == 75 and "stock replenishment" in po_item.description
    rr.refresh_from_db()
    assert rr.status == "ordered" and rr.purchase_order_item == po_item
    assert c.post("/api/procurement/purchase-orders/requisitions/send-back/", {"reorder": str(rr.id), "reason": "no"}, format="json").status_code == 400

    # Received into stock: the request is done and the item is above its level.
    po.status = PurchaseOrder.Status.ORDERED
    po.save(update_fields=["status"])
    receipt = GoodsReceipt.objects.create(purchase_order=po, reference="DN-LOW")
    gl = GoodsReceiptLine.objects.create(receipt=receipt, po_item=po_item, quantity=10)
    r = c.post(f"/api/inventory/receipt-lines/{gl.id}/inspect/", {"accepted_quantity": 10, "rejected_quantity": 0, "route": "generic", "generic": {}}, format="json")
    assert r.status_code == 200, r.content
    rr.refresh_from_db()
    tape.refresh_from_db()
    assert rr.status == "received" and tape.quantity == 12
    assert "Low Tape" not in {x["name"] for x in c.get("/api/inventory/low-stock/").json()["results"]}

    # A request Procurement sends back is withdrawn with the reason; Inventory can raise it again.
    r = c.post("/api/inventory/reorder-requests/", {"unit_type": str(player.id), "quantity": 3}, format="json")
    assert r.status_code == 201, r.content
    r = c.post("/api/procurement/purchase-orders/requisitions/send-back/", {"reorder": r.data["id"], "reason": "Model discontinued"}, format="json")
    assert r.status_code == 200, r.content
    back = ReorderRequest.objects.get(unit_type=player)
    assert back.status == "cancelled" and "Model discontinued" in back.notes
    assert c.post("/api/inventory/reorder-requests/", {"unit_type": str(player.id), "quantity": 3}, format="json").status_code == 201


@pytest.mark.django_db
def test_the_slip_and_the_export_carry_every_hand_over(ops):
    """Two hand-overs to two people are both on record; the slip says who the
    material was issued to with code, UOM and balance; the Excel export has a
    row per hand-over."""
    import io as _io

    from openpyxl import load_workbook
    from pypdf import PdfReader

    from apps.inventory.models import InventoryUnit, InventoryUnitType, IssuanceRequest

    player = InventoryUnitType.objects.create(name="Slip Player", unit="piece")
    for sn in ("SL-1", "SL-2", "SL-3"):
        InventoryUnit.objects.create(unit_type=player, serial_number=sn, model_name="SL")
    req = IssuanceRequest.objects.create(unit_type=player, quantity_requested=3, source="maintenance", purpose="Screen swap at Mall")
    c = _client(ops)
    assert c.post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 2, "received_by": "Hassan Ali · warehouse"}, format="json").status_code == 200
    r = c.post(f"/api/inventory/issuance-requests/{req.id}/issue/", {"quantity": 1, "received_by": "Site team B"}, format="json")
    assert r.status_code == 200, r.content
    body = r.data["request"]
    assert [(h["quantity"], h["received_by"], h["serials"]) for h in body["handovers"]] == [
        (2, "Hassan Ali · warehouse", ["SL-1", "SL-2"]), (1, "Site team B", ["SL-3"]),
    ]
    assert all(h["issued_by"] == "inv-ops" for h in body["handovers"])

    pdf = c.get(f"/api/inventory/issuance-requests/{req.id}/slip/")
    assert pdf.status_code == 200
    text = "".join(p.extract_text() for p in PdfReader(_io.BytesIO(pdf.content)).pages)
    for needle in ("ISSUED TO", "Hassan Ali", "Site team B", "UOM", "BALANCE", "Hand-overs", "SL-3", "Authorised by", "Maintenance"):
        assert needle in text, needle
    assert "Screen swap at Mall" in text

    x = c.get("/api/inventory/issuance-requests/export/")
    assert x.status_code == 200 and "spreadsheet" in x["Content-Type"]
    ws = load_workbook(_io.BytesIO(x.content)).active
    rows = [row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] == req.request_number]
    assert [(row[6], row[7]) for row in rows] == [(2, "Hassan Ali · warehouse"), (1, "Site team B")]
    assert rows[0][5] == "piece" and rows[0][4] == "Unique item" and rows[0][12] == 0


@pytest.mark.django_db
def test_requests_are_numbered_in_sequence(ops):
    """MR and PR numbers follow the numbering scheme like every other document."""
    import re

    from apps.inventory.models import IssuanceRequest, ReorderRequest

    mt = MaterialType.objects.create(name="Numbered Rope", unit="meter")
    item = InventoryItem.objects.create(material_type=mt, quantity=1, min_stock_level=5)
    a = IssuanceRequest.objects.create(item=item, quantity_requested=1, source="other")
    b = IssuanceRequest.objects.create(item=item, quantity_requested=1, source="other")
    assert re.fullmatch(r"MR-\d{4}-\d{5}", a.request_number), a.request_number
    assert int(b.request_number[-5:]) == int(a.request_number[-5:]) + 1

    r = _client(ops).post("/api/inventory/reorder-requests/", {"item": str(item.id), "quantity": 10}, format="json")
    assert r.status_code == 201, r.content
    assert re.fullmatch(r"PR-\d{4}-\d{5}", r.data["request_number"]), r.data["request_number"]
    low = _client(ops).get("/api/inventory/low-stock/").json()["results"]
    assert next(x for x in low if x["name"] == "Numbered Rope")["open_request"]["request_number"] == r.data["request_number"]
    row = next(x for x in _client(ops).get("/api/procurement/purchase-orders/requisitions/").json()["results"] if x.get("kind") == "reorder" and x["reorder"] == r.data["id"])
    assert row["request_number"] == r.data["request_number"]
    assert str(ReorderRequest.objects.get(pk=r.data["id"])).startswith("PR-")


@pytest.mark.django_db
def test_the_receiving_log_lists_inspected_lines_and_exports(ops, received_line):
    """An inspected line reads as a log entry — where from, what, accepted and
    rejected, filed into, serials — and the Excel export has a row for it."""
    import io as _io

    from openpyxl import load_workbook

    line = received_line["line"]
    c = _client(ops)
    r = c.post(f"/api/inventory/receipt-lines/{line.id}/inspect/", {
        "accepted_quantity": 8, "rejected_quantity": 2, "route": "generic",
        "generic": {"storage_location": "Rack L"}, "notes": "Two drums dented",
    }, format="json")
    assert r.status_code == 200, r.content

    rows = c.get("/api/inventory/receipt-lines/", {"inspection_status": "passed", "page_size": 100}).json()["results"]
    entry = next(x for x in rows if x["id"] == str(line.id))
    assert entry["source_display"] == "Purchase Order" and entry["reference"] == "DN-1" and entry["received_at"]
    assert entry["accepted_quantity"] == 8 and entry["rejected_quantity"] == 2 and entry["routed_to_display"]
    assert entry["stocked_item_sku"] and entry["storage_location"] == "Rack L" and entry["inspection_status_display"].startswith("Passed")

    x = c.get("/api/inventory/receipt-lines/export/")
    assert x.status_code == 200 and "spreadsheet" in x["Content-Type"]
    ws = load_workbook(_io.BytesIO(x.content)).active
    hdr = [cell.value for cell in ws[1]]
    row = next(row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] == line.receipt.grn_number)
    got = dict(zip(hdr, row))
    assert got["Received Qty"] == 10 and got["Accepted"] == 8 and got["Rejected"] == 2 and got["Placed At"] == "Rack L"
    assert got["UOM"] == "meter" and str(got["Result"]).startswith("Passed") and got["Notes"] == "Two drums dented"
