# Tests will be added alongside feature development.
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Brand, Device, DeviceModel
from apps.clients.models import Client
from apps.sites.models import DeviceInstallation, InstallationStep, Site


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
