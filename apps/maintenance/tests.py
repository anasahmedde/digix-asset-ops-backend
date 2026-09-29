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
        "start_date": str(timezone.now().date()),
        "vendors": [str(v1.pk), str(v2.pk)],
    }, format="json")
    assert r.status_code == 201, r.content
    assert sorted(r.data["vendor_names"]) == ["Vendor A", "Vendor B"]


@pytest.mark.django_db
def test_required_components_roundtrip(ops):
    r = _client(ops).post("/api/maintenance/schedules/", {
        "title": "Panel swap",
        "start_date": str(timezone.now().date()),
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
        "title": "Bad", "start_date": str(timezone.now().date()),
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
        "start_date": "2030-01-01",
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


@pytest.mark.django_db
def test_a_schedule_takes_its_site_from_the_asset():
    """Where the work happens is where the asset stands.

    Asking for the site separately let a schedule claim an asset was being
    serviced somewhere it does not stand, so the asset answers instead — and
    keeps answering when the asset is moved.
    """
    from apps.assets.models import AssetType, Device
    from apps.maintenance.models import MaintenanceSchedule
    from apps.sites.models import Site

    here = Site.objects.create(name="Where It Stands", address="1 Road")
    there = Site.objects.create(name="Somewhere Else", address="2 Road")
    kind = AssetType.objects.create(name="Site Follow Kind")
    device = Device.objects.create(asset_type=kind, current_site=here)

    # The site given is ignored: the asset's own is the answer.
    schedule = MaintenanceSchedule.objects.create(
        title="Quarterly clean", device=device, site=there,
        next_due=timezone.localdate(),
    )
    assert schedule.site == here

    device.current_site = there
    device.save(update_fields=["current_site"])
    schedule.save()
    assert schedule.site == there


@pytest.mark.django_db
def test_maintenance_comes_in_two_kinds():
    """Planned ahead, or a response to a fault. There is no third."""
    from apps.maintenance.models import MaintenanceSchedule

    kinds = dict(MaintenanceSchedule.MaintenanceType.choices)
    assert set(kinds) == {"preventive", "corrective"}
    assert kinds["preventive"] == "Preventive"
    assert kinds["corrective"] == "Corrective"


@pytest.mark.django_db
def test_a_schedule_without_an_asset_keeps_the_site_it_was_given():
    """Not all maintenance is on one asset — a site round has no device."""
    from apps.maintenance.models import MaintenanceSchedule
    from apps.sites.models import Site

    site = Site.objects.create(name="Round Site", address="3 Road")
    schedule = MaintenanceSchedule.objects.create(
        title="Site walk-round", site=site, next_due=timezone.localdate(),
    )
    assert schedule.site == site


@pytest.mark.django_db
def test_an_asset_with_no_site_does_not_erase_the_one_given():
    """Deriving a site should never leave a schedule with less than it had."""
    from apps.assets.models import AssetType
    from apps.maintenance.models import MaintenanceSchedule

    kind = AssetType.objects.create(name="Homeless Kind")
    device = Device.objects.create(asset_type=kind)
    site = Site.objects.create(name="Told Site", address="4 Road")

    schedule = MaintenanceSchedule.objects.create(
        title="Bench check", device=device, site=site, next_due=timezone.localdate(),
    )
    assert schedule.site == site


@pytest.mark.django_db
def test_a_schedule_records_when_its_rounds_begin():
    """The start date stays put while the next due date moves on.

    Only the next round was recorded, so after a year of visits nothing could
    say when the arrangement began.
    """
    from datetime import date

    from apps.maintenance.models import MaintenanceSchedule

    begins = date(2026, 10, 5)
    schedule = MaintenanceSchedule.objects.create(
        title="Quarterly round", frequency=MaintenanceSchedule.Frequency.MONTHLY,
        start_date=begins, next_due=begins,
    )
    schedule.advance_after_completion(begins)
    schedule.refresh_from_db()

    assert schedule.start_date == begins, "the start date is not a moving target"
    assert schedule.next_due > begins


@pytest.mark.django_db
def test_the_next_round_is_worked_out_from_the_start_and_the_frequency():
    """Two facts decide the third, so the third is never asked for.

    A monthly round starting on the first falls due a month later. A one-time
    job has no round after it: it happens on the day it was arranged for.
    """
    from datetime import date

    from apps.maintenance.models import MaintenanceSchedule

    begins = date(2026, 11, 1)
    monthly = MaintenanceSchedule.objects.create(
        title="Monthly", start_date=begins,
        frequency=MaintenanceSchedule.Frequency.MONTHLY,
    )
    assert monthly.next_due == date(2026, 12, 1)

    weekly = MaintenanceSchedule.objects.create(
        title="Weekly", start_date=begins,
        frequency=MaintenanceSchedule.Frequency.WEEKLY,
    )
    assert weekly.next_due == date(2026, 11, 8)

    once = MaintenanceSchedule.objects.create(
        title="Once", start_date=begins,
        frequency=MaintenanceSchedule.Frequency.ONE_TIME,
    )
    assert once.next_due == begins, "a one-time job happens on its start date"

    # A date given without a start still anchors the schedule.
    from_due = MaintenanceSchedule.objects.create(title="From due", next_due=begins)
    assert from_due.start_date == begins


@pytest.fixture
def parts_job(db):
    """A job with a technician on it and a drum of cable in the store."""
    from apps.assets.models import AssetType, MaterialType
    from apps.inventory.models import InventoryItem
    from apps.maintenance.models import MaintenanceSchedule
    from apps.sites.models import Site

    site = Site.objects.create(name="Parts Site", address="1 Road")
    kind = AssetType.objects.create(name="Parts Kind")
    device = Device.objects.create(asset_type=kind, current_site=site)
    tech = User.objects.create_user(
        username="parts-tech", password="x", role="technician", is_field_staff=True,
    )
    boss = User.objects.create_user(username="parts-boss", password="x", role="supervisor")
    # The organogram is what says whose line this is to answer.
    tech.reports_to = boss
    tech.save(update_fields=["reports_to"])
    material = MaterialType.objects.create(name="Parts Cable", unit="meter")
    item = InventoryItem.objects.create(material_type=material, quantity=100)
    schedule = MaintenanceSchedule.objects.create(
        title="Cable round", device=device, assigned_to=tech,
        start_date=timezone.localdate(),
    )
    return {"schedule": schedule, "item": item, "tech": tech, "boss": boss}


def _ask(client, job, quantity=20):
    return client.post("/api/maintenance/part-requests/", {
        "schedule": str(job["schedule"].id), "item": str(job["item"].id),
        "quantity_requested": quantity,
    }, format="json")


@pytest.mark.django_db
def test_an_approved_line_becomes_a_request_on_the_stores_queue(parts_job):
    """Approving does not issue anything — it asks the store to.

    The store is the only place material leaves from, so the answer to a
    technician queues there for the quantity that was agreed.
    """
    from apps.inventory.models import IssuanceRequest

    r = _ask(_client(parts_job["tech"]), parts_job)
    assert r.status_code == 201, r.content
    assert r.data["status"] == "requested"
    assert r.data["unit"] == "meter", "a line is counted the way its stock is"

    r = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True, "quantity": 12, "note": "Half a drum is plenty"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["quantity_approved"] == 12

    issued = IssuanceRequest.objects.get(pk=r.data["issuance_request"])
    assert issued.quantity_requested == 12, "the store is asked for what was agreed"
    assert issued.source == IssuanceRequest.Source.MAINTENANCE
    assert issued.maintenance_schedule_id == parts_job["schedule"].id
    assert issued.status == IssuanceRequest.Status.PENDING, "nothing has left the store yet"


@pytest.mark.django_db
def test_a_line_cannot_be_approved_for_more_than_was_asked(parts_job):
    """Cutting a line is the supervisor's call; adding to it is not."""
    r = _ask(_client(parts_job["tech"]), parts_job, quantity=20)
    r = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True, "quantity": 25}, format="json",
    )
    assert r.status_code == 400
    assert "20 meter" in str(r.data["quantity"])


