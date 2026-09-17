import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import AssetCode, Brand, Device, DeviceModel


@pytest.fixture
def admin_client(db):
    admin = User.objects.create_user(
        username="labeladmin", password="x", role="super_admin"
    )
    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.fixture
def device(db):
    brand = Brand.objects.create(name="LabelBrand")
    model = DeviceModel.objects.create(brand=brand, name="LB-55")
    return Device.objects.create(device_model=model, serial_number="SN-LABEL-1")


@pytest.mark.django_db
def test_label_generates_png_qr(admin_client, device):
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/label/", {"format": "qr"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["generated_file"]

    label = AssetCode.objects.get(device=device, format="qr", is_current=True)
    with label.generated_file.open("rb") as fh:
        assert fh.read(8).startswith(b"\x89PNG")


@pytest.mark.django_db
def test_label_regeneration_reuses_current_row(admin_client, device):
    for _ in range(2):
        resp = admin_client.post(
            f"/api/assets/devices/{device.id}/label/", {"format": "qr"}, format="json"
        )
        assert resp.status_code == 200
    assert AssetCode.objects.filter(device=device, format="qr").count() == 1


@pytest.mark.django_db
def test_label_code128_and_invalid_format(admin_client, device):
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/label/", {"format": "code128"}, format="json"
    )
    assert resp.status_code == 200
    assert AssetCode.objects.filter(device=device, format="code128").exists()

    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/label/", {"format": "pdf417"}, format="json"
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_label_requires_manager_role(device):
    tech = User.objects.create_user(username="labeltech", password="x", role="technician")
    client = APIClient()
    client.force_authenticate(tech)
    resp = client.post(
        f"/api/assets/devices/{device.id}/label/", {"format": "qr"}, format="json"
    )
    assert resp.status_code == 403


# ── Bulk labels (WF-05): one PDF, one label per page ──────────────────


def _pdf_page_count(content: bytes) -> int:
    # Pillow writes one "/Type /Page" object per page plus one "/Type /Pages" node.
    return content.count(b"/Type /Page") - content.count(b"/Type /Pages")


@pytest.fixture
def device_batch(device):
    dm = device.device_model
    others = [
        Device.objects.create(device_model=dm, serial_number=f"SN-BULK-{i}")
        for i in range(2)
    ]
    return [device, *others]


@pytest.mark.django_db
def test_bulk_labels_returns_multipage_pdf(admin_client, device_batch):
    resp = admin_client.post(
        "/api/assets/devices/labels/",
        {"ids": [str(d.id) for d in device_batch]},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"] == "application/pdf"
    assert "labels-qr-" in resp["Content-Disposition"]
    assert resp.content.startswith(b"%PDF")
    assert _pdf_page_count(resp.content) == 3


@pytest.mark.django_db
def test_bulk_labels_code128_and_invalid_format(admin_client, device_batch):
    ids = [str(d.id) for d in device_batch]
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids, "format": "code128"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    assert resp.content.startswith(b"%PDF")
    assert "labels-code128-" in resp["Content-Disposition"]

    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids, "format": "pdf417"}, format="json"
    )
    assert resp.status_code == 400
    assert "format" in resp.json()


@pytest.mark.django_db
def test_bulk_labels_rejects_empty_and_oversized_batches(admin_client, device):
    import uuid as _uuid

    for bad_body in ({}, {"ids": []}, {"ids": "not-a-list"}):
        resp = admin_client.post("/api/assets/devices/labels/", bad_body, format="json")
        assert resp.status_code == 400, bad_body

    too_many = [str(_uuid.uuid4()) for _ in range(201)]
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": too_many}, format="json"
    )
    assert resp.status_code == 400
    assert "At most 200" in str(resp.json()["ids"])


@pytest.mark.django_db
def test_bulk_labels_rejects_bad_uuids_and_unknown_ids(admin_client, device):
    import uuid as _uuid

    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ["not-a-uuid"]}, format="json"
    )
    assert resp.status_code == 400

    unknown = _uuid.uuid4()
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": [str(unknown)]}, format="json"
    )
    assert resp.status_code == 400
    assert str(unknown) in str(resp.json()["ids"])


@pytest.mark.django_db
def test_bulk_labels_400_lists_missing_ids(admin_client, device_batch):
    """A partial batch fails loudly, naming every unresolved id (deduped)."""
    import uuid as _uuid

    missing_a, missing_b = _uuid.uuid4(), _uuid.uuid4()
    ids = [str(d.id) for d in device_batch] + [
        str(missing_a), str(missing_b), str(missing_a),  # duplicate collapses
    ]
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids}, format="json"
    )
    assert resp.status_code == 400
    message = str(resp.json()["ids"])
    assert str(missing_a) in message and str(missing_b) in message
    assert message.count(str(missing_a)) == 1  # deduped before reporting
    # No devices resolved → no ledger rows written for the good ids either.
    assert AssetCode.objects.count() == 0


@pytest.mark.django_db
def test_bulk_labels_accepts_duplicate_known_ids(admin_client, device_batch):
    """Duplicated known ids collapse to one page each instead of a 400."""
    ids = [str(d.id) for d in device_batch] + [str(device_batch[0].id)]
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids}, format="json"
    )
    assert resp.status_code == 200, resp.content
    assert _pdf_page_count(resp.content) == 3


@pytest.mark.django_db
def test_bulk_labels_persist_asset_code_ledger(admin_client, device_batch):
    """Bulk prints leave the same AssetCode trail as single-label prints."""
    ids = [str(d.id) for d in device_batch]
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids, "format": "qr"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    for d in device_batch:
        label = AssetCode.objects.get(device=d, format="qr", is_current=True)
        assert label.label_size == "60x30"
        with label.generated_file.open("rb") as fh:
            assert fh.read(8).startswith(b"\x89PNG")

    # Re-printing reuses the current rows — no duplicate ledger entries.
    resp = admin_client.post(
        "/api/assets/devices/labels/", {"ids": ids, "format": "qr"}, format="json"
    )
    assert resp.status_code == 200
    assert AssetCode.objects.filter(format="qr").count() == len(device_batch)


@pytest.mark.django_db
def test_bulk_labels_requires_manager_role(device):
    tech = User.objects.create_user(username="bulktech", password="x", role="technician")
    client = APIClient()
    client.force_authenticate(tech)
    resp = client.post(
        "/api/assets/devices/labels/", {"ids": [str(device.id)]}, format="json"
    )
    assert resp.status_code == 403


# ── Asset composition (Project -> Asset -> Components) ────────────────

import pytest as _pytest
from rest_framework.test import APIClient as _APIClient

from apps.accounts.models import User as _User


@_pytest.mark.django_db
def test_components_and_project_link():
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.teams.models import Project

    from apps.assets.models import MaterialType
    from apps.inventory.models import InventoryItem

    ops = _User.objects.create_user(username="cmp-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="CmpBrand")
    dm = DeviceModel.objects.create(brand=brand, name="C-1")
    proj = Project.objects.create(name="Cmp Order")
    dev = Device.objects.create(device_model=dm, asset_code="AST-CMP-1", serial_number="CMP-1", project=proj)

    # Components are drawn from the warehouse, so stock them first.
    cabinet_stock = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Cmp Cabinet"), quantity=20
    )
    player_stock = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Cmp Media Player"), quantity=5
    )

    c = _APIClient()
    c.force_authenticate(ops)

    r = c.post("/api/assets/components/", {
        "device": str(dev.pk), "name": "SMD Cabinet P3.9",
        "component_type": "Cabinet", "quantity": 12,
        "inventory_item": str(cabinet_stock.pk),
    }, format="json")
    assert r.status_code == 201, r.content
    r = c.post("/api/assets/components/", {
        "device": str(dev.pk), "name": "Media Player", "quantity": 1,
        "inventory_item": str(player_stock.pk),
    }, format="json")
    assert r.status_code == 201

    detail = c.get(f"/api/assets/devices/{dev.pk}/").json()
    assert detail["project_name"] == "Cmp Order"
    assert len(detail["components"]) == 2

    projects = c.get("/api/teams/projects/").json()
    rows = projects.get("results", projects)
    row = next(p for p in rows if p["id"] == str(proj.pk))
    assert row["assets_count"] == 1


@pytest.mark.django_db
def test_component_takes_a_supplier_but_no_typed_warranty(db):
    """A part's warranty is recorded when it is received, not typed on the
    component row — those fields are ignored here."""
    from datetime import date

    from apps.suppliers.models import Supplier
    from apps.warranties.models import Warranty

    brand = Brand.objects.create(name="CompBrand")
    dm = DeviceModel.objects.create(brand=brand, name="C-1")
    supplier = Supplier.objects.create(name="Comp Supplier")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-COMP-1", serial_number="COMP-1",
        purchase_date=date(2026, 8, 1),
    )
    ops = User.objects.create_user(username="comp-ops", password="x", role="ops_manager")
    client = APIClient()
    client.force_authenticate(ops)
    from apps.assets.models import MaterialType
    from apps.inventory.models import InventoryItem

    card_stock = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Comp Receiving Card"), quantity=10
    )
    r = client.post("/api/assets/components/", {
        "device": str(device.pk),
        "name": "Receiving Card",
        "component_type": "Card",
        "quantity": 4,
        "supplier": str(supplier.pk),
        "inventory_item": str(card_stock.pk),
        "warranty_type": "supplier",
        "warranty_months": 12,
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["supplier_name"] == "Comp Supplier"
    assert not Warranty.objects.filter(component_id=r.data["id"]).exists()

    # component from another device is rejected on warranty create
    other = Device.objects.create(device_model=dm, asset_code="AST-COMP-2", serial_number="COMP-2")
    r2 = client.post("/api/warranties/", {
        "device": str(other.pk),
        "component": r.data["id"],
        "warranty_type": "supplier",
        "start_date": "2026-08-01",
        "end_date": "2027-08-01",
    }, format="json")
    assert r2.status_code == 400


# ── Device status machine (WF-05/07/08) ───────────────────────────────

from apps.accounts.models import AuditLog
from apps.assets.models import DeviceLifecycleEvent


@pytest.mark.django_db
def test_transition_writes_lifecycle_event_and_audit_log(admin_client, device):
    # Entering production needs the components the asset is built from.
    _stock_component(device)
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "in_production", "reason": "Assembly started"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "in_production"
    assert set(body["allowed_transitions"]) == {"in_stock", "rma"}

    event = DeviceLifecycleEvent.objects.get(
        device=device, event_type="status_change", from_value="procured"
    )
    assert event.to_value == "in_production"
    assert event.description == "Assembly started"
    assert event.performed_by.username == "labeladmin"

    log = AuditLog.objects.get(
        resource_type="device", resource_id=str(device.id), action="update"
    )
    assert log.user.username == "labeladmin"
    assert log.detail == {"from": "procured", "to": "in_production", "reason": "Assembly started"}


@pytest.mark.django_db
def test_invalid_transition_rejected_with_allowed_list(admin_client, device):
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "active", "reason": "skip ahead"},
        format="json",
    )
    assert resp.status_code == 400
    # error names the allowed next statuses
    assert "in_stock" in str(resp.json()["status"])
    # Only the registration journal entry exists — the rejected flip left none.
    assert not (
        DeviceLifecycleEvent.objects.filter(device=device)
        .exclude(description="Registered")
        .exists()
    )
    device.refresh_from_db()
    assert device.status == "procured"


