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
