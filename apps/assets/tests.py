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