@pytest.mark.django_db
def test_transition_requires_reason(admin_client, device):
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "in_stock"},
        format="json",
    )
    assert resp.status_code == 400
    resp = admin_client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "in_stock", "reason": "  "},
        format="json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_transition_role_gate(device):
    tech = User.objects.create_user(username="statetech", password="x", role="technician")
    client = APIClient()
    client.force_authenticate(tech)
    resp = client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "in_stock", "reason": "QC passed"},
        format="json",
    )
    assert resp.status_code == 403

    warehouse = User.objects.create_user(username="statewh", password="x", role="warehouse")
    client.force_authenticate(warehouse)
    resp = client.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "in_stock", "reason": "QC passed"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "in_stock"


@pytest.mark.django_db
def test_api_create_with_status_journals_initial_status(admin_client, device):
    """Status is system-tracked: registration always starts at Procured."""
    resp = admin_client.post("/api/assets/devices/", {
        "serial_number": "SN-CREATE-ACTIVE",
        # Deliberately supplied — and deliberately ignored.
        "status": "active",
    }, format="json")
    assert resp.status_code == 201, resp.content
    new_id = resp.json()["id"]
    assert resp.json()["status"] == "procured"

    events = DeviceLifecycleEvent.objects.filter(device_id=new_id)
    assert events.count() == 1  # creation journals once, no double-fire
    event = events.get()
    assert event.event_type == "status_change"
    assert event.from_value == "" and event.to_value == "procured"
    assert event.description == "Registered"

    log = AuditLog.objects.get(resource_type="device", resource_id=new_id)
    assert log.action == "create"
    assert log.detail == {"from": "", "to": "procured", "reason": "Registered"}


@pytest.mark.django_db
def test_direct_create_journals_default_status(db):
    brand = Brand.objects.create(name="CreateBrand")
    dm = DeviceModel.objects.create(brand=brand, name="CR-1")
    dev = Device.objects.create(device_model=dm, serial_number="SN-CREATE-DEF")

    event = DeviceLifecycleEvent.objects.get(device=dev)
    assert event.from_value == "" and event.to_value == "procured"
    assert event.performed_by is None


