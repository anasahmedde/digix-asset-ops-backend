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


@pytest.mark.django_db
def test_record_billability_defaults_from_warranty(ops):
    from datetime import timedelta as td

    from apps.warranties.models import Warranty

    brand = Brand.objects.create(name="MB-Brand")
    dm = DeviceModel.objects.create(brand=brand, name="MB-1")
    covered = Device.objects.create(device_model=dm, asset_code="AST-MB-1", serial_number="MB-1")
    uncovered = Device.objects.create(device_model=dm, asset_code="AST-MB-2", serial_number="MB-2")
    today = timezone.localdate()
    Warranty.objects.create(
        device=covered, warranty_type="client", status="active",
        start_date=today, end_date=today + td(days=365), months=12,
    )
    c = _client(ops)

    def make_record(device):
        schedule = MaintenanceSchedule.objects.create(
            title=f"PM {device.asset_code}", maintenance_type="preventive",
            frequency="monthly", device=device, next_due=today,
        )
        r = c.post("/api/maintenance/records/", {
            "schedule": str(schedule.pk),
            "performed_at": timezone.now().isoformat(),
            "status": "completed",
        }, format="json")
        assert r.status_code == 201, r.content
        return r.json()

    under = make_record(covered)
    assert under["is_billable"] is False and under["charge_to"] == "company"
    out = make_record(uncovered)
    assert out["is_billable"] is True and out["charge_to"] == "client"

    # Explicit values override the derivation.
    schedule = MaintenanceSchedule.objects.create(
        title="PM override", maintenance_type="preventive",
        frequency="monthly", device=covered, next_due=today,
    )
    r = c.post("/api/maintenance/records/", {
        "schedule": str(schedule.pk), "performed_at": timezone.now().isoformat(),
        "status": "completed", "is_billable": True, "charge_to": "client",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["is_billable"] is True and r.json()["charge_to"] == "client"


# ── Wave 4: maintenance due alerts (MW alerts) ────────────────────────

@pytest.mark.django_db
def test_due_alert_task_creates_alert_and_reminder_once(tech):
    from apps.analytics.models import Alert
    from apps.maintenance.tasks import generate_maintenance_due_alerts
    from apps.notifications.models import Notification

    today = timezone.localdate()
    brand = Brand.objects.create(name="DueBrand")
    dm = DeviceModel.objects.create(brand=brand, name="DUE-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-DUE-1", serial_number="DUE-1")
    schedule = MaintenanceSchedule.objects.create(
        title="Quarterly service", device=device,
        next_due=today + timedelta(days=5), assigned_to=tech,
    )
    # out of window / inactive / completed schedules are all ignored
    MaintenanceSchedule.objects.create(title="Far future", next_due=today + timedelta(days=30))
    MaintenanceSchedule.objects.create(title="Switched off", next_due=today, is_active=False)
    MaintenanceSchedule.objects.create(title="Already done", next_due=today, status="completed")

    generate_maintenance_due_alerts()

    alerts = Alert.objects.filter(category="maintenance_due")
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.severity == "warning"
    assert alert.device_id == device.id
    assert "Quarterly service" in alert.title
    assert str(schedule.next_due) in alert.message

    reminders = Notification.objects.filter(
        recipient=tech,
        notification_type="maintenance_reminder",
        title__startswith="Maintenance due",
    )
    assert reminders.count() == 1
    assert str(schedule.next_due) in reminders.get().message

    # rerun within the same cycle: unread alert + same-cycle reminder dedupe
    generate_maintenance_due_alerts()
    assert Alert.objects.filter(category="maintenance_due").count() == 1
    assert reminders.count() == 1

    # once the alert is read, the next sweep may raise a fresh one
    Alert.objects.filter(category="maintenance_due").update(is_read=True)
    generate_maintenance_due_alerts()
    assert Alert.objects.filter(category="maintenance_due").count() == 2
    assert reminders.count() == 1  # reminder still deduped for this cycle


@pytest.mark.django_db
def test_due_alert_site_only_schedule_dedupes_by_message(db):
    from apps.analytics.models import Alert
    from apps.maintenance.tasks import generate_maintenance_due_alerts

    site = Site.objects.create(name="Sweep Site", city="Lahore")
    MaintenanceSchedule.objects.create(
        title="Site sweep", site=site, next_due=timezone.localdate() + timedelta(days=2),
    )

    generate_maintenance_due_alerts()
    generate_maintenance_due_alerts()

    alerts = Alert.objects.filter(category="maintenance_due")
    assert alerts.count() == 1
    alert = alerts.get()
    assert alert.device_id is None
    assert alert.site_id == site.id
    assert "Site sweep" in alert.message


# ---------------------------------------------------------------------------
# Corrective loop: out of service raises a job, back in service closes it
# ---------------------------------------------------------------------------
import pytest as _pytest
from rest_framework.test import APIClient as _APIClient

from apps.accounts.models import User as _User
from apps.assets.models import Device as _Device
from apps.maintenance.models import (
    MaintenanceRecord as _Record,
    MaintenanceSchedule as _Schedule,
)


@_pytest.fixture
def corrective_client(db):
    ops = _User.objects.create_user(username="corr-ops", password="x", role="ops_manager")
    c = _APIClient()
    c.force_authenticate(ops)
    return c, ops


@_pytest.mark.django_db
def test_taking_an_asset_out_of_service_raises_a_corrective_job(corrective_client):
    c, ops = corrective_client
    asset = _Device.objects.create(
        asset_code="AST-CM-1", serial_number="CM-SN-1", status=_Device.Status.ACTIVE,
    )
    assert _Schedule.objects.filter(device=asset).count() == 0

    r = c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "under_maintenance", "reason": "Screen flickering intermittently", **_down()},
        format="json",
    )
    assert r.status_code == 200, r.content

    job = _Schedule.objects.get(device=asset)
    assert job.maintenance_type == _Schedule.MaintenanceType.CORRECTIVE
    assert job.frequency == _Schedule.Frequency.ONE_TIME
    assert job.status == _Schedule.Status.IN_PROCESS
    assert job.title == "Screen flickering intermittently"
    assert job.priority == _Schedule.Priority.HIGH

    # It shows up where the maintenance section and the asset's service
    # history both look for it.
    listed = c.get("/api/maintenance/schedules/", {"device": str(asset.id)}).json()
    assert (listed.get("results") or listed)[0]["id"] == str(job.id)