@pytest.mark.django_db
def test_a_rejected_line_asks_the_store_for_nothing(parts_job):
    from apps.inventory.models import IssuanceRequest

    before = IssuanceRequest.objects.count()
    r = _ask(_client(parts_job["tech"]), parts_job)
    r = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": False, "note": "Use what is on the van"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["status"] == "rejected" and r.data["quantity_approved"] == 0
    assert r.data["issuance_request"] is None
    assert IssuanceRequest.objects.count() == before


@pytest.mark.django_db
def test_a_technician_cannot_answer_their_own_request(parts_job):
    """Asking and approving are two people, or the approval means nothing."""
    tech = _client(parts_job["tech"])
    r = _ask(tech, parts_job)
    r = tech.post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    )
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_a_line_is_answered_once(parts_job):
    r = _ask(_client(parts_job["tech"]), parts_job)
    boss = _client(parts_job["boss"])
    line = f"/api/maintenance/part-requests/{r.data['id']}/decide/"
    assert boss.post(line, {"approve": True}, format="json").status_code == 200
    again = boss.post(line, {"approve": False}, format="json")
    assert again.status_code == 400
    assert "already approved" in str(again.data["status"])


@pytest.mark.django_db
def test_an_unanswered_line_can_be_withdrawn_but_an_answered_one_cannot(parts_job):
    from apps.maintenance.models import MaintenancePartRequest

    tech = _client(parts_job["tech"])
    r = _ask(tech, parts_job)
    line_id = r.data["id"]
    assert tech.delete(f"/api/maintenance/part-requests/{line_id}/").status_code == 204
    # Off the queue, still on the record — what was asked for and what became
    # of it is the question a job history answers.
    withdrawn = MaintenancePartRequest.objects.get(pk=line_id)
    assert withdrawn.status == MaintenancePartRequest.Status.CANCELLED

    r = _ask(tech, parts_job)
    _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    )
    assert tech.delete(f"/api/maintenance/part-requests/{r.data['id']}/").status_code == 400