@pytest.mark.django_db
def test_status_not_editable_via_plain_update(admin_client, device):
    resp = admin_client.patch(
        f"/api/assets/devices/{device.id}/", {"status": "active"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    device.refresh_from_db()
    assert device.status == "procured"


@pytest.mark.django_db
def test_the_three_delivery_routes(admin_client):
    """In-house build, vendor supplied, or vendor supplied and installed."""
    routes = {
        "inhouse": "SN-ROUTE-INHOUSE",
        "vendor_supplied": "SN-ROUTE-SUPPLIED",
        "vendor_turnkey": "SN-ROUTE-TURNKEY",
    }
    for source, serial in routes.items():
        r = admin_client.post(
            "/api/assets/devices/", {"serial_number": serial, "source": source}, format="json",
        )
        assert r.status_code == 201, r.content
        assert r.json()["source"] == source

    # Only an in-house build carries a production route.
    inhouse = admin_client.get("/api/assets/devices/", {"source": "inhouse"}).json()
    assert "SN-ROUTE-INHOUSE" in {r["serial_number"] for r in inhouse.get("results", inhouse)}

    turnkey_id = admin_client.get(
        "/api/assets/devices/", {"search": "SN-ROUTE-TURNKEY"}
    ).json()["results"][0]["id"]
    detail = admin_client.get(f"/api/assets/devices/{turnkey_id}/").json()
    assert detail["requires_production"] is False
    assert detail["requires_oversight"] is True
    assert detail["source_display"] == "Vendor Supplied & Installed"


@pytest.mark.django_db
def test_an_inhouse_asset_requires_a_production_route(admin_client):
    r = admin_client.post(
        "/api/assets/devices/", {"serial_number": "SN-ROUTE-2", "source": "inhouse"}, format="json",
    )
    detail = admin_client.get(f"/api/assets/devices/{r.json()['id']}/").json()
    assert detail["requires_production"] is True
    assert detail["requires_oversight"] is False
    assert detail["production_steps"] == []


# ── Excel export (XC-01) ──────────────────────────────────────────────

import io as _io

from openpyxl import load_workbook as _load_workbook

from apps.accounts.models import AuditLog as _AuditLog

XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _sheet_rows(resp):
    wb = _load_workbook(_io.BytesIO(resp.content), read_only=True)
    return [list(row) for row in wb.active.iter_rows(values_only=True)]


@pytest.mark.django_db
def test_devices_export_happy_path(admin_client, device):
    brand = device.device_model.brand
    dm2 = DeviceModel.objects.create(brand=brand, name="LB-77")
    Device.objects.create(device_model=dm2, serial_number="SN-EXPORT-2", status="in_stock")

    resp = admin_client.get("/api/assets/devices/export/")
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"] == XLSX_CT
    assert "assets-" in resp["Content-Disposition"] and ".xlsx" in resp["Content-Disposition"]

    rows = _sheet_rows(resp)
    assert rows[0][:3] == ["Asset Code", "Serial Number", "Name"]
    assert len(rows) == 3  # header + 2 devices

    log = _AuditLog.objects.filter(action="export", resource_type="device").latest("created_at")
    assert log.detail["count"] == 2
    assert log.user is not None


@pytest.mark.django_db
def test_devices_export_applies_filters(admin_client, device):
    brand = device.device_model.brand
    dm2 = DeviceModel.objects.create(brand=brand, name="LB-88")
    Device.objects.create(device_model=dm2, serial_number="SN-EXPORT-3", status="in_stock")

    resp = admin_client.get("/api/assets/devices/export/", {"status": "in_stock"})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert len(rows) == 2  # header + 1 matching device
    assert rows[1][1] == "SN-EXPORT-3"

    log = _AuditLog.objects.filter(action="export", resource_type="device").latest("created_at")
    assert log.detail == {"count": 1, "params": {"status": "in_stock"}}


# ── Wave 5: ?flag= drill-downs + search parity ────────────────────────


@pytest.fixture
def flag_devices(device):
    dm = device.device_model  # `device` itself stays procured
    return {
        "active": Device.objects.create(device_model=dm, serial_number="SN-FLAG-ACT", status="active"),
        "installed": Device.objects.create(device_model=dm, serial_number="SN-FLAG-INST", status="installed"),
        "stock": Device.objects.create(device_model=dm, serial_number="SN-FLAG-STOCK", status="in_stock"),
    }


@pytest.mark.django_db
def test_device_flag_operational(admin_client, flag_devices):
    r = admin_client.get("/api/assets/devices/", {"flag": "operational", "page_size": 100})
    assert r.status_code == 200, r.content
    serials = {row["serial_number"] for row in r.data["results"]}
    assert serials == {"SN-FLAG-ACT", "SN-FLAG-INST"}


@pytest.mark.django_db
def test_device_flag_warranty_expired(admin_client, flag_devices):
    from datetime import timedelta

    from django.utils import timezone

    from apps.warranties.models import Warranty

    today = timezone.localdate()
    # Expired-only warranty → in the bucket.
    Warranty.objects.create(
        device=flag_devices["active"], warranty_type="client", status="expired",
        start_date=today - timedelta(days=400), end_date=today - timedelta(days=35), months=12,
    )
    # Expired AND active warranties → still covered, excluded.
    Warranty.objects.create(
        device=flag_devices["installed"], warranty_type="client", status="expired",
        start_date=today - timedelta(days=400), end_date=today - timedelta(days=35), months=12,
    )
    Warranty.objects.create(
        device=flag_devices["installed"], warranty_type="client", status="active",
        start_date=today, end_date=today + timedelta(days=365), months=12,
    )
    # flag_devices["stock"] has no warranties at all → excluded.

    r = admin_client.get("/api/assets/devices/", {"flag": "warranty_expired", "page_size": 100})
    assert r.status_code == 200, r.content
    serials = [row["serial_number"] for row in r.data["results"]]
    assert serials == ["SN-FLAG-ACT"]

    # Unknown flag values are ignored — full list comes back.
    r = admin_client.get("/api/assets/devices/", {"flag": "bogus", "page_size": 100})
    assert r.status_code == 200
    assert len(r.data["results"]) == 4


@pytest.mark.django_db
def test_device_flag_applies_to_export(admin_client, flag_devices):
    resp = admin_client.get("/api/assets/devices/export/", {"flag": "operational"})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert len(rows) == 3  # header + active + installed
    assert {r[1] for r in rows[1:]} == {"SN-FLAG-ACT", "SN-FLAG-INST"}

    log = _AuditLog.objects.filter(action="export", resource_type="device").latest("created_at")
    assert log.detail == {"count": 2, "params": {"flag": "operational"}}


@pytest.mark.django_db
def test_device_search_by_model_site_and_client(admin_client, device):
    from apps.clients.models import Client
    from apps.sites.models import Site

    site = Site.objects.create(name="Searchable Plaza", city="Lahore")
    client = Client.objects.create(name="Searchable Client Co")
    device.current_site = site
    device.assigned_client = client
    device.save(update_fields=["current_site", "assigned_client", "updated_at"])

    for term in ("LB-55", "Searchable Plaza", "Searchable Client"):
        r = admin_client.get("/api/assets/devices/", {"search": term})
        assert r.status_code == 200, r.content
        assert device.serial_number in {row["serial_number"] for row in r.data["results"]}, term

    r = admin_client.get("/api/assets/devices/", {"search": "no-such-thing-xyz"})
    assert r.data["results"] == []


@pytest.mark.django_db
def test_device_detail_exposes_project_contract(admin_client, device):
    from apps.teams.models import Project

    # No project — contract fields present but empty.
    r = admin_client.get(f"/api/assets/devices/{device.id}/")
    assert r.status_code == 200, r.content
    assert r.json()["project_contract_type"] is None
    assert r.json()["project_rental_end_date"] is None

    project = Project.objects.create(
        name="Rental Order", contract_type="rental", rental_end_date="2027-01-31"
    )
    device.project = project
    device.save(update_fields=["project", "updated_at"])

    r = admin_client.get(f"/api/assets/devices/{device.id}/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["project_contract_type"] == "rental"
    assert body["project_rental_end_date"] == "2027-01-31"


# ── Vendor access (XC-04): device visibility is derived & read-only ──


@pytest.fixture
def vendor_devices(db):
    from django.utils import timezone

    from apps.sites.models import DeviceInstallation, Site
    from apps.suppliers.models import Supplier
    from apps.tickets.models import Ticket

    supplier = Supplier.objects.create(name="Asset Vendor")
    vendor_user = User.objects.create_user(
        username="asset-vendor", password="x", role="vendor", supplier=supplier
    )
    unlinked_vendor = User.objects.create_user(username="asset-vendor-none", password="x", role="vendor")

    brand = Brand.objects.create(name="VendAssetBrand")
    model = DeviceModel.objects.create(brand=brand, name="VA-55")
    dev_ticket = Device.objects.create(device_model=model, serial_number="SN-VEND-T")
    dev_install = Device.objects.create(device_model=model, serial_number="SN-VEND-I")
    dev_other = Device.objects.create(device_model=model, serial_number="SN-VEND-X")

    Ticket.objects.create(title="vendor repair", device=dev_ticket, assigned_vendor=supplier)
    site = Site.objects.create(name="Vendor Asset Site")
    DeviceInstallation.objects.create(
        device=dev_install, site=site, vendor=supplier, installed_at=timezone.now()
    )
    return {
        "vendor_user": vendor_user,
        "unlinked_vendor": unlinked_vendor,
        "dev_ticket": dev_ticket,
        "dev_install": dev_install,
        "dev_other": dev_other,
    }


def _auth(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.django_db
def test_vendor_sees_only_devices_from_their_tickets_and_installations(vendor_devices):
    c = _auth(vendor_devices["vendor_user"])
    r = c.get("/api/assets/devices/")
    assert r.status_code == 200
    ids = {row["id"] for row in r.data["results"]}
    assert ids == {str(vendor_devices["dev_ticket"].id), str(vendor_devices["dev_install"].id)}
    # The unrelated device is invisible even by direct URL.
    assert c.get(f"/api/assets/devices/{vendor_devices['dev_other'].id}/").status_code == 404
    # A vendor login without a supplier sees no devices at all.
    assert _auth(vendor_devices["unlinked_vendor"]).get("/api/assets/devices/").data["count"] == 0


@pytest.mark.django_db
def test_vendor_devices_are_read_only(vendor_devices):
    c = _auth(vendor_devices["vendor_user"])
    device = vendor_devices["dev_ticket"]
    # No PATCH — vendors are in no write-role group.
    r = c.patch(f"/api/assets/devices/{device.id}/", {"display_name": "hacked"}, format="json")
    assert r.status_code == 403
    # No status transitions either — the transition role gate excludes vendors.
    r = c.post(f"/api/assets/devices/{device.id}/transition/", {"status": "active", "reason": "no"}, format="json")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Components are REQUIREMENTS: what the asset needs, not what has been taken
# ---------------------------------------------------------------------------
@pytest.fixture
def stock_refs(db):
    from apps.assets.models import Brand, MaterialType
    from apps.inventory.models import InventoryItem, InventoryUnitType

    material = MaterialType.objects.create(name="Comp Cabinet", unit="piece")
    brand = Brand.objects.create(name="Comp Brand")
    item = InventoryItem.objects.create(material_type=material, quantity=10, unit_cost=50)
    product = InventoryUnitType.objects.create(
        name="Comp Media Player", material_type=material, brand=brand, model_name="MP-9",
    )
    return {"material": material, "item": item, "product": product}


@pytest.mark.django_db
def test_component_records_a_requirement_without_touching_stock(admin_client, device, stock_refs):
    from apps.inventory.models import StockMovement

    item = stock_refs["item"]
    r = admin_client.post(
        "/api/assets/components/",
        {"device": str(device.id), "inventory_item": str(item.id), "quantity": 3},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["quantity"] == 3
    assert r.data["name"] == "Comp Cabinet"
    assert r.data["source_label"].startswith("Stock ·")
    # Availability is reported for information, never deducted.
    assert r.data["available_quantity"] == 10
    item.refresh_from_db()
    assert item.quantity == 10, "specifying a requirement must not move stock"
    assert not StockMovement.objects.filter(item=item).exists()


@pytest.mark.django_db
def test_component_may_require_more_than_is_available(admin_client, device, stock_refs):
    """A build can be specified long before the parts exist."""
    item = stock_refs["item"]
    r = admin_client.post(
        "/api/assets/components/",
        {"device": str(device.id), "inventory_item": str(item.id), "quantity": 500},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["quantity"] == 500
    assert r.data["available_quantity"] == 10
    item.refresh_from_db()
    assert item.quantity == 10


@pytest.mark.django_db
def test_component_can_require_a_unique_product(admin_client, device, stock_refs):
    product = stock_refs["product"]
    r = admin_client.post(
        "/api/assets/components/",
        {"device": str(device.id), "inventory_unit_type": str(product.id), "quantity": 4},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["quantity"] == 4, "a unique requirement can be for several units"
    assert r.data["inventory_unit_type_name"] == "Comp Media Player MP-9"
    assert r.data["source_label"] == "Unique · Comp Media Player MP-9"
    assert r.data["available_quantity"] == 0, "the product was opened with no stock"


@pytest.mark.django_db
def test_component_without_any_inventory_reference_is_rejected(admin_client, device):
    r = admin_client.post(
        "/api/assets/components/",
        {"device": str(device.id), "name": "Hand-typed part", "quantity": 1},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "inventory_item" in r.data


@pytest.mark.django_db
def test_component_rejects_both_kinds_at_once(admin_client, device, stock_refs):
    r = admin_client.post(
        "/api/assets/components/",
        {
            "device": str(device.id),
            "inventory_item": str(stock_refs["item"].id),
            "inventory_unit_type": str(stock_refs["product"].id),
        },
        format="json",
    )
    assert r.status_code == 400, r.content


@pytest.mark.django_db
def test_removing_a_requirement_leaves_stock_alone(admin_client, device, stock_refs):
    from apps.inventory.models import StockMovement

    item = stock_refs["item"]
    created = admin_client.post(
        "/api/assets/components/",
        {"device": str(device.id), "inventory_item": str(item.id), "quantity": 4},
        format="json",
    )
    assert created.status_code == 201, created.content

    deleted = admin_client.delete(f"/api/assets/components/{created.data['id']}/")
    assert deleted.status_code == 204, deleted.content
    item.refresh_from_db()
    assert item.quantity == 10
    assert not StockMovement.objects.exists(), "requirements never write to the stock ledger"


@pytest.mark.django_db
def test_the_same_product_can_be_required_by_two_assets(admin_client, device, stock_refs):
    """Requirements are not reservations, so they do not compete."""
    from apps.assets.models import Brand, Device, DeviceModel

    brand = Brand.objects.create(name="Second Brand")
    model = DeviceModel.objects.create(brand=brand, name="S-1")
    other = Device.objects.create(device_model=model, serial_number="SECOND-SN-1")

    for target in (device, other):
        r = admin_client.post(
            "/api/assets/components/",
            {"device": str(target.id), "inventory_unit_type": str(stock_refs["product"].id), "quantity": 2},
            format="json",
        )
        assert r.status_code == 201, r.content


# ---------------------------------------------------------------------------
# Assigning an asset: the transition to `assigned` must name the assignee
# ---------------------------------------------------------------------------
def _a_technician():
    return User.objects.create_user(
        username=f"pair-tech-{User.objects.count()}", password="x", role="technician"
    )


@pytest.fixture
def in_stock_device(db):
    from apps.sites.models import Site

    brand = Brand.objects.create(name="AssignBrand")
    model = DeviceModel.objects.create(brand=brand, name="A-1")
    # Assigning opens the installation job, and a job happens somewhere, so
    # an assignable asset has a site.
    return Device.objects.create(
        device_model=model, serial_number="ASSIGN-SN-1", status=Device.Status.IN_STOCK,
        current_site=Site.objects.create(name="Assign Site"),
    )


@pytest.mark.django_db
def test_assigning_without_a_site_says_so(admin_client, db):
    """The tracker is where the work is run, and it needs a location."""
    tech = User.objects.create_user(username="siteless-tech", password="x", role="technician")
    asset = Device.objects.create(
        serial_number="NOSITE-SN-1", status=Device.Status.IN_STOCK,
    )
    r = admin_client.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "assigned", "reason": "off you go", "assigned_technician": str(tech.id)},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "Installation Tracker" in str(r.data["current_site"])


@pytest.mark.django_db
def test_assign_requires_a_technician_or_vendor(admin_client, in_stock_device):
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {"status": "assigned", "reason": "Ready to install"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "assigned_technician" in r.data


@pytest.mark.django_db
def test_assign_to_technician_pulls_manpower_details(admin_client, in_stock_device):
    tech = User.objects.create_user(
        username="assign-tech", password="x", role="technician", is_field_staff=True,
        first_name="Usman", last_name="Ali", employee_id="EMP-221",
        job_title="Installation Technician", phone="0300-1112222",
    )
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {"status": "assigned", "reason": "Site ready", "assigned_technician": str(tech.id)},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "assigned"
    assert r.data["technician_name"] == "Usman Ali"
    assert r.data["technician_employee_id"] == "EMP-221"
    assert r.data["technician_job_title"] == "Installation Technician"
    assert r.data["assigned_to_display"] == "Usman Ali · EMP-221 · Installation Technician"
    assert r.data["assigned_vendor_name"] == ""


@pytest.mark.django_db
def test_assign_to_vendor_by_hand(admin_client, in_stock_device):
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {
            "status": "assigned", "reason": "Outsourced",
            "assigned_vendor_name": "Rapid Signage Crew",
            "assigned_vendor_contact": "0321-9876543",
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["assigned_to_display"] == "Rapid Signage Crew (0321-9876543)"
    assert r.data["assigned_technician"] is None


@pytest.mark.django_db
def test_assign_rejects_both_technician_and_vendor(admin_client, in_stock_device):
    tech = User.objects.create_user(username="assign-both", password="x", role="technician")
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {
            "status": "assigned", "reason": "Confused",
            "assigned_technician": str(tech.id), "assigned_vendor_name": "Some Crew",
        },
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "assigned_vendor_name" in r.data


@pytest.mark.django_db
def test_assignment_is_journalled_in_the_lifecycle(admin_client, in_stock_device):
    from apps.assets.models import DeviceLifecycleEvent

    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {"status": "assigned", "reason": "Outsourced", "assigned_vendor_name": "Rapid Crew"},
        format="json",
    )
    assert r.status_code == 200, r.content
    event = DeviceLifecycleEvent.objects.filter(device=in_stock_device).order_by("-created_at").first()
    assert event is not None
    assert "Assigned to vendor Rapid Crew" in event.description


@pytest.mark.django_db
def test_other_transitions_need_no_assignee(admin_client, in_stock_device):
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/transition/",
        {"status": "in_transit", "reason": "Moving to depot"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "in_transit"


# ---------------------------------------------------------------------------
# Reassignment: changing who an assigned asset is with, mid-installation
# ---------------------------------------------------------------------------
@pytest.fixture
def turnkey_assigned_device(db, in_stock_device):
    """An asset the vendor installs — the only route with an outside crew,
    with our technician overseeing them."""
    tech = User.objects.create_user(
        username="turnkey-first", password="x", role="technician", is_field_staff=True,
        first_name="First", last_name="Tech", employee_id="EMP-001",
    )
    in_stock_device.source = Device.Source.VENDOR_TURNKEY
    in_stock_device.status = Device.Status.ASSIGNED
    in_stock_device.assigned_technician = tech
    in_stock_device.save()
    return in_stock_device


@pytest.fixture
def assigned_device(db, in_stock_device):
    tech = User.objects.create_user(
        username="reassign-first", password="x", role="technician", is_field_staff=True,
        first_name="First", last_name="Tech", employee_id="EMP-001",
    )
    in_stock_device.status = Device.Status.ASSIGNED
    in_stock_device.assigned_technician = tech
    in_stock_device.save()
    return {"device": in_stock_device, "tech": tech}


@pytest.mark.django_db
def test_reassign_to_a_different_technician(admin_client, assigned_device):
    device = assigned_device["device"]
    other = User.objects.create_user(
        username="reassign-second", password="x", role="technician", is_field_staff=True,
        first_name="Second", last_name="Tech", employee_id="EMP-002",
        job_title="Lead Installer",
    )
    r = admin_client.post(
        f"/api/assets/devices/{device.id}/reassign/",
        {"assigned_technician": str(other.id), "reason": "First tech is on leave"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["technician_name"] == "Second Tech"
    assert r.data["assigned_to_display"] == "Second Tech · EMP-002 · Lead Installer"
    # Status is untouched — this is not a transition.
    assert r.data["status"] == "assigned"


@pytest.mark.django_db
def test_reassign_from_technician_to_vendor(admin_client, assigned_device):
    device = assigned_device["device"]
    r = admin_client.post(
        f"/api/assets/devices/{device.id}/reassign/",
        {
            "assigned_vendor_name": "Rapid Crew", "assigned_vendor_contact": "0321-1112222",
            "reason": "Outsourcing the install",
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["assigned_technician"] is None, "the previous technician is cleared"
    assert r.data["assigned_to_display"] == "Rapid Crew (0321-1112222)"


@pytest.mark.django_db
def test_reassignment_is_journalled_with_both_sides(admin_client, assigned_device):
    from apps.assets.models import DeviceLifecycleEvent

    device = assigned_device["device"]
    r = admin_client.post(
        f"/api/assets/devices/{device.id}/reassign/",
        {"assigned_vendor_name": "Rapid Crew", "reason": "Tech unavailable"},
        format="json",
    )
    assert r.status_code == 200, r.content
    event = DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.REASSIGNMENT
    ).latest("created_at")
    assert "First Tech" in event.from_value
    assert event.to_value == "vendor Rapid Crew"
    assert "Tech unavailable" in event.description


@pytest.mark.django_db
def test_reassign_still_needs_exactly_one_assignee(admin_client, assigned_device):
    device = assigned_device["device"]
    empty = admin_client.post(
        f"/api/assets/devices/{device.id}/reassign/", {"reason": "who?"}, format="json",
    )
    assert empty.status_code == 400, empty.content
    assert "assigned_technician" in empty.data

    both = admin_client.post(
        f"/api/assets/devices/{device.id}/reassign/",
        {
            "assigned_technician": str(assigned_device["tech"].id),
            "assigned_vendor_name": "Crew", "reason": "both",
        },
        format="json",
    )
    assert both.status_code == 400, both.content


@pytest.mark.django_db
def test_cannot_reassign_an_unassigned_asset(admin_client, in_stock_device):
    tech = User.objects.create_user(username="reassign-none", password="x", role="technician")
    r = admin_client.post(
        f"/api/assets/devices/{in_stock_device.id}/reassign/",
        {"assigned_technician": str(tech.id), "reason": "nope"},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "Only an assigned asset" in r.data["detail"]


@pytest.mark.django_db
def test_reassign_requires_a_reason(admin_client, assigned_device):
    other = User.objects.create_user(username="reassign-noreason", password="x", role="technician")
    r = admin_client.post(
        f"/api/assets/devices/{assigned_device['device'].id}/reassign/",
        {"assigned_technician": str(other.id)}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "reason" in r.data


@pytest.mark.django_db
def test_technician_cannot_reassign(db, assigned_device):
    from rest_framework.test import APIClient

    tech = User.objects.create_user(username="reassign-tech-role", password="x", role="technician")
    c = APIClient(); c.force_authenticate(tech)
    r = c.post(
        f"/api/assets/devices/{assigned_device['device'].id}/reassign/",
        {"assigned_vendor_name": "Crew", "reason": "trying"}, format="json",
    )
    assert r.status_code == 403, r.content


# ---------------------------------------------------------------------------
# Changing the assignee from the Edit Asset form (a plain PATCH)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_edit_form_can_switch_technician(admin_client, assigned_device):
    device = assigned_device["device"]
    other = User.objects.create_user(
        username="edit-swap", password="x", role="technician", is_field_staff=True,
        first_name="Swap", last_name="Tech", employee_id="EMP-777",
    )
    r = admin_client.patch(
        f"/api/assets/devices/{device.id}/",
        {"assigned_technician": str(other.id), "assigned_vendor_name": "", "assigned_vendor_contact": ""},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["technician_name"] == "Swap Tech"
    assert r.data["assigned_to_display"] == "Swap Tech · EMP-777"


@pytest.mark.django_db
def test_edit_form_can_switch_to_a_third_party_vendor(admin_client, turnkey_assigned_device):
    device = turnkey_assigned_device
    r = admin_client.patch(
        f"/api/assets/devices/{device.id}/",
        {
            "assigned_technician": None,
            "assigned_vendor_name": "Third Party Crew",
            "assigned_vendor_contact": "0345-1112222",
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["assigned_technician"] is None
    assert r.data["assigned_to_display"] == "Third Party Crew (0345-1112222)"


@pytest.mark.django_db
def test_edit_form_rejects_both_assignees(admin_client, assigned_device):
    """Only a turnkey job has a vendor installing and a technician overseeing;
    anywhere else naming both is a mistake."""
    device = assigned_device["device"]
    r = admin_client.patch(
        f"/api/assets/devices/{device.id}/",
        {"assigned_technician": str(assigned_device["tech"].id), "assigned_vendor_name": "Crew"},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "assigned_vendor_name" in r.data


@pytest.mark.django_db
def test_assignment_change_from_the_edit_form_is_journalled(admin_client, turnkey_assigned_device):
    from apps.assets.models import DeviceLifecycleEvent

    device = turnkey_assigned_device
    r = admin_client.patch(
        f"/api/assets/devices/{device.id}/",
        {"assigned_technician": None, "assigned_vendor_name": "Third Party Crew"},
        format="json",
    )
    assert r.status_code == 200, r.content
    event = DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.REASSIGNMENT
    ).latest("created_at")
    assert "First Tech" in event.from_value
    assert event.to_value == "vendor Third Party Crew"


@pytest.mark.django_db
def test_editing_something_else_does_not_journal_a_reassignment(admin_client, assigned_device):
    from apps.assets.models import DeviceLifecycleEvent

    device = assigned_device["device"]
    r = admin_client.patch(
        f"/api/assets/devices/{device.id}/", {"display_name": "Renamed"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert not DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.REASSIGNMENT
    ).exists()


# ---------------------------------------------------------------------------
# The technician closes out their own installation: photo + status → Active
# ---------------------------------------------------------------------------
@pytest.fixture
def installed_device(db):
    brand = Brand.objects.create(name="ActiveBrand")
    model = DeviceModel.objects.create(brand=brand, name="AC-1")
    tech = User.objects.create_user(
        username="active-tech", password="x", role="technician", is_field_staff=True,
        first_name="Field", last_name="Tech",
    )
    device = Device.objects.create(
        device_model=model, serial_number="ACTIVE-SN-1",
        status=Device.Status.INSTALLED, assigned_technician=tech,
    )
    return {"device": device, "tech": tech}


def _tech_client(user):
    from rest_framework.test import APIClient

    c = APIClient()
    c.force_authenticate(user)
    return c


def _tiny_png():
    import base64

    from django.core.files.uploadedfile import SimpleUploadedFile

    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return SimpleUploadedFile("install.png", data, content_type="image/png")


@pytest.mark.django_db
def test_active_is_not_typed_into_the_registry(installed_device, admin_client):
    """Active belongs to the Installation Tracker — even for management."""
    r = admin_client.post(
        f"/api/assets/devices/{installed_device['device'].id}/transition/",
        {"status": "active", "reason": "Installed and powered on"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "Installation Tracker" in str(r.data["status"])
    installed_device["device"].refresh_from_db()
    assert installed_device["device"].status == "installed"


@pytest.mark.django_db
def test_assigned_technician_can_upload_the_installation_photo(installed_device):
    c = _tech_client(installed_device["tech"])
    r = c.post(
        "/api/assets/device-images/",
        {
            "device": str(installed_device["device"].id),
            "image": _tiny_png(),
            "caption": "Installed asset — Active",
            "is_primary": True,
        },
        format="multipart",
    )
    assert r.status_code == 201, r.content
    assert installed_device["device"].images.count() == 1


@pytest.mark.django_db
def test_technician_cannot_make_other_status_changes(installed_device):
    c = _tech_client(installed_device["tech"])
    r = c.post(
        f"/api/assets/devices/{installed_device['device'].id}/transition/",
        {"status": "rma", "reason": "trying something else"}, format="json",
    )
    assert r.status_code == 403, r.content
    assert "Installation Tracker" in r.data["detail"]


@pytest.mark.django_db
def test_unrelated_technician_cannot_touch_the_asset(installed_device):
    other = User.objects.create_user(
        username="other-tech", password="x", role="technician", is_field_staff=True
    )
    c = _tech_client(other)
    trans = c.post(
        f"/api/assets/devices/{installed_device['device'].id}/transition/",
        {"status": "active", "reason": "not mine"}, format="json",
    )
    assert trans.status_code == 403, trans.content

    photo = c.post(
        "/api/assets/device-images/",
        {"device": str(installed_device["device"].id), "image": _tiny_png()},
        format="multipart",
    )
    assert photo.status_code == 403, photo.content


@pytest.mark.django_db
def test_managers_can_still_upload_photos_for_any_asset(admin_client, installed_device):
    r = admin_client.post(
        "/api/assets/device-images/",
        {"device": str(installed_device["device"].id), "image": _tiny_png()},
        format="multipart",
    )
    assert r.status_code == 201, r.content


# ---------------------------------------------------------------------------
# Build flow: procured → in production (needs components), no transit detour
# ---------------------------------------------------------------------------
@pytest.fixture
def procured_device(db):
    brand = Brand.objects.create(name="BuildBrand")
    model = DeviceModel.objects.create(brand=brand, name="B-1")
    return Device.objects.create(
        device_model=model, serial_number="BUILD-SN-1", status=Device.Status.PROCURED
    )


def _stock_component(device, qty=2, *, fulfilled=True):
    """Give the device a component drawn from inventory.

    Fulfilled by default: production cannot start until the project has
    covered every line, so most tests want a build that is ready to go.
    """
    from apps.assets.models import AssetComponent, MaterialType
    from apps.inventory.models import InventoryItem

    material = MaterialType.objects.create(
        name=f"Build Part {device.serial_number} #{MaterialType.objects.count()}"
    )
    item = InventoryItem.objects.create(material_type=material, quantity=50)
    return AssetComponent.objects.create(
        device=device, name=material.name, quantity=qty, inventory_item=item,
        issued_quantity=qty if fulfilled else 0,
        fulfilment=(
            AssetComponent.Fulfilment.FULFILLED if fulfilled
            else AssetComponent.Fulfilment.PENDING
        ),
    )


@pytest.mark.django_db
def test_production_waits_for_every_component_to_be_fulfilled(admin_client, procured_device):
    """Half a parts list is a stalled build, so the floor does not start."""
    _stock_component(procured_device, fulfilled=True)
    short = _stock_component(procured_device, qty=5, fulfilled=False)

    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "starting early"}, format="json",
    )
    assert r.status_code == 400, r.content
    message = str(r.data["status"])
    assert "every component is fulfilled" in message
    assert "short 5 of 5" in message

    # Once the project covers it, the build can start.
    short.issued_quantity = short.quantity
    short.fulfilment = short.Fulfilment.FULFILLED
    short.save(update_fields=["issued_quantity", "fulfilment"])

    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "materials in hand"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "in_production"


@pytest.mark.django_db
def test_procured_no_longer_offers_in_transit(admin_client, procured_device):
    r = admin_client.get(f"/api/assets/devices/{procured_device.id}/")
    assert r.status_code == 200, r.content
    assert "in_transit" not in r.data["allowed_transitions"]
    assert "in_production" in r.data["allowed_transitions"]

    blocked = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_transit", "reason": "shipping"}, format="json",
    )
    assert blocked.status_code == 400, blocked.content


@pytest.mark.django_db
def test_in_production_requires_components(admin_client, procured_device):
    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "starting the build"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "Components section is empty" in str(r.data["status"])
    procured_device.refresh_from_db()
    assert procured_device.status == "procured", "a blocked build must not move the asset"


@pytest.mark.django_db
def test_in_production_allowed_once_components_are_fulfilled(admin_client, procured_device):
    _stock_component(procured_device)
    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "starting the build"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "in_production"


@pytest.mark.django_db
def test_in_transit_can_reach_production(admin_client, procured_device):
    """Assets already in transit are not trapped away from the build floor."""
    procured_device.status = Device.Status.IN_TRANSIT
    procured_device.save(update_fields=["status"])
    _stock_component(procured_device)

    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "arrived at the workshop"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "in_production"


@pytest.mark.django_db
def test_a_stocked_asset_does_not_go_back_into_production(admin_client, procured_device):
    """Production ends when the asset reaches stock; a rebuild is maintenance,
    not a second trip through the build route."""
    procured_device.status = Device.Status.IN_STOCK
    procured_device.save(update_fields=["status"])
    detail = admin_client.get(f"/api/assets/devices/{procured_device.id}/").json()
    assert "in_production" not in detail["allowed_transitions"]
    r = admin_client.post(
        f"/api/assets/devices/{procured_device.id}/transition/",
        {"status": "in_production", "reason": "rebuild"}, format="json",
    )
    assert r.status_code == 400, r.content


# ---------------------------------------------------------------------------
# Project decides how each requirement is covered: stock or procurement
# ---------------------------------------------------------------------------
@pytest.fixture
def project_build(db, stock_refs):
    """A project with one asset that needs both kinds of inventory."""
    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel
    from apps.inventory.models import InventoryUnit
    from apps.teams.models import Project

    project = Project.objects.create(name="Build Project")
    brand = Brand.objects.create(name="PB Brand")
    model = DeviceModel.objects.create(brand=brand, name="PB-1")
    device = Device.objects.create(
        device_model=model, serial_number="PB-SN-1", project=project, display_name="Wall A",
    )
    generic = AssetComponent.objects.create(
        device=device, name="Comp Cabinet", quantity=4, inventory_item=stock_refs["item"],
    )
    unique = AssetComponent.objects.create(
        device=device, name="Comp Media Player", quantity=2,
        inventory_unit_type=stock_refs["product"],
    )
    # Two units of the product are actually on hand.
    for i in (1, 2):
        InventoryUnit.objects.create(
            serial_number=f"PB-UNIT-{i}", unit_type=stock_refs["product"]
        )
    return {"project": project, "device": device, "generic": generic, "unique": unique}


@pytest.mark.django_db
def test_project_gathers_every_asset_bom(admin_client, project_build):
    r = admin_client.get(f"/api/teams/projects/{project_build['project'].id}/requirements/")
    assert r.status_code == 200, r.content
    assets = r.data["assets"]
    assert len(assets) == 1
    assert assets[0]["asset_code"] == project_build["device"].asset_code
    assert len(assets[0]["components"]) == 2

    totals = r.data["totals"]
    assert totals["required"] == 6          # 4 generic + 2 unique
    assert totals["outstanding"] == 6
    assert totals["awaiting_decision"] == 2

    by_name = {c["name"]: c for c in assets[0]["components"]}
    assert by_name["Comp Cabinet"]["available_quantity"] == 10
    assert by_name["Comp Cabinet"]["can_use_stock"] is True
    assert by_name["Comp Media Player"]["available_quantity"] == 2
    assert by_name["Comp Media Player"]["can_use_stock"] is True


@pytest.mark.django_db
def _ask_store(client, component, quantity=None):
    """The project's half: decide to use stock, which queues a request."""
    payload = {} if quantity is None else {"quantity": quantity}
    return client.post(
        f"/api/assets/components/{component.id}/fulfil-from-stock/", payload, format="json",
    )


def _store_issues(client, component, quantity=None):
    """The store's half: hand the material over against the queued request."""
    from apps.inventory.models import IssuanceRequest

    row = IssuanceRequest.objects.filter(asset_component=component).latest("created_at")
    return client.post(
        f"/api/inventory/issuance-requests/{row.id}/issue/",
        {"quantity": quantity if quantity is not None else row.quantity_requested},
        format="json",
    )


@pytest.mark.django_db
def test_cover_a_generic_requirement_from_stock(admin_client, project_build, stock_refs):
    """Deciding to use stock queues it; the store moving it is what counts."""
    from apps.inventory.models import StockMovement

    component = project_build["generic"]
    r = _ask_store(admin_client, component)
    assert r.status_code == 200, r.content
    assert r.data["requested"] == 4
    assert r.data["request_number"]

    # Nothing has left the warehouse yet.
    stock_refs["item"].refresh_from_db()
    assert stock_refs["item"].quantity == 10, "the decision alone must not move stock"

    r = _store_issues(admin_client, component)
    assert r.status_code == 200, r.content
    assert r.data["issued"] == 4
    assert r.data["request"]["status"] == "fulfilled"

    component.refresh_from_db()
    assert component.fulfilment == "fulfilled"
    assert component.outstanding_quantity == 0

    stock_refs["item"].refresh_from_db()
    assert stock_refs["item"].quantity == 6, "stock moves when the store issues it"
    assert StockMovement.objects.filter(item=stock_refs["item"], movement_type="out").exists()


@pytest.mark.django_db
def test_cover_a_unique_requirement_consumes_serials(admin_client, project_build):
    from apps.inventory.models import InventoryUnit

    component = project_build["unique"]
    assert _ask_store(admin_client, component).status_code == 200
    r = _store_issues(admin_client, component)
    assert r.status_code == 200, r.content
    assert sorted(r.data["serials"]) == ["PB-UNIT-1", "PB-UNIT-2"]
    assert InventoryUnit.objects.filter(status="issued").count() == 2
    # The serials travel on the request, so the issue slip can list them.
    assert sorted(r.data["request"]["issued_serials"]) == ["PB-UNIT-1", "PB-UNIT-2"]


@pytest.mark.django_db
def test_partial_issue_leaves_the_rest_outstanding(admin_client, project_build):
    """The store can hand over less than was asked; the balance stays owed."""
    component = project_build["generic"]
    assert _ask_store(admin_client, component, 2).status_code == 200

    r = _store_issues(admin_client, component, 1)
    assert r.status_code == 200, r.content
    assert r.data["request"]["status"] == "partial"
    assert r.data["request"]["outstanding_quantity"] == 1, "the balance stays on the queue"

    component.refresh_from_db()
    assert component.issued_quantity == 1
    assert component.outstanding_quantity == 3
    assert component.fulfilment == "from_stock"


@pytest.mark.django_db
def test_cannot_issue_more_than_the_requirement(admin_client, project_build):
    component = project_build["generic"]
    r = admin_client.post(
        f"/api/assets/components/{component.id}/fulfil-from-stock/",
        {"quantity": 99}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "outstanding" in str(r.data["quantity"])


@pytest.mark.django_db
def test_issuing_more_than_stock_is_refused_with_a_hint(admin_client, project_build, stock_refs):
    stock_refs["item"].quantity = 1
    stock_refs["item"].save(update_fields=["quantity"])
    component = project_build["generic"]
    # Asking is always allowed — the shortfall is the store's problem to report.
    assert _ask_store(admin_client, component).status_code == 200

    r = _store_issues(admin_client, component)
    assert r.status_code == 400, r.content
    assert "procure the shortfall" in str(r.data["quantity"])


@pytest.mark.django_db
def test_procurement_is_allowed_even_when_stock_would_cover_it(admin_client, project_build, stock_refs):
    """The user decides whether to buy — availability never forces the hand."""
    component = project_build["generic"]
    assert stock_refs["item"].quantity >= component.quantity

    r = admin_client.post(
        f"/api/assets/components/{component.id}/mark-for-procurement/", {}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["fulfilment"] == "procurement"
    stock_refs["item"].refresh_from_db()
    assert stock_refs["item"].quantity == 10, "flagging for purchase must not touch stock"


@pytest.mark.django_db
def test_resetting_a_decision_returns_issued_stock(admin_client, project_build, stock_refs):
    from apps.inventory.models import IssuanceRequest

    component = project_build["generic"]
    _ask_store(admin_client, component)
    _store_issues(admin_client, component)
    stock_refs["item"].refresh_from_db()
    assert stock_refs["item"].quantity == 6

    r = admin_client.post(
        f"/api/assets/components/{component.id}/reset-fulfilment/", {}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["fulfilment"] == "pending"
    assert r.data["issued_quantity"] == 0
    stock_refs["item"].refresh_from_db()
    assert stock_refs["item"].quantity == 10
    # Undoing the decision also withdraws anything still sitting with the store.
    assert not IssuanceRequest.objects.filter(asset_component=component).exclude(
        status=IssuanceRequest.Status.CANCELLED
    ).exists()


# ---------------------------------------------------------------------------
# Registration: no serial typed in — the asset code is generated
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_register_an_asset_without_a_serial(admin_client, db):
    brand = Brand.objects.create(name="NoSerial Brand")
    model = DeviceModel.objects.create(brand=brand, name="NS-1")
    r = admin_client.post(
        "/api/assets/devices/",
        {"device_model": str(model.id), "source": "inhouse"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["asset_code"].startswith("DGX-")
    # Serial falls back to the generated code so the unique index is safe.
    assert r.data["serial_number"] == r.data["asset_code"]


@pytest.mark.django_db
def test_two_assets_without_serials_do_not_collide(admin_client, db):
    brand = Brand.objects.create(name="NoSerial Brand 2")
    model = DeviceModel.objects.create(brand=brand, name="NS-2")
    codes = set()
    for _ in range(3):
        r = admin_client.post(
            "/api/assets/devices/", {"device_model": str(model.id)}, format="json",
        )
        assert r.status_code == 201, r.content
        codes.add(r.data["serial_number"])
    assert len(codes) == 3


@pytest.mark.django_db
def test_a_manufacturer_serial_is_still_accepted(admin_client, db):
    brand = Brand.objects.create(name="NoSerial Brand 3")
    model = DeviceModel.objects.create(brand=brand, name="NS-3")
    r = admin_client.post(
        "/api/assets/devices/",
        {"device_model": str(model.id), "serial_number": "MFR-12345"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["serial_number"] == "MFR-12345"


# ---------------------------------------------------------------------------
# The build shows up on the asset's own lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_issuing_parts_journals_and_starts_the_build(admin_client, project_build):
    from apps.assets.models import DeviceLifecycleEvent

    device = project_build["device"]
    assert device.status == Device.Status.PROCURED

    assert _ask_store(admin_client, project_build["generic"], 2).status_code == 200
    assert _store_issues(admin_client, project_build["generic"], 2).status_code == 200

    device.refresh_from_db()
    # Two of the four cabinets, and the media players untouched: the build
    # cannot start on a parts list that is still short.
    assert device.status == Device.Status.PROCURED

    for component, quantity in (
        (project_build["generic"], 2), (project_build["unique"], 2),
    ):
        assert _ask_store(admin_client, component, quantity).status_code == 200
        r = _store_issues(admin_client, component, quantity)
        assert r.status_code == 200, r.content

    device.refresh_from_db()
    # Everything is in hand now, so the floor starts.
    assert device.status == Device.Status.IN_PRODUCTION

    note = DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.NOTE
    ).latest("created_at")
    assert "Comp Media Player" in note.description

    moved = DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE
    ).latest("created_at")
    assert moved.to_value == "in_production"


@pytest.mark.django_db
def test_flagging_for_procurement_journals_without_moving_the_asset(admin_client, project_build):
    from apps.assets.models import DeviceLifecycleEvent

    device = project_build["device"]
    r = admin_client.post(
        f"/api/assets/components/{project_build['unique'].id}/mark-for-procurement/", {}, format="json",
    )
    assert r.status_code == 200, r.content

    device.refresh_from_db()
    assert device.status == Device.Status.PROCURED, "nothing was built yet"
    note = DeviceLifecycleEvent.objects.filter(
        device=device, event_type=DeviceLifecycleEvent.EventType.NOTE
    ).latest("created_at")
    assert "flagged for procurement" in note.description


@pytest.mark.django_db
def test_an_already_building_asset_is_not_moved_backwards(admin_client, project_build):
    device = project_build["device"]
    device.status = Device.Status.IN_STOCK
    device.save(update_fields=["status"])

    r = admin_client.post(
        f"/api/assets/components/{project_build['generic'].id}/fulfil-from-stock/",
        {"quantity": 1}, format="json",
    )
    assert r.status_code == 200, r.content
    device.refresh_from_db()
    assert device.status == Device.Status.IN_STOCK, "only a procured asset starts building"


# ---------------------------------------------------------------------------
# Inventory master data can be created by the people who open stock
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_warehouse_can_name_a_new_material_type(db):
    """Opening a stock item must not be blocked by missing master data."""
    from rest_framework.test import APIClient

    wh = User.objects.create_user(username="wh-material", password="x", role="warehouse")
    c = APIClient(); c.force_authenticate(wh)
    r = c.post("/api/assets/material-types/", {"name": "WH New Material", "unit": "piece"}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["name"] == "WH New Material"


@pytest.mark.django_db
def test_a_read_only_role_still_cannot_create_material_types(db):
    from rest_framework.test import APIClient

    viewer = User.objects.create_user(username="viewer-material", password="x", role="client_viewer")
    c = APIClient(); c.force_authenticate(viewer)
    r = c.post("/api/assets/material-types/", {"name": "Nope", "unit": "piece"}, format="json")
    assert r.status_code == 403, r.content


# ---------------------------------------------------------------------------
# Production routing for in-house builds (in-house vs outside workshop)
# ---------------------------------------------------------------------------
@pytest.fixture
def inhouse_asset(db):
    return Device.objects.create(serial_number="ROUTE-SN-1", source=Device.Source.INHOUSE)


def _step(client, device, number, name, **extra):
    payload = {"device": str(device.id), "step_number": number, "name": name}
    payload.update(extra)
    return client.post("/api/assets/production-steps/", payload, format="json")


@pytest.mark.django_db
def test_lay_out_a_route_mixing_inhouse_and_workshop(admin_client, inhouse_asset, db):
    from apps.suppliers.models import Supplier

    painter = Supplier.objects.create(name="Ali Paint Works")

    a = _step(admin_client, inhouse_asset, 1, "Frame welding")
    assert a.status_code == 201, a.content
    # Where it happens is the project's call, made in Execution; until then it is open.
    assert a.data["location"] == "undecided" and a.data["location_display"] == "Not decided"

    b = _step(admin_client, inhouse_asset, 2, "Painting",
              location="external", workshop=str(painter.id))
    assert b.status_code == 201, b.content
    assert b.data["workshop_display"] == "Ali Paint Works"

    c = _step(admin_client, inhouse_asset, 3, "Panaflex pasting",
              location="external", workshop_name="Corner Signage Shop")
    assert c.status_code == 201, c.content
    assert c.data["workshop_display"] == "Corner Signage Shop"

    detail = admin_client.get(f"/api/assets/devices/{inhouse_asset.id}/").json()
    assert [s["step_number"] for s in detail["production_steps"]] == [1, 2, 3]


@pytest.mark.django_db
def test_an_external_step_must_say_where_it_goes(admin_client, inhouse_asset):
    r = _step(admin_client, inhouse_asset, 1, "Painting", location="external")
    assert r.status_code == 400, r.content
    assert "workshop" in r.data


@pytest.mark.django_db
def test_an_inhouse_step_cannot_name_a_workshop(admin_client, inhouse_asset):
    r = _step(admin_client, inhouse_asset, 1, "Welding", workshop_name="Somewhere")
    assert r.status_code == 400, r.content
    assert "workshop" in r.data


@pytest.mark.django_db
def test_step_numbers_are_unique_per_asset(admin_client, inhouse_asset):
    assert _step(admin_client, inhouse_asset, 1, "First").status_code == 201
    clash = _step(admin_client, inhouse_asset, 1, "Also first")
    assert clash.status_code == 400, clash.content
    assert "step_number" in clash.data


@pytest.mark.django_db
def test_vendor_built_assets_have_no_production_route(admin_client, db):
    vendor_asset = Device.objects.create(
        serial_number="ROUTE-VENDOR-1", source=Device.Source.VENDOR_SUPPLIED
    )
    r = _step(admin_client, vendor_asset, 1, "Should not exist")
    assert r.status_code == 400, r.content
    assert "in-house builds" in str(r.data["device"])


@pytest.mark.django_db
def test_a_step_on_a_work_order_follows_it(admin_client, inhouse_asset, db):
    """Giving an operation to a workshop is a work order: raising it marks
    the step 'Work Order Raised', completing it completes the step, and a
    cancelled order hands the decision back to the project."""
    from apps.assets.models import ProductionStep
    from apps.suppliers.models import Supplier
    from apps.workorders.models import WorkOrder

    painter = Supplier.objects.create(name="Ali Paint Works")
    step_id = _step(admin_client, inhouse_asset, 1, "Painting").data["id"]
    step = ProductionStep.objects.get(pk=step_id)
    assert step.location == "undecided"

    # Nobody moves a workshop step by hand.
    r = admin_client.post(
        f"/api/assets/production-steps/{step_id}/transition/", {"status": "sent_out"}, format="json",
    )
    assert r.status_code == 400, r.content

    order = WorkOrder.objects.create(title="Painting", supplier=painter, production_step=step, device=inhouse_asset)
    step.refresh_from_db()
    assert step.location == "external" and step.workshop == painter
    assert step.status == "sent_out" and step.sent_at is not None
    detail = admin_client.get(f"/api/assets/production-steps/{step_id}/").json()
    assert detail["allowed_transitions"] == [] and "work order" in detail["hold_reason"].lower()

    order.status = WorkOrder.Status.COMPLETED
    order.save(update_fields=["status"])
    step.refresh_from_db()
    assert step.status == "completed" and step.completed_at is not None

    # A cancelled order gives the decision back.
    other_id = _step(admin_client, inhouse_asset, 2, "Drying").data["id"]
    other = ProductionStep.objects.get(pk=other_id)
    order2 = WorkOrder.objects.create(title="Drying", supplier=painter, production_step=other, device=inhouse_asset)
    other.refresh_from_db()
    assert other.status == "sent_out"
    order2.status = WorkOrder.Status.CANCELLED
    order2.save(update_fields=["status"])
    other.refresh_from_db()
    assert other.location == "undecided" and other.status == "pending"


@pytest.mark.django_db
def test_a_step_work_order_spawns_no_installation_project(admin_client, inhouse_asset, db):
    """One operation given to a workshop is production, not an installation:
    approving its work order must not open a delivery project."""
    from apps.assets.models import ProductionStep
    from apps.suppliers.models import Supplier
    from apps.teams.models import Project
    from apps.workorders.models import WorkOrder

    painter = Supplier.objects.create(name="Ali Paint Works")
    step = ProductionStep.objects.get(pk=_step(admin_client, inhouse_asset, 1, "Painting").data["id"])
    order = WorkOrder.objects.create(
        title="Painting", supplier=painter, production_step=step, device=inhouse_asset,
        order_type=WorkOrder.OrderType.PRODUCTION,
    )
    for status in ("pending_approval", "approved"):
        r = admin_client.post(f"/api/work-orders/{order.id}/transition/", {"status": status}, format="json")
        assert r.status_code == 200, r.content
    assert not Project.objects.filter(source_work_order=order).exists()


@pytest.mark.django_db
def test_the_step_flow_is_guarded(admin_client, inhouse_asset):
    step_id = _step(admin_client, inhouse_asset, 1, "Welding").data["id"]
    # pending to returned makes no sense, it was never sent anywhere
    r = admin_client.post(
        f"/api/assets/production-steps/{step_id}/transition/", {"status": "returned"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "Cannot move" in r.data["detail"]


# ---------------------------------------------------------------------------
# Turnkey jobs carry a vendor AND an overseeing technician
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_turnkey_may_have_both_vendor_and_oversight_technician(admin_client, db):
    from apps.sites.models import Site

    asset = Device.objects.create(
        serial_number="TURNKEY-SN-1", source=Device.Source.VENDOR_TURNKEY,
        status=Device.Status.IN_STOCK,
        current_site=Site.objects.create(name="Turnkey Site"),
    )
    tech = User.objects.create_user(
        username="oversight-tech", password="x", role="technician", is_field_staff=True,
        first_name="Over", last_name="Sight",
    )
    r = admin_client.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {
            "status": "assigned", "reason": "Vendor building, we oversee",
            "assigned_technician": str(tech.id),
            "assigned_vendor_name": "Skyline Fabricators",
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["technician_name"] == "Over Sight"
    assert r.data["assigned_vendor_name"] == "Skyline Fabricators"


@pytest.mark.django_db
def test_supplying_and_installing_vendors_are_separate_records(admin_client, db):
    """One firm sells us the asset; another may put it up. On a vendor-supplied
    route our own technician does the installing, so there is no second firm."""
    from apps.sites.models import Site

    asset = Device.objects.create(
        serial_number="SUPPLIED-SN-1", source=Device.Source.VENDOR_SUPPLIED,
        status=Device.Status.IN_STOCK,
        current_site=Site.objects.create(name="Supplied Site"),
    )
    tech = User.objects.create_user(
        username="single-tech", password="x", role="technician",
        first_name="Ins", last_name="Taller",
    )

    # Who sold it to us belongs to the asset, not to the assignment.
    r = admin_client.patch(
        f"/api/assets/devices/{asset.id}/",
        {"supply_vendor_name": "Skyline Displays", "supply_vendor_contact": "+92300111"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["supply_vendor_name"] == "Skyline Displays"

    # The installation still goes to our technician.
    r = admin_client.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "assigned", "reason": "fitted by us", "assigned_technician": str(tech.id)},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["technician_name"] == "Ins Taller"
    assert r.data["supply_vendor_name"] == "Skyline Displays"

    # Naming an installing vendor on this route is a mistake, not a shortcut.
    r = admin_client.patch(
        f"/api/assets/devices/{asset.id}/", {"assigned_vendor_name": "Some Crew"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "installed by our own technician" in str(r.data["assigned_vendor_name"])


@pytest.mark.django_db
def test_inhouse_build_has_neither_vendor(admin_client, db):
    asset = Device.objects.create(
        serial_number="INHOUSE-SN-1", source=Device.Source.INHOUSE,
        status=Device.Status.IN_STOCK,
    )
    r = admin_client.patch(
        f"/api/assets/devices/{asset.id}/", {"supply_vendor_name": "Skyline"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "not bought from a vendor" in str(r.data["supply_vendor_name"])

    r = admin_client.patch(
        f"/api/assets/devices/{asset.id}/", {"assigned_vendor_name": "Some Crew"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "installed by our own technician" in str(r.data["assigned_vendor_name"])


# ---------------------------------------------------------------------------
# Warranty captured as an expiry date, term derived
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_registration_derives_the_term_from_the_expiry_date(admin_client, db):
    from dateutil.relativedelta import relativedelta
    from django.utils import timezone

    from apps.warranties.models import Warranty

    today = timezone.now().date()
    expiry = today + relativedelta(months=18)
    r = admin_client.post(
        "/api/assets/devices/",
        {"serial_number": "WARR-SN-1", "client_warranty_end": expiry.isoformat()},
        format="json",
    )
    assert r.status_code == 201, r.content
    w = Warranty.objects.get(device_id=r.data["id"])
    assert w.end_date == expiry
    assert w.months == 18, "the period is worked out from the date"


@pytest.mark.django_db
def test_a_past_expiry_is_rejected(admin_client, db):
    r = admin_client.post(
        "/api/assets/devices/",
        {"serial_number": "WARR-SN-2", "client_warranty_end": "2020-01-01"},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "client_warranty_end" in r.data


@pytest.mark.django_db
def test_registering_without_a_device_model_works(admin_client, db):
    """An asset is identified by its type, name and components."""
    r = admin_client.post(
        "/api/assets/devices/",
        {"serial_number": "NOMODEL-SN-1", "display_name": "Lobby Standee"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.data["device_model"] is None
    assert r.data["display_name"] == "Lobby Standee"
    for gone in ("mac_address", "imei", "mobile_id", "firmware_version"):
        assert gone not in r.data


# ---------------------------------------------------------------------------
# Routing master: an asset type is worked out once, then reused
# ---------------------------------------------------------------------------
@pytest.fixture
def standee_type(db):
    from apps.assets.models import AssetType

    return AssetType.objects.get_or_create(name="Route Test Standee")[0]


def _typed_asset(asset_type, serial):
    return Device.objects.create(
        serial_number=serial, source=Device.Source.INHOUSE, asset_type=asset_type
    )


@pytest.mark.django_db
def test_first_asset_of_a_type_has_no_saved_route(admin_client, standee_type):
    asset = _typed_asset(standee_type, "TPL-FIRST")
    detail = admin_client.get(f"/api/assets/devices/{asset.id}/").json()
    assert detail["route_template_available"] is False

    r = admin_client.post(f"/api/assets/devices/{asset.id}/apply-route-template/", {}, format="json")
    assert r.status_code == 404, r.content
    assert "No standard route saved for Route Test Standee" in r.data["detail"]


@pytest.mark.django_db
def test_save_a_route_then_reuse_it_on_the_next_asset(admin_client, standee_type, db):
    from apps.suppliers.models import Supplier

    painter = Supplier.objects.create(name="Ali Paint Works")
    first = _typed_asset(standee_type, "TPL-A")

    for number, name, loc, shop in (
        (1, "Frame welding", "in_house", None),
        (2, "Painting", "external", painter),
        (3, "Panaflex pasting", "external", None),
    ):
        payload = {
            "device": str(first.id), "step_number": number, "name": name, "location": loc,
        }
        if shop:
            payload["workshop"] = str(shop.id)
        elif loc == "external":
            payload["workshop_name"] = "Corner Signage Shop"
        assert admin_client.post(
            "/api/assets/production-steps/", payload, format="json"
        ).status_code == 201

    saved = admin_client.post(
        f"/api/assets/devices/{first.id}/save-route-template/", {}, format="json"
    )
    assert saved.status_code == 200, saved.content
    assert saved.data["saved_steps"] == 3
    assert saved.data["asset_type"] == "Route Test Standee"

    # The next standee starts from the known route.
    second = _typed_asset(standee_type, "TPL-B")
    detail = admin_client.get(f"/api/assets/devices/{second.id}/").json()
    assert detail["route_template_available"] is True

    applied = admin_client.post(
        f"/api/assets/devices/{second.id}/apply-route-template/", {}, format="json"
    )
    assert applied.status_code == 200, applied.content
    assert applied.data["applied"] == 3
    names = [s["name"] for s in applied.data["steps"]]
    assert names == ["Frame welding", "Painting", "Panaflex pasting"]
    # The route brings the operations, not the first asset's decisions: where
    # each one happens is decided again, per project, in Execution.
    assert {s["location"] for s in applied.data["steps"]} == {"undecided"}
    assert all(s["workshop_display"] is None for s in applied.data["steps"])
    # Copied steps start fresh, not carrying the first asset's progress.
    assert all(s["status"] == "pending" for s in applied.data["steps"])


@pytest.mark.django_db
def test_applying_a_route_twice_is_refused(admin_client, standee_type):
    first = _typed_asset(standee_type, "TPL-C")
    admin_client.post(
        "/api/assets/production-steps/",
        {"device": str(first.id), "step_number": 1, "name": "Welding"}, format="json",
    )
    admin_client.post(f"/api/assets/devices/{first.id}/save-route-template/", {}, format="json")

    second = _typed_asset(standee_type, "TPL-D")
    assert admin_client.post(
        f"/api/assets/devices/{second.id}/apply-route-template/", {}, format="json"
    ).status_code == 200
    again = admin_client.post(
        f"/api/assets/devices/{second.id}/apply-route-template/", {}, format="json"
    )
    assert again.status_code == 400, again.content
    assert "already has a route" in again.data["detail"]


@pytest.mark.django_db
def test_a_route_needs_an_asset_type_to_belong_to(admin_client, db):
    untyped = Device.objects.create(serial_number="TPL-NOTYPE", source=Device.Source.INHOUSE)
    admin_client.post(
        "/api/assets/production-steps/",
        {"device": str(untyped.id), "step_number": 1, "name": "Welding"}, format="json",
    )
    r = admin_client.post(
        f"/api/assets/devices/{untyped.id}/save-route-template/", {}, format="json"
    )
    assert r.status_code == 400, r.content
    assert "asset type" in r.data["detail"]


@pytest.mark.django_db
def test_vendor_built_assets_cannot_apply_a_route(admin_client, standee_type):
    vendor_asset = Device.objects.create(
        serial_number="TPL-VENDOR", source=Device.Source.VENDOR_SUPPLIED, asset_type=standee_type,
    )
    r = admin_client.post(
        f"/api/assets/devices/{vendor_asset.id}/apply-route-template/", {}, format="json"
    )
    assert r.status_code == 400, r.content
    assert "in-house builds" in r.data["detail"]


@pytest.mark.django_db
def test_resaving_replaces_the_old_route(admin_client, standee_type):
    first = _typed_asset(standee_type, "TPL-E")
    admin_client.post(
        "/api/assets/production-steps/",
        {"device": str(first.id), "step_number": 1, "name": "Old way"}, format="json",
    )
    admin_client.post(f"/api/assets/devices/{first.id}/save-route-template/", {}, format="json")

    revised = _typed_asset(standee_type, "TPL-F")
    for number, name in ((1, "New way"), (2, "Extra check")):
        admin_client.post(
            "/api/assets/production-steps/",
            {"device": str(revised.id), "step_number": number, "name": name}, format="json",
        )
    saved = admin_client.post(
        f"/api/assets/devices/{revised.id}/save-route-template/", {}, format="json"
    )
    assert saved.data["saved_steps"] == 2

    third = _typed_asset(standee_type, "TPL-G")
    applied = admin_client.post(
        f"/api/assets/devices/{third.id}/apply-route-template/", {}, format="json"
    )
    assert [s["name"] for s in applied.data["steps"]] == ["New way", "Extra check"]


@_pytest.mark.django_db
def test_components_rejected_on_vendor_supplied_asset():
    """A vendor-supplied asset arrives complete, so it has no BOM of ours."""
    from apps.assets.models import Device, MaterialType
    from apps.inventory.models import InventoryItem

    ops = _User.objects.create_user(username="vend-ops", password="x", role="ops_manager")
    stock = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="Vend Frame"), quantity=10
    )

    c = _APIClient()
    c.force_authenticate(ops)

    for route in (Device.Source.VENDOR_SUPPLIED, Device.Source.VENDOR_TURNKEY):
        dev = Device.objects.create(asset_code=f"AST-VEND-{route}", source=route)
        r = c.post("/api/assets/components/", {
            "device": str(dev.pk), "quantity": 2, "inventory_item": str(stock.pk),
        }, format="json")
        assert r.status_code == 400, r.content
        assert "arrive complete" in str(r.json()["device"])

        detail = c.get(f"/api/assets/devices/{dev.pk}/").json()
        assert detail["requires_production"] is False
        assert detail["components"] == []

    # The same payload is fine on an in-house build.
    inhouse = Device.objects.create(asset_code="AST-VEND-IN", source=Device.Source.INHOUSE)
    r = c.post("/api/assets/components/", {
        "device": str(inhouse.pk), "quantity": 2, "inventory_item": str(stock.pk),
    }, format="json")
    assert r.status_code == 201, r.content


# ---------------------------------------------------------------------------
# Reordering a production route, and reusing a saved parts list
# ---------------------------------------------------------------------------
@_pytest.mark.django_db
def test_production_steps_can_be_moved_up_and_down(admin_client, db):
    from apps.assets.models import AssetType, Device, ProductionStep

    asset_type = AssetType.objects.create(name="Move Standee")
    dev = Device.objects.create(asset_code="AST-MOVE-1", asset_type=asset_type)
    for i, name in enumerate(["Cut", "Weld", "Paint"], start=1):
        ProductionStep.objects.create(device=dev, step_number=i, name=name)

    third = dev.production_steps.get(name="Paint")
    r = admin_client.post(f"/api/assets/production-steps/{third.id}/move/", {"direction": "up"}, format="json")
    assert r.status_code == 200, r.content
    assert [s["name"] for s in sorted(r.data, key=lambda s: s["step_number"])] == ["Cut", "Paint", "Weld"]

    first = dev.production_steps.get(name="Cut")
    r = admin_client.post(f"/api/assets/production-steps/{first.id}/move/", {"direction": "down"}, format="json")
    assert r.status_code == 200, r.content
    assert [s["name"] for s in sorted(r.data, key=lambda s: s["step_number"])] == ["Paint", "Cut", "Weld"]

    # The ends of the route have nowhere further to go.
    top = dev.production_steps.order_by("step_number").first()
    r = admin_client.post(f"/api/assets/production-steps/{top.id}/move/", {"direction": "up"}, format="json")
    assert r.status_code == 400
    assert "already first" in r.data["detail"]

    r = admin_client.post(f"/api/assets/production-steps/{top.id}/move/", {"direction": "sideways"}, format="json")
    assert r.status_code == 400


@_pytest.mark.django_db
def test_component_set_is_saved_and_reused_per_asset_type():
    """The parts list is worked out once for a type, then reused — the BOM
    master, the sibling of the routing master."""
    from apps.assets.models import AssetType, Device, MaterialType
    from apps.inventory.models import InventoryItem

    ops = _User.objects.create_user(username="bom-ops", password="x", role="ops_manager")
    c = _APIClient()
    c.force_authenticate(ops)

    asset_type = AssetType.objects.create(name="BOM Standee")
    frame = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="BOM Frame"), quantity=10
    )
    vinyl = InventoryItem.objects.create(
        material_type=MaterialType.objects.create(name="BOM Vinyl"), quantity=40
    )

    first = Device.objects.create(asset_code="AST-BOM-1", asset_type=asset_type)
    for item, qty in ((frame, 1), (vinyl, 6)):
        r = c.post("/api/assets/components/", {
            "device": str(first.pk), "inventory_item": str(item.pk), "quantity": qty,
        }, format="json")
        assert r.status_code == 201, r.content

    r = c.post(f"/api/assets/devices/{first.pk}/save-component-template/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["saved_lines"] == 2

    # The next asset of the same type starts from it.
    second = Device.objects.create(asset_code="AST-BOM-2", asset_type=asset_type)
    detail = c.get(f"/api/assets/devices/{second.pk}/").json()
    assert detail["component_template_available"] is True

    r = c.post(f"/api/assets/devices/{second.pk}/apply-component-template/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["applied"] == 2
    assert sorted((cmp["name"], cmp["quantity"]) for cmp in r.data["components"]) == [
        ("BOM Frame", 1), ("BOM Vinyl", 6),
    ]

    # Applying twice would duplicate the build, so it is refused.
    r = c.post(f"/api/assets/devices/{second.pk}/apply-component-template/", {}, format="json")
    assert r.status_code == 400
    assert "already has components" in r.data["detail"]

    # A type with nothing saved says so rather than silently doing nothing.
    other = Device.objects.create(
        asset_code="AST-BOM-3", asset_type=AssetType.objects.create(name="BOM Other")
    )
    r = c.post(f"/api/assets/devices/{other.pk}/apply-component-template/", {}, format="json")
    assert r.status_code == 404


@_pytest.mark.django_db
def test_vendor_supplied_asset_never_enters_production(admin_client, db):
    """A vendor-supplied asset arrives complete: it is bought, not built, so
    production is neither offered nor allowed for it."""
    from apps.assets.models import Device

    asset = Device.objects.create(asset_code="AST-VP-1", source=Device.Source.VENDOR_SUPPLIED)
    detail = admin_client.get(f"/api/assets/devices/{asset.id}/").json()
    assert "in_production" not in detail["allowed_transitions"]
    assert "in_stock" in detail["allowed_transitions"]
    r = admin_client.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "in_production", "reason": "vendor building it"}, format="json",
    )
    assert r.status_code == 400, r.content

    # An in-house build with nothing on it still has to be itemised first.
    inhouse = Device.objects.create(asset_code="AST-VP-2", source=Device.Source.INHOUSE)
    r = admin_client.post(
        f"/api/assets/devices/{inhouse.id}/transition/",
        {"status": "in_production", "reason": "no parts yet"}, format="json",
    )
    assert r.status_code == 400
    assert "Components section is empty" in str(r.data["status"])


@_pytest.mark.django_db
def test_asset_returns_to_active_from_maintenance_in_the_registry(admin_client, db):
    """Coming back into service is registry work, not a site visit: the asset
    was installed long ago and there is nothing for the tracker to record."""
    from apps.assets.models import Device

    asset = Device.objects.create(
        asset_code="AST-MAINT-1", serial_number="MAINT-SN-1",
        status=Device.Status.UNDER_MAINTENANCE,
    )
    detail = admin_client.get(f"/api/assets/devices/{asset.id}/").json()
    assert "active" in detail["allowed_transitions"]
    # It was installed before it broke; the only ways out are back into
    # service, to the vendor, or out of the fleet.
    assert "installed" not in detail["allowed_transitions"]

    r = admin_client.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "active", "reason": "Repaired and back in service"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "active"

    # The two site moves stay with the tracker.
    asset.status = Device.Status.INSTALLED
    asset.save(update_fields=["status"])
    detail = admin_client.get(f"/api/assets/devices/{asset.id}/").json()
    assert "active" not in detail["allowed_transitions"]
    assert "under_maintenance" in detail["allowed_transitions"]


# ---------------------------------------------------------------------------
# The asset's own warranties: the vendor's to us, ours to the client
# ---------------------------------------------------------------------------
@_pytest.mark.django_db
def test_client_warranty_can_be_set_and_moved_from_the_edit_form(admin_client, db):
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.assets.models import Device

    expiry = _tz.now().date() + timedelta(days=365)
    r = admin_client.post("/api/assets/devices/", {
        "serial_number": "WRT-SN-1", "client_warranty_end": expiry.isoformat(),
    }, format="json")
    assert r.status_code == 201, r.content
    device_id = r.json()["id"]

    detail = admin_client.get(f"/api/assets/devices/{device_id}/").json()
    assert detail["client_warranty"]["end_date"] == expiry.isoformat()
    assert detail["client_warranty"]["months"] == 12
    assert detail["vendor_warranty"] is None

    # Editing moves the same warranty rather than stacking a second one.
    moved = expiry + timedelta(days=365)
    r = admin_client.patch(
        f"/api/assets/devices/{device_id}/",
        {"client_warranty_end": moved.isoformat()}, format="json",
    )
    assert r.status_code == 200, r.content
    device = Device.objects.get(pk=device_id)
    assert device.warranties.filter(warranty_type="client").count() == 1
    assert device.warranties.get(warranty_type="client").end_date.isoformat() == moved.isoformat()

    # Leaving it out of a later edit does not drop it.
    admin_client.patch(f"/api/assets/devices/{device_id}/", {"display_name": "Renamed"}, format="json")
    assert device.warranties.filter(warranty_type="client").count() == 1


@_pytest.mark.django_db
def test_vendor_warranty_only_applies_to_a_vendor_supplied_asset(admin_client, db):
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.assets.models import Device

    expiry = (_tz.now().date() + timedelta(days=730)).isoformat()

    supplied = Device.objects.create(
        asset_code="AST-WRT-V1", serial_number="WRT-SN-V1",
        source=Device.Source.VENDOR_SUPPLIED,
    )
    r = admin_client.patch(
        f"/api/assets/devices/{supplied.id}/", {"vendor_warranty_end": expiry}, format="json",
    )
    assert r.status_code == 200, r.content
    detail = admin_client.get(f"/api/assets/devices/{supplied.id}/").json()
    assert detail["vendor_warranty"]["end_date"] == expiry
    assert detail["vendor_warranty"]["months"] == 24

    # An in-house build has no supplying vendor, so there is nothing to record.
    inhouse = Device.objects.create(
        asset_code="AST-WRT-I1", serial_number="WRT-SN-I1", source=Device.Source.INHOUSE,
    )
    r = admin_client.patch(
        f"/api/assets/devices/{inhouse.id}/", {"vendor_warranty_end": expiry}, format="json",
    )
    assert r.status_code == 200, r.content
    assert inhouse.warranties.count() == 0


@_pytest.mark.django_db
def test_component_warranty_is_not_mistaken_for_the_asset_warranty(admin_client, db):
    """A part's own cover belongs to the part, not to what we gave the client."""
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.assets.models import AssetComponent, Device
    from apps.warranties.models import Warranty

    device = Device.objects.create(asset_code="AST-WRT-C1", serial_number="WRT-SN-C1")
    component = AssetComponent.objects.create(device=device, name="SMD Module", quantity=1)
    today = _tz.now().date()
    Warranty.objects.create(
        device=device, component=component, warranty_type="manufacturer",
        start_date=today, end_date=today + timedelta(days=365), months=12,
    )

    detail = admin_client.get(f"/api/assets/devices/{device.id}/").json()
    assert detail["client_warranty"] is None
    assert detail["vendor_warranty"] is None



@_pytest.mark.django_db
def test_raising_a_quantity_waits_for_a_manager(admin_client, db):
    """The extra spends approved money, so asking is not the same as getting."""
    from apps.assets.models import AssetComponent, Device, DeviceLifecycleEvent

    device = Device.objects.create(asset_code="AST-INC-1", serial_number="INC-1")
    component = AssetComponent.objects.create(
        device=device, name="SMD Module", quantity=4, issued_quantity=4,
        fulfilment=AssetComponent.Fulfilment.FULFILLED,
    )
    tech = User.objects.create_user(username="inc-tech", password="x", role="technician")
    tech_client = APIClient()
    tech_client.force_authenticate(tech)

    r = tech_client.post(f"/api/assets/components/{component.id}/increase-quantity/",
                         {"additional": 2, "reason": "damaged", "notes": "Two dropped on site"}, format="json")
    assert r.status_code == 200, r.content
    component.refresh_from_db()
    # Asked for, not taken: the requirement has not moved.
    assert component.quantity == 4
    assert component.pending_increase == 2
    assert component.fulfilment == AssetComponent.Fulfilment.FULFILLED
    assert r.data["increase_requested_by_name"] is not None

    # One request at a time, and the reason still has to hold up.
    again = tech_client.post(f"/api/assets/components/{component.id}/increase-quantity/",
                             {"additional": 1, "reason": "faulty"}, format="json")
    assert again.status_code == 400

    # A technician cannot grant their own request.
    denied = tech_client.post(f"/api/assets/components/{component.id}/approve-increase/", {}, format="json")
    assert denied.status_code == 403

    r = admin_client.post(f"/api/assets/components/{component.id}/approve-increase/", {}, format="json")
    assert r.status_code == 200, r.content
    component.refresh_from_db()
    assert component.quantity == 6
    assert component.outstanding_quantity == 2
    assert component.pending_increase is None
    # The extra needs its own stock-or-procure decision.
    assert component.fulfilment == AssetComponent.Fulfilment.PENDING
    note = DeviceLifecycleEvent.objects.filter(device=device, event_type="note").latest("created_at")
    assert "4 → 6" in note.description and "Damaged / manhandled" in note.description

    # A request that is turned down leaves the requirement exactly as it was.
    tech_client.post(f"/api/assets/components/{component.id}/increase-quantity/",
                     {"additional": 5, "reason": "miscalculated"}, format="json")
    r = admin_client.post(f"/api/assets/components/{component.id}/reject-increase/",
                          {"notes": "Recount first"}, format="json")
    assert r.status_code == 200, r.content
    component.refresh_from_db()
    assert component.quantity == 6 and component.pending_increase is None
    note = DeviceLifecycleEvent.objects.filter(device=device, event_type="note").latest("created_at")
    assert "turned down" in note.description and "Recount first" in note.description

    # Bad requests are still refused up front.
    r = tech_client.post(f"/api/assets/components/{component.id}/increase-quantity/",
                         {"additional": 1, "reason": "other"}, format="json")
    assert r.status_code == 400  # "other" has to say what happened
    r = tech_client.post(f"/api/assets/components/{component.id}/increase-quantity/",
                         {"additional": 1, "reason": "gremlins"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_a_copied_route_leaves_the_decision_to_the_project(admin_client, inhouse_asset, db):
    """Copying an asset brings its operations, not its decisions: where each
    one happens is decided again, per project."""
    from apps.suppliers.models import Supplier
    from apps.assets.models import ProductionStep

    painter = Supplier.objects.create(name="Copy Paint Works")
    first = _step(admin_client, inhouse_asset, 1, "Cutting").data["id"]
    second = _step(admin_client, inhouse_asset, 2, "Painting").data["id"]
    for step_id, location in ((first, "in_house"), (second, "external")):
        step = ProductionStep.objects.get(pk=step_id)
        step.location = location
        step.workshop = painter if location == "external" else None
        step.save(update_fields=["location", "workshop"])

    r = admin_client.post("/api/assets/devices/", {
        "source": "inhouse", "serial_number": "COPY-ROUTE-1", "copy_from": str(inhouse_asset.id),
    }, format="json")
    assert r.status_code == 201, r.content
    copied = ProductionStep.objects.filter(device_id=r.data["id"]).order_by("step_number")
    assert [s.name for s in copied] == ["Cutting", "Painting"]
    assert {s.location for s in copied} == {"undecided"}
    assert all(s.workshop_id is None for s in copied)
