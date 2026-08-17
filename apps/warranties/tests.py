# Tests will be added alongside model implementations.
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Brand, Device, DeviceModel
from apps.warranties.models import Warranty
from apps.warranties.tasks import complete_expired_warranties


@pytest.fixture
def ops(db):
    return User.objects.create_user(username="war-ops", password="x", role="ops_manager")


@pytest.fixture
def device(db):
    brand = Brand.objects.create(name="WarBrand")
    dm = DeviceModel.objects.create(brand=brand, name="W-1")
    return Device.objects.create(device_model=dm, asset_code="AST-WAR-1", serial_number="WAR-1")


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_client_warranty_created_with_device(ops):
    brand = Brand.objects.create(name="WarBrand2")
    dm = DeviceModel.objects.create(brand=brand, name="W-2")
    c = _client(ops)
    r = c.post("/api/assets/devices/", {
        "serial_number": "WAR-NEW-1",
        "asset_code": "AST-WAR-NEW-1",
        "device_model": str(dm.pk),
        "client_warranty_months": 6,
    }, format="json")
    assert r.status_code == 201, r.content
    w = Warranty.objects.get(device__serial_number="WAR-NEW-1")
    assert w.warranty_type == "client"
    assert w.months == 6
    assert (w.end_date - w.start_date).days >= 180


@pytest.mark.django_db
def test_auto_complete_after_term(device):
    w = Warranty.objects.create(
        device=device, warranty_type="client", status="active", months=3,
        start_date=timezone.now().date() - timedelta(days=120),
        end_date=timezone.now().date() - timedelta(days=1),
    )
    assert complete_expired_warranties() == 1
    w.refresh_from_db()
    assert w.status == "expired"
    assert w.get_status_display() == "Warranty Completed"
    # one-shot
    assert complete_expired_warranties() == 0


@pytest.mark.django_db
def test_reissue_flow(ops, device):
    w = Warranty.objects.create(
        device=device, warranty_type="client", status="expired", months=3,
        start_date=timezone.now().date() - timedelta(days=120),
        end_date=timezone.now().date() - timedelta(days=1),
    )
    c = _client(ops)
    r = c.post(f"/api/warranties/{w.pk}/reissue/", {"months": 12}, format="json")
    assert r.status_code == 201, r.content
    w.refresh_from_db()
    assert w.status == "reissued"
    new = Warranty.objects.get(reissued_from=w)
    assert new.warranty_type == "client" and new.months == 12 and new.status == "active"

    # invalid term rejected
    w2 = Warranty.objects.create(
        device=device, warranty_type="client", status="expired",
        start_date=timezone.now().date() - timedelta(days=60),
        end_date=timezone.now().date() - timedelta(days=2),
    )
    r = c.post(f"/api/warranties/{w2.pk}/reissue/", {"months": 5}, format="json")
    assert r.status_code == 400