@_pytest.mark.django_db
def test_returning_to_service_closes_the_job_with_a_record(corrective_client):
    c, ops = corrective_client
    asset = _Device.objects.create(
        asset_code="AST-CM-2", serial_number="CM-SN-2", status=_Device.Status.ACTIVE,
    )
    c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "under_maintenance", "reason": "Power supply replaced", **_down()}, format="json",
    )
    job = _Schedule.objects.get(device=asset)

    r = c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "active", "reason": "PSU swapped, tested, back in service"}, format="json",
    )
    assert r.status_code == 200, r.content

    job.refresh_from_db()
    assert job.status == _Schedule.Status.COMPLETED
    assert job.is_active is False

    record = _Record.objects.get(schedule=job)
    assert record.status == _Record.Status.COMPLETED
    assert record.performed_by_id == ops.id
    assert record.notes == "PSU swapped, tested, back in service"
    # No warranty on file, so the visit is billable to the client.
    assert record.is_billable is True
    assert record.charge_to == "client"


@_pytest.mark.django_db
def test_one_outage_raises_one_job(corrective_client):
    c, _ = corrective_client
    asset = _Device.objects.create(
        asset_code="AST-CM-3", serial_number="CM-SN-3", status=_Device.Status.ACTIVE,
    )
    c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "under_maintenance", "reason": "Dead pixels", **_down()}, format="json",
    )
    # Out to installed and straight back out again — the first job is still
    # open on the way in, so it must not be duplicated.
    asset.refresh_from_db()
    asset.status = _Device.Status.UNDER_MAINTENANCE
    asset._previous_status = _Device.Status.ACTIVE
    asset.save(update_fields=["status"])
    assert _Schedule.objects.filter(device=asset).count() == 1


