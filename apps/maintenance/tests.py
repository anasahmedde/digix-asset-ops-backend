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


@pytest.fixture
def tech(db):
    return User.objects.create_user(
        username="maint-tech", password="x", role="technician", first_name="Mia", last_name="Fixer"
    )


@pytest.mark.django_db
def test_assignee_notified_on_assignment(ops, tech):
    from apps.notifications.models import Notification

    schedule = MaintenanceSchedule.objects.create(
        title="Filter clean", priority="high",
        next_due=timezone.now().date() + timedelta(days=3), assigned_to=tech,
    )
    notifs = Notification.objects.filter(recipient=tech, notification_type="maintenance_reminder")
    assert notifs.count() == 1
    assert "Filter clean" in notifs.first().message

    schedule.instructions = "touch"
    schedule.save()
    assert notifs.count() == 1  # unrelated save doesn't re-notify


@pytest.mark.django_db
def test_completed_record_advances_schedule(tech):
    from apps.assets.models import Brand, Device, DeviceModel

    brand = Brand.objects.create(name="MaintBrand2")
    dm = DeviceModel.objects.create(brand=brand, name="M-2")
    device = Device.objects.create(device_model=dm, asset_code="AST-MAINT-2", serial_number="MAINT-2")
    from apps.assets.models import AssetComponent

    comp = AssetComponent.objects.create(device=device, name="PSU", quantity=2)
    schedule = MaintenanceSchedule.objects.create(
        title="Monthly check", frequency="monthly", device=device,
        next_due=timezone.now().date(), assigned_to=tech, status="in_process",
    )
    c = _client(tech)
    r = c.post("/api/maintenance/records/", {
        "schedule": str(schedule.pk),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "notes": "Replaced PSU",
        "components_used": [str(comp.pk)],
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["component_names"] == ["PSU"]
    assert r.data["performed_by_name"] == "Mia Fixer"
    schedule.refresh_from_db()
    assert schedule.status == "active"
    assert schedule.next_due > timezone.now().date()

    # one-time schedules close out instead of rolling forward
    once = MaintenanceSchedule.objects.create(
        title="One off", frequency="one_time", next_due=timezone.now().date(),
    )
    r = c.post("/api/maintenance/records/", {
        "schedule": str(once.pk),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
    }, format="json")
    assert r.status_code == 201, r.content
    once.refresh_from_db()
    assert once.status == "completed" and once.is_active is False


@pytest.mark.django_db
def test_record_rejects_foreign_components(tech):
    from apps.assets.models import AssetComponent, Brand, Device, DeviceModel

    brand = Brand.objects.create(name="MaintBrand3")
    dm = DeviceModel.objects.create(brand=brand, name="M-3")
    d1 = Device.objects.create(device_model=dm, asset_code="AST-MAINT-3", serial_number="MAINT-3")
    d2 = Device.objects.create(device_model=dm, asset_code="AST-MAINT-4", serial_number="MAINT-4")
    foreign = AssetComponent.objects.create(device=d2, name="Frame")
    schedule = MaintenanceSchedule.objects.create(
        title="Check", frequency="monthly", device=d1, next_due=timezone.now().date(),
    )
    r = _client(tech).post("/api/maintenance/records/", {
        "schedule": str(schedule.pk),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "components_used": [str(foreign.pk)],
    }, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_schedule_supports_multiple_vendors(ops):
    from apps.suppliers.models import Supplier

    v1 = Supplier.objects.create(name="Vendor A")
    v2 = Supplier.objects.create(name="Vendor B")
    r = _client(ops).post("/api/maintenance/schedules/", {
        "title": "Deep clean",
        "next_due": str(timezone.now().date()),
        "vendors": [str(v1.pk), str(v2.pk)],
    }, format="json")
    assert r.status_code == 201, r.content
    assert sorted(r.data["vendor_names"]) == ["Vendor A", "Vendor B"]


@pytest.mark.django_db
def test_required_components_roundtrip(ops):
    r = _client(ops).post("/api/maintenance/schedules/", {
        "title": "Panel swap",
        "next_due": str(timezone.now().date()),
        "required_components": [
            {"name": "SMD Module P3.9", "quantity": 6},
            {"name": "Silicone sealant", "quantity": 2},
        ],
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["required_components"] == [
        {"name": "SMD Module P3.9", "quantity": 6},
        {"name": "Silicone sealant", "quantity": 2},
    ]
    # malformed rows rejected
    bad = _client(ops).post("/api/maintenance/schedules/", {
        "title": "Bad", "next_due": str(timezone.now().date()),
        "required_components": [{"quantity": 3}],
    }, format="json")
    assert bad.status_code == 400