@pytest.mark.django_db
def test_a_line_is_withdrawn_by_the_person_who_asked_not_by_a_colleague(parts_job):
    mate = User.objects.create_user(
        username="parts-mate", password="x", role="technician", is_field_staff=True,
    )
    r = _ask(_client(parts_job["tech"]), parts_job)
    assert _client(mate).delete(
        f"/api/maintenance/part-requests/{r.data['id']}/"
    ).status_code == 403
    assert _client(parts_job["boss"]).delete(
        f"/api/maintenance/part-requests/{r.data['id']}/"
    ).status_code == 204


@pytest.mark.django_db
def test_a_supervisor_answers_for_their_own_team_only(parts_job):
    other_boss = User.objects.create_user(
        username="parts-other-boss", password="x", role="supervisor",
    )
    r = _ask(_client(parts_job["tech"]), parts_job)
    denied = _client(other_boss).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    )
    assert denied.status_code == 403
    assert "team" in str(denied.data)

    allowed = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    )
    assert allowed.status_code == 200, allowed.content


@pytest.mark.django_db
def test_nobody_approves_their_own_request_for_parts(parts_job):
    boss = _client(parts_job["boss"])
    r = _ask(boss, parts_job)
    assert r.status_code == 201, r.content
    denied = boss.post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    )
    assert denied.status_code == 403
    assert "your own" in str(denied.data)


def _issue(job, line_id, quantity, serials=None):
    """The store hands over what was approved, so there is something to settle."""
    from apps.maintenance.models import MaintenancePartRequest

    line = MaintenancePartRequest.objects.get(pk=line_id)
    issuance = line.issuance_request
    issuance.quantity_issued = quantity
    issuance.issued_serials = serials or []
    issuance.sync_status()
    issuance.save()
    return issuance


