# Tests will be added alongside model implementations.
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Brand, Device, DeviceModel
from apps.maintenance.models import MaintenanceSchedule
from apps.sites.models import Site


@pytest.fixture
def ops(db):
    return User.objects.create_user(username="maint-ops", password="x", role="ops_manager")


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_map_data_includes_target_device(ops):
    site = Site.objects.create(name="Maint Site", city="Lahore", latitude=31.5, longitude=74.3)
    brand = Brand.objects.create(name="MaintBrand")
    dm = DeviceModel.objects.create(brand=brand, name="M-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-MAINT-1", serial_number="MAINT-1")
    targeted = MaintenanceSchedule.objects.create(
        title="Panel clean", site=site, device=device, next_due=timezone.now().date() + timedelta(days=7)
    )
    site_wide = MaintenanceSchedule.objects.create(
        title="Site sweep", site=site, next_due=timezone.now().date() + timedelta(days=7)
    )
    r = _client(ops).get("/api/maintenance/schedules/map_data/")
    assert r.status_code == 200, r.content
    by_id = {str(row["id"]): row for row in r.data}
    assert by_id[str(targeted.id)]["device"] == device.id
    assert by_id[str(site_wide.id)]["device"] is None
