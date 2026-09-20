# Tests will be added alongside feature development.
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Brand, Device, DeviceModel
from apps.clients.models import Client
from apps.sites.models import DeviceInstallation, InstallationStep, Site
from apps.sites.tasks import escalate_overdue_installations


@pytest.fixture
def ops(db):
    return User.objects.create_user(username="site-ops", password="x", role="ops_manager")


@pytest.fixture
def tech(db):
    return User.objects.create_user(
        username="site-tech", password="x", role="technician", first_name="Tariq", last_name="Installer"
    )


@pytest.fixture
def installation(db, tech):
    site = Site.objects.create(name="Install Site", city="Karachi")
    brand = Brand.objects.create(name="InstBrand")
    dm = DeviceModel.objects.create(brand=brand, name="I-1")
    primary = Client.objects.create(name="Primary Client", contact_person="Ali POC", contact_phone="0300-1234567")
    extra = Client.objects.create(name="Second Client")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-INST-1", serial_number="INST-1",
        display_name="Mall Entrance Screen", assigned_client=primary,
    )
    device.clients.add(extra)
    return DeviceInstallation.objects.create(
        device=device, site=site, installed_by=tech, installed_at=timezone.now()
    )


def _signed_page(name="signed-handover.pdf"):
    """Stand-in for the certificate coming back from site."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, b"%PDF-1.4 signed", content_type="application/pdf")


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_list_exposes_tracker_columns(ops, installation):
    r = _client(ops).get("/api/sites/installations/")
    assert r.status_code == 200, r.content
    row = r.data["results"][0]
    assert row["asset_name"] == "Mall Entrance Screen"
    assert row["client_names"] == ["Primary Client", "Second Client"]
    assert row["poc_name"] == "Ali POC"
    assert row["poc_phone"] == "0300-1234567"
    assert row["installed_by_name"] == "Tariq Installer"
    assert row["due_date"] is None
    assert row["completed_at"] is None
    assert row["client_delays"] == 0


@pytest.mark.django_db
def test_search_by_client_and_installer(ops, installation):
    c = _client(ops)
    assert c.get("/api/sites/installations/", {"search": "Second Client"}).data["count"] == 1
    assert c.get("/api/sites/installations/", {"search": "Tariq"}).data["count"] == 1
    assert c.get("/api/sites/installations/", {"search": "Mall Entrance"}).data["count"] == 1
    assert c.get("/api/sites/installations/", {"search": "no-such-thing"}).data["count"] == 0


@pytest.mark.django_db
def test_due_date_writable_by_manager(ops, installation):
    r = _client(ops).patch(
        f"/api/sites/installations/{installation.id}/", {"due_date": "2026-09-15"}, format="json"
    )
    assert r.status_code == 200, r.content
    installation.refresh_from_db()
    assert str(installation.due_date) == "2026-09-15"


@pytest.mark.django_db
def test_completed_at_stamped_and_cleared_with_steps(installation):
    steps = list(installation.steps.order_by("step_number"))
    assert len(steps) == 6
    for step in steps[:-1]:
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    installation.refresh_from_db()
    assert installation.completed_at is None

    steps[-1].status = InstallationStep.StepStatus.SKIPPED
    steps[-1].save()
    installation.refresh_from_db()
    assert installation.completed_at is not None

    # Reopening a step clears the completion stamp again.
    steps[0].status = InstallationStep.StepStatus.IN_PROGRESS
    steps[0].save()
    installation.refresh_from_db()
    assert installation.completed_at is None


@pytest.mark.django_db
def test_technician_can_log_client_delay(tech, installation):
    step = installation.steps.first()
    r = _client(tech).post("/api/sites/installation-delays/", {
        "installation": str(installation.id),
        "step": str(step.id),
        "cause": "client",
        "description": "Client did not grant site access.",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["reported_by_name"] == "Tariq Installer"
    assert r.data["cause_display"] == "Client"

    listing = _client(tech).get("/api/sites/installations/")
    assert listing.data["results"][0]["client_delays"] == 1


@pytest.mark.django_db
def test_delay_step_must_belong_to_installation(tech, installation):
    other_site = Site.objects.create(name="Other Site", city="Lahore")
    other = DeviceInstallation.objects.create(
        device=installation.device, site=other_site, installed_at=timezone.now()
    )
    foreign_step = other.steps.first()
    r = _client(tech).post("/api/sites/installation-delays/", {
        "installation": str(installation.id),
        "step": str(foreign_step.id),
        "cause": "client",
    }, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_installer_notified_on_assignment(tech, installation):
    from apps.notifications.models import Notification

    # Created with installed_by=tech (fixture) → one assignment notification.
    notifs = Notification.objects.filter(
        recipient=tech, notification_type="installation_assigned"
    )
    assert notifs.count() == 1
    assert installation.device.asset_code in notifs.first().message

    # Unrelated save must not re-notify.
    installation.notes = "touched"
    installation.save()
    assert notifs.count() == 1

    # Reassignment notifies the new installer.
    other = User.objects.create_user(username="site-tech-2", password="x", role="technician")
    installation.installed_by = other
    installation.save()
    assert Notification.objects.filter(
        recipient=other, notification_type="installation_assigned"
    ).count() == 1


@pytest.mark.django_db
def test_installer_phone_exposed(ops, tech, installation):
    tech.phone = "0301-7654321"
    tech.save()
    r = _client(ops).get("/api/sites/installations/")
    assert r.data["results"][0]["installed_by_phone"] == "0301-7654321"
    r = _client(ops).get(f"/api/sites/installations/{installation.id}/")
    assert r.data["installed_by_phone"] == "0301-7654321"


@pytest.mark.django_db
def test_step_update_restricted_to_installer_or_super_admin(ops, tech, installation):
    step = installation.steps.first()
    # ops manager may NOT advance steps from desktop
    r = _client(ops).patch(f"/api/sites/installation-steps/{step.id}/", {"status": "in_progress"}, format="json")
    assert r.status_code == 403
    # assigned installer may
    r = _client(tech).patch(f"/api/sites/installation-steps/{step.id}/", {"status": "in_progress"}, format="json")
    assert r.status_code == 200, r.content
    # super admin may (incl. the new on-hold status)
    admin = User.objects.create_user(username="site-admin", password="x", role="super_admin")
    r = _client(admin).patch(f"/api/sites/installation-steps/{step.id}/", {"status": "on_hold"}, format="json")
    assert r.status_code == 200, r.content
    step.refresh_from_db()
    assert step.status == "on_hold"


@pytest.mark.django_db
def test_delay_create_restricted(ops, installation):
    r = _client(ops).post("/api/sites/installation-delays/", {
        "installation": str(installation.id), "cause": "client",
    }, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_custom_step_pipeline(ops, installation):
    r = _client(ops).post("/api/sites/installations/", {
        "device": str(installation.device_id),
        "site": str(installation.site_id),
        "installed_at": timezone.now().isoformat(),
        "step_types": ["survey", "programming", "handover"],
    }, format="json")
    assert r.status_code == 201, r.content
    steps = r.data["steps"]
    assert [s["step_type"] for s in steps] == ["survey", "programming", "handover"]
    assert [s["step_number"] for s in steps] == [1, 2, 3]


@pytest.mark.django_db
def test_custom_named_steps_and_vendor(ops, installation):
    from apps.suppliers.models import Supplier

    vendor = Supplier.objects.create(name="Rigging Co")
    r = _client(ops).post("/api/sites/installations/", {
        "device": str(installation.device_id),
        "site": str(installation.site_id),
        "installed_at": timezone.now().isoformat(),
        "vendor": str(vendor.pk),
        "step_types": ["survey", "Crane lift", "handover"],
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["vendor_name"] == "Rigging Co"
    steps = r.data["steps"]
    assert [s["step_type"] for s in steps] == ["survey", "other", "handover"]
    assert steps[1]["step_type_display"] == "Crane lift"


@pytest.mark.django_db
def test_on_hold_steps_count_in_list(ops, installation):
    step = installation.steps.first()
    step.status = InstallationStep.StepStatus.ON_HOLD
    step.save()
    r = _client(ops).get("/api/sites/installations/")
    row = next(x for x in r.data["results"] if x["id"] == str(installation.id))
    assert row["on_hold_steps"] == 1


@pytest.mark.django_db
def test_installation_creation_puts_the_device_on_the_track(installation):
    device = installation.device
    device.refresh_from_db()
    # The fixture device starts as procured; opening the job puts it on the
    # installation track and the flip is journalled. It is *assigned*, not
    # installed — raising the job is not doing the work.
    assert device.status == "assigned"
    event = device.lifecycle_events.get(event_type="status_change", to_value="assigned")
    assert event.from_value == "procured"
    assert "Install Site" in event.description


@pytest.mark.django_db
def test_completion_marks_device_installed(installation):
    device = installation.device
    device.status = "in_stock"
    device.save()
    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    device.refresh_from_db()
    assert device.status == "installed"


@pytest.mark.django_db
def test_completion_records_when_the_asset_went_in(installation):
    """The installation date comes off the installation — never typed by hand."""
    device = installation.device
    device.status = "assigned"
    device.installation_date = None
    device.save()
    assert device.installation_date is None

    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()

    installation.refresh_from_db()
    device.refresh_from_db()
    assert installation.completed_at is not None
    assert device.installation_date == timezone.localdate(installation.completed_at)


@pytest.mark.django_db
def test_installation_date_is_filled_even_when_the_status_was_set_by_hand(installation):
    """An asset already marked Installed still gets its date from the tracker."""
    device = installation.device
    device.status = "installed"
    device.installation_date = None
    device.save()

    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()

    installation.refresh_from_db()
    device.refresh_from_db()
    assert device.installation_date == timezone.localdate(installation.completed_at)


def _complete_non_handover_steps(installation):
    for step in installation.steps.exclude(step_type=InstallationStep.StepType.HANDOVER):
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()


def test_handover_happy_path(installation, ops):
    _complete_non_handover_steps(installation)
    client = _client(ops)
    resp = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Mr. Client POC", "acceptance_notes": "All good",
         "signed_document": _signed_page()},
        format="multipart",
    )
    assert resp.status_code == 201, resp.data
    body = resp.json()
    assert body["handover"]["accepted_by_name"] == "Mr. Client POC"

    device = installation.device
    device.refresh_from_db()
    installation.refresh_from_db()
    assert device.status == "active"
    assert device.assigned_client_id == installation.handover.client_id
    assert device.current_site_id == installation.site_id
    assert device.installation_date == installation.handover.handover_date
    assert installation.completed_at is not None
    assert installation.steps.get(step_type="handover").status == "completed"
    # Journalled through the machine with the acceptance reason.
    event = device.lifecycle_events.get(event_type="status_change", to_value="active")
    assert "Mr. Client POC" in event.description


def test_handover_blocked_while_steps_pending(installation, ops):
    client = _client(ops)
    resp = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Early Bird"},
        format="multipart",
    )
    assert resp.status_code == 400
    assert "remaining steps" in resp.json()["detail"]


def test_handover_twice_rejected(installation, ops):
    _complete_non_handover_steps(installation)
    client = _client(ops)
    first = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Once", "signed_document": _signed_page()},
        format="multipart",
    )
    assert first.status_code == 201
    again = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Twice", "signed_document": _signed_page()},
        format="multipart",
    )
    assert again.status_code == 400
    assert "already" in again.json()["detail"]


def test_handover_forbidden_for_unassigned_technician(installation):
    _complete_non_handover_steps(installation)
    stranger = User.objects.create_user(username="site-tech-x", password="x", role="technician")
    client = _client(stranger)
    resp = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Nope"},
        format="multipart",
    )
    assert resp.status_code == 403


def test_handover_requires_client_when_device_has_none(installation, ops):
    _complete_non_handover_steps(installation)
    device = installation.device
    device.assigned_client = None
    device.save(update_fields=["assigned_client"])
    # Nothing anywhere names a client: not the asset, not a project, not the
    # site it stands on. Only then is there genuinely nobody to hand it to.
    device.clients.clear()
    installation.site.client = None
    installation.site.save(update_fields=["client"])
    client = _client(ops)
    resp = client.post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "No Client", "signed_document": _signed_page()},
        format="multipart",
    )
    assert resp.status_code == 400
    assert "client" in resp.json()


def test_assigned_installer_and_supervisor_can_handover(installation, tech):
    _complete_non_handover_steps(installation)
    r = _client(tech).post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Installer Handover", "signed_document": _signed_page()},
        format="multipart",
    )
    assert r.status_code == 201, r.content

    # A supervisor on a fresh installation works too.
    site2 = Site.objects.create(name="Second Site", city="Lahore")
    device2 = Device.objects.create(
        device_model=installation.device.device_model,
        asset_code="AST-INST-2", serial_number="INST-2",
        assigned_client=installation.device.assigned_client,
    )
    inst2 = DeviceInstallation.objects.create(
        device=device2, site=site2, installed_at=timezone.now()
    )
    _complete_non_handover_steps(inst2)
    supervisor = User.objects.create_user(username="site-super", password="x", role="supervisor")
    r2 = _client(supervisor).post(
        f"/api/sites/installations/{inst2.pk}/handover/",
        {"accepted_by_name": "Supervisor Handover", "signed_document": _signed_page()},
        format="multipart",
    )
    assert r2.status_code == 201, r2.content


def test_handover_reanchors_warranty_even_when_steps_already_done(installation, ops):
    from datetime import timedelta as td

    from apps.warranties.models import Warranty

    today = timezone.localdate()
    warranty = Warranty.objects.create(
        device=installation.device, warranty_type="client", status="active",
        start_date=today, end_date=today + td(days=365), months=12,
    )
    # Close out the WHOLE checklist first (mobile flow), incl. handover step.
    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    installation.refresh_from_db()
    assert installation.completed_at is not None

    paper_date = (today - td(days=30)).isoformat()
    r = _client(ops).post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Paper Acceptance", "handover_date": paper_date,
         "signed_document": _signed_page()},
        format="multipart",
    )
    assert r.status_code == 201, r.content
    warranty.refresh_from_db()
    assert str(warranty.start_date) == paper_date


# ── Wave 4: installation due-date escalation (ES-05) ──────────────────
#
# Policies come from the setup seed migration: scope=installation /
# trigger=due_date — stage 1 (hours=0) -> ops_manager, stage 2 (hours=24)
# -> group_head + ops_manager. Anchor = local midnight ending the due date.


@pytest.fixture
def esc_users(db):
    return {
        "gh": User.objects.create_user(username="inst-esc-gh", password="x", role="group_head"),
        "admin": User.objects.create_user(username="inst-esc-admin", password="x", role="super_admin"),
    }


def _overdue(installation, days):
    DeviceInstallation.objects.filter(pk=installation.pk).update(
        due_date=timezone.localdate() - timedelta(days=days)
    )


def _esc_notifs(installation):
    from apps.notifications.models import Notification

    return Notification.objects.filter(
        installation=installation, notification_type="installation_escalated"
    )


@pytest.mark.django_db
def test_overdue_installation_fires_stage1(ops, tech, installation, esc_users):
    _overdue(installation, days=1)  # anchor = today 00:00 → stage 1 only

    assert escalate_overdue_installations() == 1
    installation.refresh_from_db()
    assert set(installation.escalation_state) == {"due_date:1"}

    notifs = _esc_notifs(installation)
    assert notifs.filter(recipient=tech).exists()  # assigned installer
    assert notifs.filter(recipient=ops).exists()  # escalate_to_role (stage 1)
    assert notifs.filter(recipient=esc_users["admin"]).exists()  # super admin always
    assert not notifs.filter(recipient=esc_users["gh"]).exists()  # group head = stage 2
    assert all(n.is_actionable for n in notifs)


@pytest.mark.django_db
def test_stage2_fires_24h_later_to_group_head(ops, tech, installation, esc_users):
    _overdue(installation, days=2)  # anchor = yesterday 00:00 → both stages elapsed

    assert escalate_overdue_installations() == 2
    installation.refresh_from_db()
    assert set(installation.escalation_state) == {"due_date:1", "due_date:2"}
    assert _esc_notifs(installation).filter(recipient=esc_users["gh"]).exists()


@pytest.mark.django_db
def test_completed_installations_skipped(installation, esc_users):
    DeviceInstallation.objects.filter(pk=installation.pk).update(
        due_date=timezone.localdate() - timedelta(days=5),
        completed_at=timezone.now(),
    )
    assert escalate_overdue_installations() == 0
    installation.refresh_from_db()
    assert installation.escalation_state == {}
    assert not _esc_notifs(installation).exists()


@pytest.mark.django_db
def test_escalation_rerun_is_idempotent(installation, esc_users):
    _overdue(installation, days=3)
    assert escalate_overdue_installations() == 2
    installation.refresh_from_db()
    first_state = dict(installation.escalation_state)
    first_count = _esc_notifs(installation).count()

    assert escalate_overdue_installations() == 0
    installation.refresh_from_db()
    assert installation.escalation_state == first_state  # keys and timestamps untouched
    assert _esc_notifs(installation).count() == first_count


@pytest.mark.django_db
def test_escalated_filter_and_serializer_flag(ops, installation, esc_users):
    _overdue(installation, days=1)
    other = DeviceInstallation.objects.create(
        device=installation.device,
        site=installation.site,
        installed_at=timezone.now(),
    )
    escalate_overdue_installations()

    c = _client(ops)
    hot = c.get("/api/sites/installations/", {"escalated": "true"})
    assert [row["id"] for row in hot.data["results"]] == [str(installation.id)]
    assert hot.data["results"][0]["escalated"] is True
    assert "due_date:1" in hot.data["results"][0]["escalation_state"]

    cold = c.get("/api/sites/installations/", {"escalated": "false"})
    cold_ids = [row["id"] for row in cold.data["results"]]
    assert str(other.id) in cold_ids and str(installation.id) not in cold_ids
    row = next(r for r in cold.data["results"] if r["id"] == str(other.id))
    assert row["escalated"] is False

    detail = c.get(f"/api/sites/installations/{installation.id}/")
    assert detail.data["escalated"] is True
    assert "due_date:1" in detail.data["escalation_state"]


# ── Excel export (XC-01) ──────────────────────────────────────────────

import io as _io

from openpyxl import load_workbook as _load_workbook

from apps.accounts.models import AuditLog as _AuditLog


def _sheet_rows(resp):
    wb = _load_workbook(_io.BytesIO(resp.content), read_only=True)
    return [list(row) for row in wb.active.iter_rows(values_only=True)]


@pytest.mark.django_db
def test_installations_export_happy_path(ops, installation):
    # New installations auto-seed the 6-step pipeline; complete half of it.
    for step in installation.steps.order_by("step_number")[:3]:
        step.status = "completed"
        step.save()

    resp = _client(ops).get("/api/sites/installations/export/")
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    rows = _sheet_rows(resp)
    assert rows[0][:4] == ["Asset Code", "Asset Name", "Site", "Clients"]
    assert len(rows) == 2
    row = rows[1]
    assert row[0] == "AST-INST-1"
    assert row[1] == "Mall Entrance Screen"
    assert row[2] == "Install Site"
    assert row[3] == "Primary Client, Second Client"
    assert row[4] == "Tariq Installer"
    assert row[8] == 50  # 1 of 2 steps completed
    assert row[9] == "No"  # not escalated

    log = _AuditLog.objects.filter(action="export", resource_type="installation").latest("created_at")
    assert log.detail["count"] == 1
    assert log.user_id == ops.id


@pytest.mark.django_db
def test_installations_export_applies_filters(ops, installation, tech):
    other_site = Site.objects.create(name="Other Site", city="Lahore")
    brand = Brand.objects.create(name="ExpBrand")
    dm = DeviceModel.objects.create(brand=brand, name="E-1")
    other_device = Device.objects.create(
        device_model=dm, asset_code="AST-EXP-2", serial_number="EXP-2"
    )
    DeviceInstallation.objects.create(
        device=other_device, site=other_site, installed_by=tech, installed_at=timezone.now()
    )

    resp = _client(ops).get("/api/sites/installations/export/", {"site": str(other_site.id)})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert [r[0] for r in rows[1:]] == ["AST-EXP-2"]

    log = _AuditLog.objects.filter(action="export", resource_type="installation").latest("created_at")
    assert log.detail == {"count": 1, "params": {"site": str(other_site.id)}}


# ── Wave 5: ?bucket= tracker drill-downs ──────────────────────────────


@pytest.fixture
def bucket_installations(db, tech):
    """One installation per progress bucket (default 6-step checklist each)."""
    from apps.sites.models import InstallationDelay

    site = Site.objects.create(name="Bucket Site", city="Karachi")
    brand = Brand.objects.create(name="BucketBrand")
    dm = DeviceModel.objects.create(brand=brand, name="B-1")

    def mk(name, **kwargs):
        device = Device.objects.create(
            device_model=dm, serial_number=f"BKT-{name}", display_name=f"Bucket {name}",
        )
        return DeviceInstallation.objects.create(
            device=device, site=site, installed_by=tech, installed_at=timezone.now(), **kwargs
        )

    fresh = mk("fresh")

    progress = mk("progress")  # two advanced steps — distinct must dedupe
    for step, status in zip(progress.steps.order_by("step_number"), ("in_progress", "completed")):
        step.status = status
        step.save()

    hold = mk("hold")  # two held steps — distinct must dedupe
    for step in hold.steps.order_by("step_number")[:2]:
        step.status = InstallationStep.StepStatus.ON_HOLD
        step.save()

    done = mk("done")
    for step in done.steps.order_by("step_number"):
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    done.refresh_from_db()
    assert done.completed_at is not None  # signal stamped completion

    late = mk("late", due_date=timezone.localdate() - timedelta(days=1))

    delayed = mk("delayed")  # two client delays — distinct must dedupe
    for i in range(2):
        InstallationDelay.objects.create(
            installation=delayed, cause=InstallationDelay.Cause.CLIENT,
            description=f"Client kept the site closed ({i})", reported_by=tech,
        )
    # A non-client delay alone must NOT put an installation in the bucket.
    InstallationDelay.objects.create(
        installation=fresh, cause=InstallationDelay.Cause.VENDOR, description="Vendor late",
    )

    return {
        "fresh": fresh, "progress": progress, "hold": hold,
        "done": done, "late": late, "delayed": delayed,
    }


@pytest.mark.django_db
def test_installation_bucket_filters(ops, bucket_installations):
    c = _client(ops)

    def names(bucket):
        r = c.get("/api/sites/installations/", {"bucket": bucket, "page_size": 100})
        assert r.status_code == 200, r.content
        return [row["asset_name"] for row in r.data["results"]]

    assert names("completed") == ["Bucket done"]
    assert names("overdue") == ["Bucket late"]
    assert names("on_hold") == ["Bucket hold"]  # deduped despite 2 held steps
    assert names("in_progress") == ["Bucket progress"]  # deduped despite 2 steps
    assert sorted(names("not_started")) == ["Bucket delayed", "Bucket fresh", "Bucket late"]
    assert names("delayed") == ["Bucket delayed"]  # client-cause only, deduped

    # Unknown values are ignored — the whole tracker comes back.
    r = c.get("/api/sites/installations/", {"bucket": "bogus", "page_size": 100})
    assert len(r.data["results"]) == len(bucket_installations)


@pytest.mark.django_db
def test_installation_bucket_applies_to_export(ops, bucket_installations):
    resp = _client(ops).get("/api/sites/installations/export/", {"bucket": "overdue"})
    assert resp.status_code == 200, resp.content
    rows = _sheet_rows(resp)
    assert [r[1] for r in rows[1:]] == ["Bucket late"]

    log = _AuditLog.objects.filter(action="export", resource_type="installation").latest("created_at")
    assert log.detail == {"count": 1, "params": {"bucket": "overdue"}}


# ── Vendor access (XC-04) ─────────────────────────────────────────────


@pytest.fixture
def vendor_install(db, installation):
    """A second installation done by a vendor, plus vendor logins."""
    from apps.suppliers.models import Supplier

    supplier = Supplier.objects.create(name="Install Vendor")
    other_supplier = Supplier.objects.create(name="Rival Vendor")
    vendor_user = User.objects.create_user(
        username="site-vendor", password="x", role="vendor", supplier=supplier
    )
    other_vendor_user = User.objects.create_user(
        username="site-vendor-b", password="x", role="vendor", supplier=other_supplier
    )
    unlinked_vendor = User.objects.create_user(username="site-vendor-none", password="x", role="vendor")

    site = Site.objects.create(name="Vendor Site", city="Lahore")
    brand = Brand.objects.create(name="VendBrand")
    dm = DeviceModel.objects.create(brand=brand, name="V-1")
    device = Device.objects.create(
        device_model=dm, asset_code="AST-VEND-1", serial_number="VEND-1",
        display_name="Vendor Screen",
    )
    vendor_installation = DeviceInstallation.objects.create(
        device=device, site=site, vendor=supplier, installed_at=timezone.now()
    )
    return {
        "supplier": supplier,
        "vendor_user": vendor_user,
        "other_vendor_user": other_vendor_user,
        "unlinked_vendor": unlinked_vendor,
        "vendor_installation": vendor_installation,
    }


@pytest.mark.django_db
def test_vendor_sees_only_own_installations(vendor_install, installation):
    r = _client(vendor_install["vendor_user"]).get("/api/sites/installations/")
    assert r.status_code == 200
    assert r.data["count"] == 1
    assert r.data["results"][0]["id"] == str(vendor_install["vendor_installation"].id)
    # Detail of the tech-run installation is invisible to the vendor.
    r = _client(vendor_install["vendor_user"]).get(f"/api/sites/installations/{installation.id}/")
    assert r.status_code == 404
    # Other supplier's vendor and an unlinked vendor login see nothing.
    assert _client(vendor_install["other_vendor_user"]).get("/api/sites/installations/").data["count"] == 0
    assert _client(vendor_install["unlinked_vendor"]).get("/api/sites/installations/").data["count"] == 0


@pytest.mark.django_db
def test_vendor_advances_own_step_403_on_others(vendor_install, installation):
    c = _client(vendor_install["vendor_user"])
    own_step = vendor_install["vendor_installation"].steps.first()
    r = c.patch(f"/api/sites/installation-steps/{own_step.id}/", {"status": "in_progress"}, format="json")
    assert r.status_code == 200, r.content
    own_step.refresh_from_db()
    assert own_step.status == "in_progress"

    other_step = installation.steps.first()
    r = c.patch(f"/api/sites/installation-steps/{other_step.id}/", {"status": "in_progress"}, format="json")
    assert r.status_code == 403
    r = _client(vendor_install["other_vendor_user"]).patch(
        f"/api/sites/installation-steps/{own_step.id}/", {"status": "completed"}, format="json"
    )
    assert r.status_code == 403


@pytest.mark.django_db
def test_vendor_uploads_photos_own_installation_only(vendor_install, installation):
    import io

    from PIL import Image

    def _png():
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(buf, format="PNG")
        buf.seek(0)
        buf.name = "site.png"
        return buf

    c = _client(vendor_install["vendor_user"])
    r = c.post("/api/sites/installation-photos/", {
        "installation": str(vendor_install["vendor_installation"].id),
        "photo_type": "pre_install",
        "image": _png(),
    }, format="multipart")
    assert r.status_code == 201, r.content

    r = c.post("/api/sites/installation-photos/", {
        "installation": str(installation.id),
        "photo_type": "pre_install",
        "image": _png(),
    }, format="multipart")
    assert r.status_code == 403


@pytest.mark.django_db
def test_vendor_cannot_handover(vendor_install):
    inst = vendor_install["vendor_installation"]
    for step in inst.steps.exclude(step_type=InstallationStep.StepType.HANDOVER):
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    r = _client(vendor_install["vendor_user"]).post(
        f"/api/sites/installations/{inst.pk}/handover/",
        {"accepted_by_name": "Client POC"},
        format="multipart",
    )
    assert r.status_code == 403


def test_handover_client_falls_back_to_project_then_site(installation, ops):
    from apps.teams.models import Project

    device = installation.device
    project_client = Client.objects.create(name="Project Buyer")
    project = Project.objects.create(name="Fallback Project", client=project_client)
    device.assigned_client = None
    device.project = project
    device.save(update_fields=["assigned_client", "project"])
    _complete_non_handover_steps(installation)
    r = _client(ops).post(
        f"/api/sites/installations/{installation.pk}/handover/",
        {"accepted_by_name": "Fallback POC", "signed_document": _signed_page()},
        format="multipart",
    )
    assert r.status_code == 201, r.content
    device.refresh_from_db()
    assert device.assigned_client_id == project_client.pk


# ---------------------------------------------------------------------------
# WF-09: assigning an installation to a technician / vendor
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_installation_exposes_technician_manpower_details(db):
    """The technician's identity comes from their manpower record."""
    from apps.accounts.models import User as _U
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.sites.models import DeviceInstallation, Site
    from django.utils import timezone
    from rest_framework.test import APIClient

    admin = _U.objects.create_user(username="wf09-admin", password="x", role="super_admin")
    tech = _U.objects.create_user(
        username="wf09-tech", password="x", role="technician", is_field_staff=True,
        first_name="Bilal", last_name="Ahmed", employee_id="EMP-014",
        job_title="Senior Technician", phone="0300-1234567",
    )
    brand = Brand.objects.create(name="WF09 Brand")
    model = DeviceModel.objects.create(brand=brand, name="W-1")
    device = Device.objects.create(device_model=model, serial_number="WF09-SN-1")
    site = Site.objects.create(name="WF09 Site")
    inst = DeviceInstallation.objects.create(
        device=device, site=site, installed_by=tech, installed_at=timezone.now()
    )

    c = APIClient(); c.force_authenticate(admin)
    r = c.get(f"/api/sites/installations/{inst.id}/")
    assert r.status_code == 200, r.content
    assert r.data["installed_by_name"] == "Bilal Ahmed"
    assert r.data["installed_by_employee_id"] == "EMP-014"
    assert r.data["installed_by_job_title"] == "Senior Technician"
    assert r.data["installed_by_phone"] == "0300-1234567"


