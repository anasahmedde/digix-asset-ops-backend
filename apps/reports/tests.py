import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import AssetType, Device
from apps.maintenance.models import MaintenanceSchedule, MaintenanceVisit
from apps.sites.models import Site


@pytest.mark.django_db
def test_visits_report_lists_each_visit_and_rolls_up_by_technician():
    ops = User.objects.create_user(username="rep-ops", password="x", role="ops_manager")
    tech = User.objects.create_user(username="rep-tech", password="x", role="technician",
                                    first_name="Rana", last_name="Tech")
    site = Site.objects.create(name="Report Site", address="x")
    kind = AssetType.objects.create(name="Rep Screen")
    device = Device.objects.create(asset_type=kind, asset_code="AST-REP-1", status=Device.Status.ACTIVE)
    job = MaintenanceSchedule.objects.create(
        title="Monthly clean", device=device, site=site, assigned_to=tech,
        next_due=timezone.localdate(), maintenance_type="preventive",
    )
    now = timezone.now()
    for i in range(2):
        MaintenanceVisit.objects.create(
            schedule=job, due_date=timezone.localdate(), assigned_to=tech, sequence=i + 1,
            status=MaintenanceVisit.Status.COMPLETED, started_at=now, completed_at=now + timezone.timedelta(hours=2),
            asset_verified_at=now if i == 0 else None,
        )
    c = APIClient()
    c.force_authenticate(ops)

    r = c.get("/api/reports/generate/", {"type": "visits"})
    assert r.status_code == 200, r.content
    keys = [col["key"] for col in r.data["columns"]]
    assert keys[:3] == ["technician", "site", "project"]
    assert len(r.data["rows"]) >= 2
    assert {row["asset_verified"] for row in r.data["rows"] if row["asset"] == "AST-REP-1"} == {"Yes", "No"}

    r = c.get("/api/reports/generate/", {"type": "visits", "group_by": "technician", "technician": str(tech.id)})
    assert r.status_code == 200, r.content
    # The schedule opened its own planned round as well as the two completed ones.
    assert r.data["rows"] == [{
        "technician": "Rana Tech", "visits": 3, "completed": 2, "in_progress": 0,
        "awaiting_review": 0, "planned": 1, "unresolved": 0, "hours": 4.0, "avg_hours": 2.0,
    }]

    r = c.get("/api/reports/generate/", {"type": "visits", "group_by": "site", "site": str(site.id)})
    assert r.data["rows"][0]["site"] == "Report Site" and r.data["rows"][0]["visits"] == 3