def _approved_line(job, quantity=12):
    r = _ask(_client(job["tech"]), job, quantity=quantity)
    r = _client(job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True, "quantity": quantity}, format="json",
    )
    return r.data["id"]


@pytest.mark.django_db
def test_what_the_visit_did_not_use_goes_back_to_receiving(parts_job):
    """A technician saying a part is spare does not put it back on the shelf.

    The leftover is received the way a delivery is — a line waiting on
    inspection — and the job records what it used and what it handed back.
    """
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine
    from apps.maintenance.models import MaintenancePartRequest

    line_id = _approved_line(parts_job, quantity=12)
    _issue(parts_job, line_id, 12)

    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        "schedule": str(parts_job["schedule"].id),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "parts_settlement": [{"part_request": line_id, "used": 9}],
    }, format="json")
    assert r.status_code == 201, r.content

    line = MaintenancePartRequest.objects.get(pk=line_id)
    assert (line.quantity_used, line.quantity_returned) == (9, 3)
    assert line.visit_id == parts_job["schedule"].visits.get(status="completed").id, (
        "the line belongs to the round that used it"
    )

    receipt = GoodsReceipt.objects.get(source=GoodsReceipt.Source.MAINTENANCE_RETURN)
    assert line.return_reference == receipt.grn_number
    assert r.data["return_grn"] == receipt.grn_number, "the visit says where the rest went"
    back = receipt.lines.get()
    assert back.quantity == 3
    assert back.inspection_status == GoodsReceiptLine.Inspection.PENDING
    parts_job["item"].refresh_from_db()
    assert parts_job["item"].quantity == 100, "stock only moves when receiving passes the line"


@pytest.mark.django_db
def test_a_visit_that_used_everything_sends_nothing_back(parts_job):
    from apps.inventory.models import GoodsReceipt
    from apps.maintenance.models import MaintenancePartRequest

    line_id = _approved_line(parts_job, quantity=5)
    _issue(parts_job, line_id, 5)

    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        "schedule": str(parts_job["schedule"].id),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "parts_settlement": [{"part_request": line_id, "used": 5}],
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["return_grn"] is None
    assert not GoodsReceipt.objects.filter(source=GoodsReceipt.Source.MAINTENANCE_RETURN).exists()
    assert MaintenancePartRequest.objects.get(pk=line_id).quantity_returned == 0


@pytest.mark.django_db
def test_a_visit_cannot_use_more_than_the_store_issued(parts_job):
    """And the visit is not recorded on a settlement that does not add up."""
    from apps.maintenance.models import MaintenanceRecord

    line_id = _approved_line(parts_job, quantity=4)
    _issue(parts_job, line_id, 4)

    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        "schedule": str(parts_job["schedule"].id),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "parts_settlement": [{"part_request": line_id, "used": 6}],
    }, format="json")
    assert r.status_code == 400, r.content
    assert not MaintenanceRecord.objects.exists(), "the visit rolls back with its settlement"