@pytest.fixture
def wf09_setup(db):
    from apps.accounts.models import User as _U
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.sites.models import DeviceInstallation, Site
    from django.utils import timezone
    from rest_framework.test import APIClient

    admin = _U.objects.create_user(username="wf09b-admin", password="x", role="super_admin")
    brand = Brand.objects.create(name="WF09B Brand")
    model = DeviceModel.objects.create(brand=brand, name="W-2")
    device = Device.objects.create(device_model=model, serial_number="WF09B-SN-1")
    site = Site.objects.create(name="WF09B Site")
    inst = DeviceInstallation.objects.create(device=device, site=site, installed_at=timezone.now())
    c = APIClient(); c.force_authenticate(admin)
    return {"client": c, "inst": inst}


@pytest.mark.django_db
def test_vendor_can_be_entered_manually(wf09_setup):
    c, inst = wf09_setup["client"], wf09_setup["inst"]
    r = c.patch(
        f"/api/sites/installations/{inst.id}/",
        {"external_vendor_name": "Rapid Signage Crew", "external_vendor_contact": "0321-9876543"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.data["vendor"] is None
    assert r.data["vendor_display"] == "Rapid Signage Crew (0321-9876543)"


@pytest.mark.django_db
def test_registered_vendor_still_wins_the_display(wf09_setup, db):
    from apps.suppliers.models import Supplier

    c, inst = wf09_setup["client"], wf09_setup["inst"]
    supplier = Supplier.objects.create(name="Registered Vendor Ltd")
    r = c.patch(f"/api/sites/installations/{inst.id}/", {"vendor": str(supplier.id)}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["vendor_display"] == "Registered Vendor Ltd"


@pytest.mark.django_db
def test_cannot_set_both_registered_and_manual_vendor(wf09_setup, db):
    from apps.suppliers.models import Supplier

    c, inst = wf09_setup["client"], wf09_setup["inst"]
    supplier = Supplier.objects.create(name="Both Vendor Ltd")
    r = c.patch(
        f"/api/sites/installations/{inst.id}/",
        {"vendor": str(supplier.id), "external_vendor_name": "Hand Typed Crew"},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert "external_vendor_name" in r.data


@pytest.mark.django_db
def test_manual_vendor_contact_needs_a_name(wf09_setup):
    c, inst = wf09_setup["client"], wf09_setup["inst"]
    r = c.patch(
        f"/api/sites/installations/{inst.id}/",
        {"external_vendor_contact": "0300-0000000"}, format="json",
    )
    assert r.status_code == 400, r.content
    assert "external_vendor_name" in r.data


# ---------------------------------------------------------------------------
# The tracker drives Installed -> Active, with the technician's photo
# ---------------------------------------------------------------------------
def _png():
    import base64

    from django.core.files.uploadedfile import SimpleUploadedFile

    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return SimpleUploadedFile("live.png", data, content_type="image/png")


@pytest.fixture
def live_install(db):
    tech = User.objects.create_user(
        username="live-tech", password="x", role="technician", is_field_staff=True
    )
    site = Site.objects.create(name="Live Site")
    device = Device.objects.create(
        asset_code="AST-LIVE-1", serial_number="LIVE-1", status="assigned",
        assigned_technician=tech,
    )
    installation = DeviceInstallation.objects.create(
        device=device, site=site, installed_by=tech, installed_at=timezone.now()
    )
    # Work the checklist the way the technician would; finishing it is what
    # makes the asset Installed and so eligible to be marked Active.
    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    device.refresh_from_db()
    return {"tech": tech, "device": device, "installation": installation}


@pytest.mark.django_db
def test_creating_the_installation_puts_the_asset_on_the_track(db):
    """The registry follows the tracker without anyone editing it — onto the
    track when the job opens, Installed only once the checklist is done."""
    site = Site.objects.create(name="Track Site")
    device = Device.objects.create(
        asset_code="AST-TRACK-1", serial_number="TRACK-1", status="in_stock",
    )
    installation = DeviceInstallation.objects.create(
        device=device, site=site, installed_at=timezone.now()
    )
    device.refresh_from_db()
    assert device.status == "assigned"

    # Working the checklist through is what makes it Installed.
    for step in installation.steps.all():
        step.status = InstallationStep.StepStatus.COMPLETED
        step.save()
    device.refresh_from_db()
    assert device.status == "installed"


@pytest.mark.django_db
def test_technician_activates_from_the_tracker_with_a_photo(live_install):
    c = APIClient()
    c.force_authenticate(live_install["tech"])
    installation, device = live_install["installation"], live_install["device"]

    # The photo is the evidence, so it is not optional.
    r = c.post(f"/api/sites/installations/{installation.id}/activate/", {}, format="multipart")
    assert r.status_code == 400, r.content
    assert "Upload a photo" in str(r.data["photos"])

    r = c.post(
        f"/api/sites/installations/{installation.id}/activate/",
        {"photos": _png(), "notes": "Powered on and running"},
        format="multipart",
    )
    assert r.status_code == 200, r.content

    device.refresh_from_db()
    assert device.status == "active"
    # It lands in the asset's own gallery as well as the installation record.
    assert device.images.count() == 1
    assert device.images.first().is_primary is True
    assert installation.photos.count() == 1
    # And the change is journalled against the technician who made it.
    event = device.lifecycle_events.order_by("-created_at").first()
    assert event.performed_by_id == live_install["tech"].id

    # Twice is a no-op, not a second journal entry.
    r = c.post(
        f"/api/sites/installations/{installation.id}/activate/",
        {"photos": _png()}, format="multipart",
    )
    assert r.status_code == 400
    assert "already active" in r.data["detail"]


@pytest.mark.django_db
def test_unrelated_technician_cannot_activate(live_install):
    other = User.objects.create_user(
        username="other-live-tech", password="x", role="technician", is_field_staff=True
    )
    c = APIClient()
    c.force_authenticate(other)
    r = c.post(
        f"/api/sites/installations/{live_install['installation'].id}/activate/",
        {"photos": _png()}, format="multipart",
    )
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_assigning_an_asset_opens_its_installation_job(db):
    """The reported gap: an asset assigned in the registry never reached the
    tracker, because the job had to be raised there by hand."""
    from rest_framework.test import APIClient as _Client

    ops = User.objects.create_user(username="track-ops", password="x", role="ops_manager")
    tech = User.objects.create_user(
        username="track-tech", password="x", role="technician", is_field_staff=True,
    )
    site = Site.objects.create(name="Assign-Opens Site")
    device = Device.objects.create(
        asset_code="AST-OPEN-1", serial_number="OPEN-1", status="in_stock", current_site=site,
    )
    assert device.installations.count() == 0

    c = _Client()
    c.force_authenticate(ops)
    r = c.post(
        f"/api/assets/devices/{device.id}/transition/",
        {"status": "assigned", "reason": "Ready to install", "assigned_technician": str(tech.id)},
        format="json",
    )
    assert r.status_code == 200, r.content

    installation = device.installations.get()
    assert installation.site_id == site.id
    assert installation.installed_by_id == tech.id
    # It comes with the standard checklist, so the tracker has something to run.
    assert installation.steps.count() == 6

    # It shows up in the tracker listing.
    listed = c.get("/api/sites/installations/", {"device": str(device.id)}).json()
    assert (listed.get("results") or listed)[0]["id"] == str(installation.id)

    # Reassigning mid-flight keeps the job and its progress.
    other = User.objects.create_user(
        username="track-tech-2", password="x", role="technician", is_field_staff=True,
    )
    r = c.post(
        f"/api/assets/devices/{device.id}/reassign/",
        {"assigned_technician": str(other.id), "reason": "swap"}, format="json",
    )
    assert r.status_code == 200, r.content
    assert device.installations.count() == 1


@pytest.mark.django_db
def test_an_asset_assigned_before_it_had_a_site_joins_the_tracker_later(db):
    """Assets assigned without a site were invisible on the tracker; setting
    the site puts them on it rather than leaving them stranded."""
    from rest_framework.test import APIClient as _Client

    ops = User.objects.create_user(username="late-ops", password="x", role="ops_manager")
    tech = User.objects.create_user(
        username="late-tech", password="x", role="technician", is_field_staff=True,
    )
    device = Device.objects.create(
        asset_code="AST-LATE-1", serial_number="LATE-1", status="assigned",
        assigned_technician=tech,
    )
    assert device.installations.count() == 0

    site = Site.objects.create(name="Late Site")
    c = _Client()
    c.force_authenticate(ops)
    r = c.patch(f"/api/assets/devices/{device.id}/", {"current_site": str(site.id)}, format="json")
    assert r.status_code == 200, r.content

    installation = device.installations.get()
    assert installation.site_id == site.id
    assert installation.installed_by_id == tech.id
    assert installation.steps.count() == 6


# ---------------------------------------------------------------------------
# Installation checklists: defined once per asset type, reused after that
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_installation_checklist_is_saved_and_reused_per_asset_type(db):
    from rest_framework.test import APIClient as _Client

    from apps.assets.models import AssetType
    from apps.sites.models import InstallationRouteTemplate, InstallationStep as _Step

    ops = User.objects.create_user(username="chk-ops", password="x", role="ops_manager")
    c = _Client()
    c.force_authenticate(ops)

    asset_type = AssetType.objects.create(name="Checklist Standee")
    site = Site.objects.create(name="Checklist Site")
    first = Device.objects.create(
        asset_code="AST-CHK-1", serial_number="CHK-1", asset_type=asset_type, current_site=site,
    )
    job = DeviceInstallation.objects.create(device=first, site=site, installed_at=timezone.now())
    # Falls back to the generic list until a standard exists.
    assert job.steps.count() == 6

    # Trim it to what this type actually needs, then save it as the standard.
    job.steps.filter(step_type__in=["wiring", "programming"]).delete()
    r = c.post(f"/api/sites/installations/{job.id}/save-step-template/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["saved_steps"] == 4
    assert InstallationRouteTemplate.objects.filter(asset_type=asset_type).exists()

    # The next asset of the same type opens with that checklist, not the generic one.
    second = Device.objects.create(
        asset_code="AST-CHK-2", serial_number="CHK-2", asset_type=asset_type, current_site=site,
    )
    job2 = DeviceInstallation.objects.create(device=second, site=site, installed_at=timezone.now())
    assert job2.steps.count() == 4
    assert list(job2.steps.values_list("step_number", flat=True)) == [1, 2, 3, 4]

    detail = c.get(f"/api/sites/installations/{job2.id}/").json()
    assert detail["step_template_available"] is True

    # A job already under way is not silently rebuilt.
    step = job2.steps.first()
    step.status = _Step.StepStatus.IN_PROGRESS
    step.save()
    r = c.post(f"/api/sites/installations/{job2.id}/apply-step-template/", {}, format="json")
    assert r.status_code == 400
    assert "already started" in r.data["detail"]


@pytest.mark.django_db
def test_installation_health_reports_the_thing_to_act_on(db):
    from datetime import timedelta

    from rest_framework.test import APIClient as _Client

    from apps.sites.models import InstallationDelay, InstallationStep as _Step

    ops = User.objects.create_user(username="health-ops", password="x", role="ops_manager")
    c = _Client()
    c.force_authenticate(ops)

    site = Site.objects.create(name="Health Site")
    device = Device.objects.create(asset_code="AST-HLT-1", serial_number="HLT-1")
    job = DeviceInstallation.objects.create(
        device=device, site=site, installed_at=timezone.now(),
        due_date=timezone.localdate() + timedelta(days=30),
    )

    def health():
        return c.get(f"/api/sites/installations/{job.id}/").json()

    assert health()["health"] == "not_started"

    step = job.steps.first()
    step.status = _Step.StepStatus.IN_PROGRESS
    step.save()
    assert health()["health"] == "on_time"

    InstallationDelay.objects.create(
        installation=job, cause=InstallationDelay.Cause.CLIENT, description="Client not ready",
    )
    body = health()
    assert body["health"] == "delayed"
    assert "unresolved delay" in body["health_reason"]

    # On hold outranks a delay: it is what somebody has to act on.
    step.status = _Step.StepStatus.ON_HOLD
    step.save()
    body = health()
    assert body["health"] == "on_hold"
    assert "On hold at" in body["health_reason"]

    # Overdue when nothing is blocking but the date has passed.
    step.status = _Step.StepStatus.IN_PROGRESS
    step.save()
    job.delays.update(resolved_at=timezone.now())
    job.due_date = timezone.localdate() - timedelta(days=2)
    job.save(update_fields=["due_date"])
    body = health()
    assert body["health"] == "overdue"
    assert "past the due date" in body["health_reason"]


@pytest.mark.django_db
def test_client_warranty_runs_from_the_installation_date(installation, ops):
    """The term may be typed days after the job; the cover still starts on the
    day the asset was installed — the date the asset shows."""
    from datetime import timedelta as td

    from apps.sites.signals import _anchor_client_warranties, installation_date_for
    from apps.warranties.models import Warranty

    installed = timezone.now() - td(days=9)
    installation.completed_at = installed
    installation.save(update_fields=["completed_at"])
    today = timezone.localdate()
    warranty = Warranty.objects.create(
        device=installation.device, warranty_type="client", status="active",
        start_date=today, end_date=today + td(days=365), months=12,
    )
    _anchor_client_warranties(installation)
    warranty.refresh_from_db()
    assert warranty.start_date == installation_date_for(installation) == timezone.localdate(installed)
    assert (warranty.end_date.year, warranty.end_date.month) == ((warranty.start_date.year + 1), warranty.start_date.month)


@pytest.fixture
def checklist(db):
    """An installation with six steps, numbered 1 to 6."""
    from django.utils import timezone

    from apps.assets.models import AssetType, Device
    from apps.sites.models import DeviceInstallation, InstallationStep, Site

    site = Site.objects.create(name="Order Site", address="1 Road")
    kind = AssetType.objects.create(name="Order Kind")
    device = Device.objects.create(asset_type=kind, current_site=site)
    job = DeviceInstallation(device=device, site=site, installed_at=timezone.now())
    job._skip_default_steps = True
    job.save()
    job.steps.all().delete()
    kinds = ["survey", "wiring", "structure", "programming", "testing", "handover"]
    for n, kind_name in enumerate(kinds, start=1):
        InstallationStep.objects.create(
            installation=job, step_type=kind_name, step_number=n,
        )
    return job


def _numbers(job):
    return list(job.steps.order_by("step_number").values_list("step_number", flat=True))


def _types(job):
    return list(job.steps.order_by("step_number").values_list("step_type", flat=True))


@pytest.mark.django_db
def test_removing_a_step_closes_the_gap_it_leaves(ops, checklist):
    """A checklist that reads 1, 2, 3, 5, 6 looks like it lost a step.

    The numbers are the positions in the list, so deleting the fourth makes
    the fifth the fourth rather than leaving a hole where it used to be.
    """
    fourth = checklist.steps.get(step_number=4)
    r = _client(ops).delete(f"/api/sites/installation-steps/{fourth.id}/")
    assert r.status_code == 204, r.content

    assert _numbers(checklist) == [1, 2, 3, 4, 5]
    assert _types(checklist) == ["survey", "wiring", "structure", "testing", "handover"]


@pytest.mark.django_db
def test_a_step_can_be_moved_to_any_position(ops, checklist):
    """A step remembered late belongs where it happens, not at the end."""
    ids = [str(s.id) for s in checklist.steps.order_by("step_number")]
    # Take the last one and put it first.
    r = _client(ops).post(
        f"/api/sites/installations/{checklist.id}/reorder-steps/",
        {"steps": [ids[-1], *ids[:-1]]}, format="json",
    )
    assert r.status_code == 200, r.content

    assert _numbers(checklist) == [1, 2, 3, 4, 5, 6]
    assert _types(checklist) == [
        "handover", "survey", "wiring", "structure", "programming", "testing",
    ]


@pytest.mark.django_db
def test_reordering_from_a_stale_list_keeps_every_step(ops, checklist):
    """A screen that has not refreshed must not be able to drop a step.

    Anything the caller leaves out keeps its place at the end rather than
    being forgotten, so a slow browser cannot delete work by accident.
    """
    ids = [str(s.id) for s in checklist.steps.order_by("step_number")]
    r = _client(ops).post(
        f"/api/sites/installations/{checklist.id}/reorder-steps/",
        {"steps": [ids[3]]}, format="json",
    )
    assert r.status_code == 200, r.content

    assert checklist.steps.count() == 6
    assert _numbers(checklist) == [1, 2, 3, 4, 5, 6]
    assert _types(checklist)[0] == "programming"


@pytest.mark.django_db
def test_reordering_needs_the_order(ops, checklist):
    r = _client(ops).post(
        f"/api/sites/installations/{checklist.id}/reorder-steps/", {"steps": []},
        format="json",
    )
    assert r.status_code == 400
    assert "steps" in r.data


@pytest.fixture
def handover_ready(db, ops):
    """An installed asset on a project raised for a named client."""
    from django.utils import timezone

    from apps.assets.models import AssetType, Device
    from apps.clients.models import Client
    from apps.sites.models import DeviceInstallation, Site
    from apps.teams.models import Project, ProjectScopeItem

    owner = Client.objects.create(name="Handover Owner")
    stranger = Client.objects.create(name="Somebody Else")
    site = Site.objects.create(name="Handover Guard Site", address="1 Road")
    project = Project.objects.create(name="Handover Guard Rollout", client=owner)
    kind = AssetType.objects.create(name="Handover Guard Kind")
    device = Device.objects.create(
        asset_type=kind, current_site=site, status=Device.Status.INSTALLED,
    )
    ProjectScopeItem.objects.create(project=project, device=device, quantity=1)
    job = DeviceInstallation(device=device, site=site, installed_at=timezone.now())
    job._skip_default_steps = True
    job.save()
    return {"job": job, "device": device, "owner": owner, "stranger": stranger}


@pytest.mark.django_db
def test_handover_refuses_a_client_the_project_never_named(ops, handover_ready):
    """Two people could otherwise disagree about whose asset it is.

    The client was settled when the project was raised. Naming a different one
    at handover would hand the asset to somebody the order was never sold to,
    so it is refused and says where to change it.
    """
    r = _client(ops).post(
        f"/api/sites/installations/{handover_ready['job'].id}/handover/",
        {
            "accepted_by_name": "Site Manager",
            "client": str(handover_ready["stranger"].id),
            "signed_document": _signed_page(),
        },
        format="multipart",
    )
    assert r.status_code == 400, r.content
    assert "Handover Owner" in str(r.data["client"])


@pytest.mark.django_db
def test_handover_takes_the_project_client_without_being_told(ops, handover_ready):
    """Nobody re-types what the project already recorded."""
    from apps.sites.models import HandoverRecord

    r = _client(ops).post(
        f"/api/sites/installations/{handover_ready['job'].id}/handover/",
        {"accepted_by_name": "Site Manager", "signed_document": _signed_page()},
        format="multipart",
    )
    assert r.status_code in (200, 201), r.content

    record = HandoverRecord.objects.get(installation=handover_ready["job"])
    assert record.client == handover_ready["owner"]


@pytest.mark.django_db
def test_handover_needs_the_signed_document(ops, handover_ready):
    """The client's signature is the handover, so there is no handover without it.

    Recording one on a promise that the paperwork will follow leaves an asset
    marked as accepted with nothing to show for it.
    """
    from apps.sites.models import HandoverRecord

    r = _client(ops).post(
        f"/api/sites/installations/{handover_ready['job'].id}/handover/",
        {"accepted_by_name": "Site Manager"}, format="multipart",
    )
    assert r.status_code == 400, r.content
    assert "signed_document" in r.data
    assert not HandoverRecord.objects.filter(installation=handover_ready["job"]).exists()


@pytest.mark.django_db
def test_the_signed_document_is_kept_on_the_record(ops, handover_ready):
    """What was signed stays attached to what it settled."""
    from apps.sites.models import HandoverRecord

    r = _client(ops).post(
        f"/api/sites/installations/{handover_ready['job'].id}/handover/",
        {"accepted_by_name": "Site Manager", "signed_document": _signed_page()},
        format="multipart",
    )
    assert r.status_code in (200, 201), r.content

    record = HandoverRecord.objects.get(installation=handover_ready["job"])
    assert record.signed_document, "the signed certificate should be filed against the handover"
    # Uploads are stored under a generated name, so what matters is that the
    # file is there and readable, not what it was called on somebody's laptop.
    assert record.signed_document.name.endswith(".pdf")
    with record.signed_document.open("rb") as fh:
        assert fh.read().startswith(b"%PDF")
