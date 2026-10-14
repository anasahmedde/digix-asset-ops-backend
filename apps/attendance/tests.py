import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User, UserCapability
from apps.sites.models import Site


@pytest.mark.django_db
def test_a_technician_checks_in_and_reads_it_back_without_the_register():
    """Clocking in and asking "am I in?" are the person's own business; the
    register (view_attendance) is not needed for either."""
    tech = User.objects.create_user(username="att-tech", password="x", role="technician", is_field_staff=True)
    # As on the live system: the technician role does not carry the register.
    UserCapability.objects.create(user=tech, capability="view_attendance", allowed=False)
    assert not tech.can("view_attendance")
    site = Site.objects.create(name="Packages Mall", address="Lahore")
    c = APIClient()
    c.force_authenticate(tech)

    r = c.post("/api/attendance/records/", {"check_type": "check_in", "site": str(site.id)}, format="json")
    assert r.status_code == 201, r.content
    r = c.get("/api/attendance/records/status/")
    assert r.status_code == 200, r.content
    assert r.data["checked_in"] is True and str(r.data["last"]["site"]) == str(site.id)

    r = c.post("/api/attendance/records/", {"check_type": "check_out", "site": str(site.id)}, format="json")
    assert r.status_code == 201, r.content
    assert c.get("/api/attendance/records/status/").data["checked_in"] is False

    # The register itself stays closed to them.
    assert c.get("/api/attendance/records/").status_code == 403
