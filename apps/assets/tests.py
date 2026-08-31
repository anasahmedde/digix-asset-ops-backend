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


# ── Asset composition (Project -> Asset -> Components) ────────────────

import pytest as _pytest
from rest_framework.test import APIClient as _APIClient

from apps.accounts.models import User as _User


@_pytest.mark.django_db
def test_components_and_project_link():
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.teams.models import Project

    ops = _User.objects.create_user(username="cmp-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="CmpBrand")
    dm = DeviceModel.objects.create(brand=brand, name="C-1")
    proj = Project.objects.create(name="Cmp Order")
    dev = Device.objects.create(device_model=dm, asset_code="AST-CMP-1", serial_number="CMP-1", project=proj)

    c = _APIClient()
    c.force_authenticate(ops)

    r = c.post("/api/assets/components/", {
        "device": str(dev.pk), "name": "SMD Cabinet P3.9",
        "component_type": "Cabinet", "quantity": 12,
    }, format="json")
    assert r.status_code == 201, r.content
    r = c.post("/api/assets/components/", {
        "device": str(dev.pk), "name": "Media Player", "quantity": 1,
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
def test_component_with_supplier_and_warranty(db):
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
    r = client.post("/api/assets/components/", {
        "device": str(device.pk),
        "name": "Receiving Card",
        "component_type": "Card",
        "quantity": 4,
        "supplier": str(supplier.pk),
        "warranty_type": "supplier",
        "warranty_months": 12,
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["supplier_name"] == "Comp Supplier"
    w = Warranty.objects.get(component_id=r.data["id"])
    assert w.device == device and w.supplier == supplier
    assert w.warranty_type == "supplier" and w.months == 12
    # anchored at the device purchase (delivery) date
    assert str(w.start_date) == "2026-08-01" and str(w.end_date) == "2027-08-01"

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
    """Registration may plant any status, but it must be journalled."""
    resp = admin_client.post("/api/assets/devices/", {
        "device_model": str(device.device_model_id),
        "serial_number": "SN-CREATE-ACTIVE",
        "status": "active",
    }, format="json")
    assert resp.status_code == 201, resp.content
    new_id = resp.json()["id"]
    assert resp.json()["status"] == "active"

    events = DeviceLifecycleEvent.objects.filter(device_id=new_id)
    assert events.count() == 1  # creation journals once, no double-fire
    event = events.get()
    assert event.event_type == "status_change"
    assert event.from_value == "" and event.to_value == "active"
    assert event.description == "Registered"

    log = AuditLog.objects.get(resource_type="device", resource_id=new_id)
    assert log.action == "create"
    assert log.detail == {"from": "", "to": "active", "reason": "Registered"}


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
def test_source_flag_default_and_filter(admin_client, device):
    detail = admin_client.get(f"/api/assets/devices/{device.id}/").json()
    assert detail["source"] == "third_party"

    resp = admin_client.post("/api/assets/devices/", {
        "device_model": str(device.device_model_id),
        "serial_number": "SN-INHOUSE-1",
        "source": "inhouse",
    }, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["source"] == "inhouse"

    rows = admin_client.get("/api/assets/devices/", {"source": "inhouse"}).json()
    results = rows.get("results", rows)
    assert [r["serial_number"] for r in results] == ["SN-INHOUSE-1"]