@pytest.mark.django_db
def test_unique_units_come_back_by_serial(parts_job):
    """Which units are back matters: stock counts them one by one."""
    from apps.assets.models import MaterialType
    from apps.inventory.models import GoodsReceipt, InventoryUnitType

    kind = InventoryUnitType.objects.create(
        material_type=MaterialType.objects.create(name="Parts Meter", unit="piece"),
        name="Flow meter",
    )
    r = _client(parts_job["tech"]).post("/api/maintenance/part-requests/", {
        "schedule": str(parts_job["schedule"].id), "unit_type": str(kind.id),
        "quantity_requested": 2,
    }, format="json")
    assert r.status_code == 201, r.content
    line_id = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{r.data['id']}/decide/",
        {"approve": True}, format="json",
    ).data["id"]
    _issue(parts_job, line_id, 2, serials=["FM-1", "FM-2"])

    body = {
        "schedule": str(parts_job["schedule"].id),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
    }
    # One is spare, but which one?
    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        **body, "parts_settlement": [{"part_request": line_id, "used": 1}],
    }, format="json")
    assert r.status_code == 400, r.content

    # And it has to be one that went out on this job.
    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        **body,
        "parts_settlement": [{"part_request": line_id, "used": 1, "serials": ["FM-9"]}],
    }, format="json")
    assert r.status_code == 400, r.content

    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        **body,
        "parts_settlement": [{"part_request": line_id, "used": 1, "serials": ["FM-2"]}],
    }, format="json")
    assert r.status_code == 201, r.content
    back = GoodsReceipt.objects.get(source=GoodsReceipt.Source.MAINTENANCE_RETURN).lines.get()
    assert back.serial_numbers == ["FM-2"]
    assert back.inspection_notes == f"unit_type:{kind.id}", "receiving knows what it is"