@_pytest.mark.django_db
def test_leaving_maintenance_for_rma_also_closes_the_job(corrective_client):
    c, _ = corrective_client
    asset = _Device.objects.create(
        asset_code="AST-CM-4", serial_number="CM-SN-4", status=_Device.Status.ACTIVE,
    )
    c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "under_maintenance", "reason": "Controller fault", **_down()}, format="json",
    )
    r = c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "rma", "reason": "Beyond on-site repair"}, format="json",
    )
    assert r.status_code == 200, r.content
    job = _Schedule.objects.get(device=asset)
    assert job.status == _Schedule.Status.COMPLETED
    assert _Record.objects.filter(schedule=job).count() == 1



def _down():
    """What taking an asset out of service now has to say about the job."""
    from datetime import timedelta

    from django.utils import timezone as _tz

    tech, _ = _User.objects.get_or_create(
        username="down-tech", defaults={"role": "technician", "first_name": "Down", "last_name": "Tech"}
    )
    return {
        "maintenance_due": (_tz.localdate() + timedelta(days=5)).isoformat(),
        "maintenance_assigned_to": str(tech.id),
    }


@_pytest.mark.django_db
def test_going_down_requires_the_corrective_job_details(corrective_client):
    c, _ = corrective_client
    asset = _Device.objects.create(asset_code="AST-CM-D1", serial_number="CM-D1", status=_Device.Status.ACTIVE)
    r = c.post(
        f"/api/assets/devices/{asset.id}/transition/",
        {"status": "under_maintenance", "reason": "No picture"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "maintenance_due" in r.data and "maintenance_assigned_to" in r.data


@_pytest.mark.django_db
def test_the_job_carries_what_the_asset_dialog_asked_for(corrective_client):
    from datetime import timedelta

    from django.utils import timezone as _tz

    c, _ = corrective_client
    tech = _User.objects.create_user(username="cm-fixer", password="x", role="technician")
    asset = _Device.objects.create(asset_code="AST-CM-D2", serial_number="CM-D2", status=_Device.Status.ACTIVE)
    due = _tz.localdate() + timedelta(days=3)
    r = c.post(f"/api/assets/devices/{asset.id}/transition/", {
        "status": "under_maintenance", "reason": "Half the panel is dark",
        "maintenance_due": due.isoformat(), "maintenance_assigned_to": str(tech.id),
        "maintenance_priority": "medium", "maintenance_instructions": "Bring two spare receiving cards",
    }, format="json")
    assert r.status_code == 200, r.content

    job = _Schedule.objects.get(device=asset)
    assert job.title == "Half the panel is dark"
    assert job.next_due == due
    assert job.assigned_to_id == tech.id
    assert job.priority == "medium"
    assert job.instructions == "Bring two spare receiving cards"


@_pytest.mark.django_db
def test_completing_the_job_puts_the_asset_back_in_service(corrective_client):
    from django.utils import timezone as _tz

    c, _ = corrective_client
    asset = _Device.objects.create(asset_code="AST-CM-D3", serial_number="CM-D3", status=_Device.Status.ACTIVE)
    c.post(f"/api/assets/devices/{asset.id}/transition/",
           {"status": "under_maintenance", "reason": "Flicker", **_down()}, format="json")
    job = _Schedule.objects.get(device=asset)

    # The technician closes it from the maintenance register.
    tech = job.assigned_to
    t = _APIClient()
    t.force_authenticate(tech)
    r = t.post("/api/maintenance/records/", {
        "schedule": str(job.id), "performed_at": _tz.now().isoformat(),
        "status": "completed", "notes": "Replaced the receiving card",
    }, format="json")
    assert r.status_code == 201, r.content

    asset.refresh_from_db()
    assert asset.status == _Device.Status.ACTIVE
    job.refresh_from_db()
    assert job.status == _Schedule.Status.COMPLETED
    # One completion record — the return to service does not file a second.
    assert _Record.objects.filter(schedule=job).count() == 1
    event = asset.lifecycle_events.order_by("-created_at").first()
    assert event.to_value == "active"
    assert "Back in service" in event.description


@_pytest.mark.django_db
def test_schedule_materials_are_picked_from_inventory(corrective_client):
    from apps.assets.models import MaterialType
    from apps.inventory.models import InventoryItem

    c, _ = corrective_client
    item = InventoryItem.objects.create(material_type=MaterialType.objects.create(name="PM Sealant"), quantity=9)
    r = c.post("/api/maintenance/schedules/", {
        "title": "Quarterly visit", "maintenance_type": "preventive", "frequency": "quarterly",
        "next_due": "2030-01-01",
        "required_components": [{"inventory_item": str(item.id), "quantity": 2}],
    }, format="json")
    assert r.status_code == 201, r.content
    row = r.data["required_components"][0]
    assert row["inventory_item"] == str(item.id)
    assert row["name"] == "PM Sealant"
    assert row["quantity"] == 2



@_pytest.mark.django_db
def test_a_closed_job_cannot_be_reopened_from_a_stale_edit(corrective_client):
    c, _ = corrective_client
    asset = _Device.objects.create(asset_code="AST-CM-R1", serial_number="CM-R1", status=_Device.Status.ACTIVE)
    c.post(f"/api/assets/devices/{asset.id}/transition/",
           {"status": "under_maintenance", "reason": "Stuck pixels", **_down()}, format="json")
    c.post(f"/api/assets/devices/{asset.id}/transition/",
           {"status": "active", "reason": "Pixels reseated"}, format="json")
    job = _Schedule.objects.get(device=asset)
    assert job.status == _Schedule.Status.COMPLETED

    # A copy of the form opened before the job closed still says "in process".
    r = c.patch(f"/api/maintenance/schedules/{job.id}/", {"status": "in_process"}, format="json")
    assert r.status_code == 400, r.content
    job.refresh_from_db()
    assert job.status == _Schedule.Status.COMPLETED

    # Editing other details without touching the status is still allowed.
    r = c.patch(f"/api/maintenance/schedules/{job.id}/", {"instructions": "Filed"}, format="json")
    assert r.status_code == 200, r.content



@_pytest.mark.django_db
def test_an_open_job_cannot_be_deleted_while_the_asset_is_out_of_service(corrective_client):
    """Deleting it would strand the asset: out of service, nothing tracking it."""
    from django.utils import timezone as _tz

    c, _ = corrective_client
    asset = _Device.objects.create(asset_code="AST-CM-D9", serial_number="CM-D9", status=_Device.Status.ACTIVE)
    c.post(f"/api/assets/devices/{asset.id}/transition/",
           {"status": "under_maintenance", "reason": "Blank screen", **_down()}, format="json")
    job = _Schedule.objects.get(device=asset)

    r = c.delete(f"/api/maintenance/schedules/{job.id}/")
    assert r.status_code == 400, r.content
    assert "complete it instead" in str(r.data)
    assert _Schedule.objects.filter(pk=job.pk).exists()

    # Completing it returns the asset to service, and then it can be removed.
    tech = job.assigned_to
    t = _APIClient()
    t.force_authenticate(tech)
    t.post("/api/maintenance/records/", {
        "schedule": str(job.id), "performed_at": _tz.now().isoformat(),
        "status": "completed", "notes": "Board swapped",
    }, format="json")
    asset.refresh_from_db()
    assert asset.status == _Device.Status.ACTIVE

    r = c.delete(f"/api/maintenance/schedules/{job.id}/")
    assert r.status_code == 204, r.content
