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


# ── The field app's offline outbox ──────────────────────────────────────

@pytest.mark.django_db
def test_an_act_recorded_offline_is_dated_when_it_happened_and_sent_once(settings):
    """No signal on site: the phone keeps the check-in with its own time and
    sends it later. It is dated then, marked offline, and a resend after a
    lost reply is answered again rather than done twice."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.attendance.models import AttendanceRecord

    tech = User.objects.create_user(username="off-tech", password="x", role="technician", is_field_staff=True)
    site = Site.objects.create(name="Basement Mall", address="Lahore")
    c = APIClient()
    c.force_authenticate(tech)
    then = timezone.now() - timedelta(hours=3)
    headers = {"HTTP_X_RECORDED_AT": then.isoformat(), "HTTP_X_IDEMPOTENCY_KEY": "op-0001"}

    r = c.post("/api/attendance/records/", {"check_type": "check_in", "site": str(site.id)}, format="json", **headers)
    assert r.status_code == 201, r.content
    again = c.post("/api/attendance/records/", {"check_type": "check_in", "site": str(site.id)}, format="json", **headers)
    assert again.status_code == 201 and again.json()["id"] == r.data["id"], "a resend is answered, not repeated"
    assert AttendanceRecord.objects.filter(user=tech).count() == 1

    record = AttendanceRecord.objects.get(user=tech)
    assert abs((record.created_at - then).total_seconds()) < 1
    assert record.recorded_offline and record.uploaded_at > record.created_at
    assert r.data["recorded_offline"] is True

    # A phone clock running ahead, or something a week old, is refused.
    ahead = (timezone.now() + timedelta(hours=1)).isoformat()
    assert c.post("/api/attendance/records/", {"check_type": "check_out", "site": str(site.id)}, format="json",
                  HTTP_X_RECORDED_AT=ahead).status_code == 400
    stale = (timezone.now() - timedelta(days=8)).isoformat()
    assert c.post("/api/attendance/records/", {"check_type": "check_out", "site": str(site.id)}, format="json",
                  HTTP_X_RECORDED_AT=stale).status_code == 400


@pytest.mark.django_db
def test_an_offline_act_is_judged_by_where_the_person_was_at_the_time():
    """Checked in at A at 10:00 with no signal, then at B at 13:00 with
    signal: the scan made at A at 10:30, uploaded after, still counts as on
    site at A."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.attendance.presence import clocked_in_at
    from common import clock

    tech = User.objects.create_user(username="off-tech2", password="x", role="technician", is_field_staff=True)
    a = Site.objects.create(name="Site A", address="Lahore")
    b = Site.objects.create(name="Site B", address="Lahore")
    c = APIClient()
    c.force_authenticate(tech)
    ten = timezone.now() - timedelta(hours=4)
    c.post("/api/attendance/records/", {"check_type": "check_in", "site": str(a.id)}, format="json",
           HTTP_X_RECORDED_AT=ten.isoformat())
    c.post("/api/attendance/records/", {"check_type": "check_in", "site": str(b.id)}, format="json")
    assert clocked_in_at(tech, b.id) and not clocked_in_at(tech, a.id), "now, they are at B"
    token = clock._recorded.set(ten + timedelta(minutes=30))
    try:
        assert clocked_in_at(tech, a.id), "at 10:30 they were at A"
    finally:
        clock._recorded.reset(token)