@pytest.mark.django_db
def test_a_request_sent_back_by_the_store_waits_on_the_supervisor_again(parts_job):
    """The job does not sit reading "awaiting issue" for something the store
    has handed back. The line returns to the supervisor, whose approval is
    what puts it in front of the store in the first place.
    """
    from apps.accounts.models import User
    from apps.inventory.models import IssuanceRequest
    from apps.maintenance.models import MaintenancePartRequest

    store = User.objects.create_user(username="parts-store", password="x", role="warehouse")
    line_id = _approved_line(parts_job, quantity=6)
    line = MaintenancePartRequest.objects.get(pk=line_id)
    issuance = line.issuance_request

    r = _client(store).post(
        f"/api/inventory/issuance-requests/{issuance.id}/send-back/",
        {"note": "Out of stock — decide again"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["sent_back_to"] == parts_job["schedule"].title

    line.refresh_from_db()
    issuance.refresh_from_db()
    assert issuance.status == IssuanceRequest.Status.CANCELLED
    assert line.status == MaintenancePartRequest.Status.REQUESTED, "back with the supervisor"
    assert line.quantity_approved is None and line.issuance_request_id is None
    assert "Out of stock" in line.decision_note

    # And the supervisor can answer it again, which asks the store afresh.
    r = _client(parts_job["boss"]).post(
        f"/api/maintenance/part-requests/{line_id}/decide/",
        {"approve": True, "quantity": 4}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["quantity_approved"] == 4
    assert r.data["issuance_request"] != str(issuance.id), "a fresh request, not the cancelled one"


@pytest.mark.django_db
def test_a_jobs_parts_are_collected_by_whoever_is_on_the_job(parts_job):
    """Parts and the work they are for stay with the same person.

    Handing them to anyone else leaves the job's record saying something
    untrue about who holds what.
    """
    from apps.accounts.models import User
    from apps.inventory.models import IssuanceRequest

    store = User.objects.create_user(username="collect-store", password="x", role="warehouse")
    line_id = _approved_line(parts_job, quantity=3)
    issuance = IssuanceRequest.objects.get(maintenance_part_request=line_id)
    tech = parts_job["tech"].get_full_name() or parts_job["tech"].username

    r = _client(store).post(f"/api/inventory/issuance-requests/{issuance.id}/issue/",
                            {"quantity": 1, "received_by": "Someone off the street"}, format="json")
    assert r.status_code == 400, r.content
    assert tech in str(r.data["received_by"])

    r = _client(store).post(f"/api/inventory/issuance-requests/{issuance.id}/issue/",
                            {"quantity": 1, "received_by": tech}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["request"]["received_by"] == tech

    # Left blank, the store does not have to type it: the job already says.
    r = _client(store).post(f"/api/inventory/issuance-requests/{issuance.id}/issue/",
                            {"quantity": 1}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["request"]["received_by"] == tech
    assert r.data["request"]["maintenance_assignee"] == tech


@pytest.mark.django_db
def test_every_round_is_planned_on_its_own(parts_job):
    """A recurring schedule is an arrangement, not one person's job.

    Each round is its own row: due on its own date, attended by whoever is
    free, and closing one opens the next.
    """
    from apps.accounts.models import User
    from apps.maintenance.models import MaintenanceVisit

    schedule = parts_job["schedule"]
    visit = schedule.visits.get()
    assert visit.status == MaintenanceVisit.Status.PLANNED
    assert visit.due_date == schedule.next_due
    assert visit.assigned_to_id == schedule.assigned_to_id, "the standing technician, to start with"

    # This round goes to somebody else, and the schedule's default is untouched.
    stand_in = User.objects.create_user(
        username="stand-in", password="x", role="technician", is_field_staff=True,
    )
    r = _client(parts_job["boss"]).patch(
        f"/api/maintenance/visits/{visit.id}/",
        {"assigned_to": str(stand_in.id), "due_date": str(schedule.next_due + timedelta(days=2))},
        format="json",
    )
    assert r.status_code == 200, r.content
    visit.refresh_from_db()
    schedule.refresh_from_db()
    assert visit.assigned_to_id == stand_in.id
    assert schedule.assigned_to_id == parts_job["tech"].id, "the arrangement keeps its own technician"
    assert schedule.next_due == visit.due_date, "the open round is when it is next due"

    # A technician cannot hand their own round to somebody else.
    r = _client(parts_job["tech"]).patch(
        f"/api/maintenance/visits/{visit.id}/", {"assigned_to": str(parts_job["tech"].id)}, format="json",
    )
    assert r.status_code == 403, r.content

    # Starting says so on the round and on the schedule.
    r = _client(parts_job["tech"]).post(f"/api/maintenance/visits/{visit.id}/start/", {}, format="json")
    assert r.status_code == 200, r.content
    visit.refresh_from_db(); schedule.refresh_from_db()
    assert visit.status == MaintenanceVisit.Status.IN_PROGRESS and visit.started_at is not None
    assert schedule.status == MaintenanceSchedule.Status.IN_PROCESS
    r = _client(parts_job["tech"]).post(f"/api/maintenance/visits/{visit.id}/start/", {}, format="json")
    assert r.status_code == 400, "a round already under way cannot start again"

    # Closing it out records the round and opens the next one.
    r = _client(parts_job["tech"]).post("/api/maintenance/records/", {
        "schedule": str(schedule.id),
        "performed_at": timezone.now().isoformat(),
        "status": "completed",
        "notes": "Round one done.",
    }, format="json")
    assert r.status_code == 201, r.content
    visit.refresh_from_db(); schedule.refresh_from_db()
    assert visit.status == MaintenanceVisit.Status.COMPLETED
    assert str(visit.record_id) == r.data["id"]

    nxt = schedule.visits.exclude(pk=visit.pk).get()
    assert nxt.status == MaintenanceVisit.Status.PLANNED
    assert nxt.due_date == schedule.next_due > visit.due_date
    assert nxt.assigned_to_id == schedule.assigned_to_id, "back to the standing technician"


@pytest.mark.django_db
def test_a_part_is_asked_for_on_the_round_it_is_needed_for(parts_job):
    schedule = parts_job["schedule"]
    r = _ask(_client(parts_job["tech"]), parts_job, quantity=2)
    assert r.status_code == 201, r.content
    assert str(r.data["visit"]) == str(schedule.open_visit().id)


@pytest.mark.django_db
def test_a_ticket_raised_against_an_asset_shows_up_as_a_corrective_job():
    """A fault reported is work to be done, and work lives in the register.

    Tickets take the complaint; the repair is planned, parted and recorded in
    maintenance, so raising one opens the job there and closing it files the
    job's completion record.
    """
    from apps.assets.models import AssetType
    from apps.maintenance.models import MaintenanceSchedule
    from apps.tickets.models import Ticket

    boss = User.objects.create_user(username="ticket-boss", password="x", role="ops_manager")
    site = Site.objects.create(name="Ticket Site", address="2 Road")
    device = Device.objects.create(
        asset_type=AssetType.objects.create(name="Ticket Kind"), current_site=site,
        status=Device.Status.ACTIVE,
    )

    r = _client(boss).post("/api/tickets/", {
        "title": "Screen flickering", "description": "Flickers on the hour.",
        "device": str(device.id), "priority": "high", "category": "repair",
    }, format="json")
    assert r.status_code == 201, r.content
    ticket = Ticket.objects.get(pk=r.data["id"])

    job = MaintenanceSchedule.objects.get(ticket=ticket)
    assert job.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE
    assert job.device_id == device.id and job.site_id == site.id
    assert job.title == "Screen flickering"
    assert job.frequency == MaintenanceSchedule.Frequency.ONE_TIME
    assert job.visits.count() == 1, "and it has a round to plan, like any other job"

    # A second ticket on the same asset joins the outage rather than doubling it.
    r2 = _client(boss).post("/api/tickets/", {
        "title": "Screen still flickering", "device": str(device.id), "category": "repair",
    }, format="json")
    assert r2.status_code == 201, r2.content
    assert MaintenanceSchedule.objects.filter(device=device).count() == 1

    device.refresh_from_db()
    assert device.status == Device.Status.UNDER_MAINTENANCE, (
        "an asset with a fault open against it is not in service"
    )

    # Closing the ticket closes the job it raised, and the asset comes back.
    ticket.status = Ticket.Status.CLOSED
    ticket.save(update_fields=["status"])
    job.refresh_from_db()
    device.refresh_from_db()
    assert job.status == MaintenanceSchedule.Status.COMPLETED
    assert job.records.count() == 1
    assert device.status == Device.Status.ACTIVE



@pytest.mark.django_db
def test_a_ticket_over_several_assets_opens_a_job_for_each():
    """Each asset on a ticket is its own repair.

    One complaint can cover two standees, but they are attended, parted and
    closed out separately — so each gets its own job, and closing the ticket
    closes them all.
    """
    from apps.assets.models import AssetType
    from apps.maintenance.models import MaintenanceSchedule
    from apps.tickets.models import Ticket

    boss = User.objects.create_user(username="multi-boss", password="x", role="ops_manager")
    site = Site.objects.create(name="Multi Site", address="3 Road")
    kind = AssetType.objects.create(name="Multi Kind")
    first = Device.objects.create(asset_type=kind, current_site=site, status=Device.Status.ACTIVE)
    second = Device.objects.create(asset_type=kind, current_site=site, status=Device.Status.ACTIVE)

    r = _client(boss).post("/api/tickets/", {
        "title": "Both standees dark", "device": str(first.id),
        "devices": [str(first.id), str(second.id)], "category": "repair",
    }, format="json")
    assert r.status_code == 201, r.content
    ticket = Ticket.objects.get(pk=r.data["id"])

    jobs = MaintenanceSchedule.objects.filter(ticket=ticket)
    assert jobs.count() == 2
    assert {j.device_id for j in jobs} == {first.id, second.id}
    assert all(j.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE for j in jobs)
    first.refresh_from_db(); second.refresh_from_db()
    assert first.status == second.status == Device.Status.UNDER_MAINTENANCE

    # An asset added later joins with its own job too.
    third = Device.objects.create(asset_type=kind, current_site=site, status=Device.Status.ACTIVE)
    ticket.devices.add(third)
    assert MaintenanceSchedule.objects.filter(ticket=ticket, device=third).exists()

    ticket.status = Ticket.Status.CLOSED
    ticket.save(update_fields=["status"])
    assert not MaintenanceSchedule.objects.filter(ticket=ticket).exclude(
        status=MaintenanceSchedule.Status.COMPLETED
    ).exists(), "every job the ticket raised is closed with it"
    for d in (first, second, third):
        d.refresh_from_db()
        assert d.status == Device.Status.ACTIVE
