# Tests will be added alongside model implementations.
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import AssetComponent, Brand, Device, DeviceModel
from apps.teams.models import Project


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_project_scope_and_milestones():
    ops = User.objects.create_user(username="team-ops", password="x", role="ops_manager")
    brand = Brand.objects.create(name="TeamBrand")
    dm = DeviceModel.objects.create(brand=brand, name="T-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-TEAM-1", serial_number="TEAM-1")
    comp = AssetComponent.objects.create(device=device, name="SMD Module", quantity=10)
    other_device = Device.objects.create(device_model=dm, asset_code="AST-TEAM-2", serial_number="TEAM-2")
    project = Project.objects.create(name="Mall rollout", phase="production")
    c = _client(ops)

    r = c.post("/api/teams/scope-items/", {
        "project": str(project.pk),
        "device": str(device.pk),
        "component": str(comp.pk),
        "quantity": 4,
        "start_date": "2026-09-01",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.data["component_name"] == "SMD Module"

    # component from a different asset is rejected
    r = c.post("/api/teams/scope-items/", {
        "project": str(project.pk),
        "device": str(other_device.pk),
        "component": str(comp.pk),
    }, format="json")
    assert r.status_code == 400

    r = c.post("/api/teams/milestones/", {
        "project": str(project.pk), "title": "Structures ready", "due_date": "2026-09-15", "order": 1,
    }, format="json")
    assert r.status_code == 201, r.content

    detail = c.get(f"/api/teams/projects/{project.pk}/")
    assert detail.data["phase"] == "production"
    assert detail.data["phase_display"] == "Production"
    assert len(detail.data["scope_items"]) == 1
    assert len(detail.data["milestones"]) == 1

    # phase is writable through the normal update flow
    r = c.patch(f"/api/teams/projects/{project.pk}/", {"phase": "delivery"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["phase"] == "delivery"
