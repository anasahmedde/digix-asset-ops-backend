# Tests will be added alongside feature development.
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AuditLog, User
from apps.accounts.tasks import deactivate_left_employees


@pytest.fixture
def admin(db):
    return User.objects.create_user(username="acc-admin", password="x", role="super_admin")


@pytest.fixture
def technician(db):
    return User.objects.create_user(
        username="acc-tech", password="x", role="technician",
        first_name="Tariq", cnic="11111-1111111-1",
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="acc-other", password="x", role="technician",
        cnic="22222-2222222-2",
    )


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_cnic_validator_rejects_bad_format(admin):
    c = _client(admin)
    r = c.post("/api/accounts/users/", {
        "username": "acc-badcnic",
        "password": "password123",
        "cnic": "12345-BAD-1",
    }, format="json")
    assert r.status_code == 400, r.content
    assert "cnic" in r.data
    # digits in the wrong grouping are rejected too
    r = c.post("/api/accounts/users/", {
        "username": "acc-badcnic2",
        "password": "password123",
        "cnic": "1234-12345678-1",
    }, format="json")
    assert r.status_code == 400
    assert "cnic" in r.data


@pytest.mark.django_db
def test_hr_fields_round_trip_on_create(admin):
    c = _client(admin)
    r = c.post("/api/accounts/users/", {
        "username": "acc-hr",
        "password": "password123",
        "first_name": "Hina",
        "last_name": "Raza",
        "role": "technician",
        "employee_id": "EMP-0042",
        "cnic": "12345-1234567-1",
        "join_date": "2026-01-15",
        "leaving_date": "2026-12-31",
    }, format="json")
    assert r.status_code == 201, r.content
    user = User.objects.get(username="acc-hr")
    r = c.get(f"/api/accounts/users/{user.pk}/")
    assert r.status_code == 200
    assert r.data["employee_id"] == "EMP-0042"
    assert r.data["cnic"] == "12345-1234567-1"
    assert r.data["join_date"] == "2026-01-15"
    assert r.data["leaving_date"] == "2026-12-31"


@pytest.mark.django_db
def test_deactivate_left_employees():
    today = timezone.now().date()
    left = User.objects.create_user(
        username="acc-left", password="x", leaving_date=today - timedelta(days=1)
    )
    leaving_today = User.objects.create_user(
        username="acc-today", password="x", leaving_date=today
    )
    future = User.objects.create_user(
        username="acc-future", password="x", leaving_date=today + timedelta(days=30)
    )
    staying = User.objects.create_user(username="acc-staying", password="x")
    already_off = User.objects.create_user(
        username="acc-off", password="x",
        leaving_date=today - timedelta(days=10), is_active=False,
    )

    assert deactivate_left_employees() == 1

    left.refresh_from_db()
    leaving_today.refresh_from_db()
    future.refresh_from_db()
    staying.refresh_from_db()
    assert left.is_active is False
    assert leaving_today.is_active is True  # last day still has access
    assert future.is_active is True
    assert staying.is_active is True

    log = AuditLog.objects.get(resource_type="user", resource_id=str(left.pk))
    assert log.action == AuditLog.Action.UPDATE
    assert log.detail["auto"] is True
    assert "deactivat" in log.detail["reason"].lower()
    assert not AuditLog.objects.filter(resource_id=str(already_off.pk)).exists()

    # one-shot
    assert deactivate_left_employees() == 0
    assert AuditLog.objects.filter(resource_type="user").count() == 1


@pytest.mark.django_db
def test_technician_cannot_patch_other_user(technician, other_user):
    c = _client(technician)
    r = c.patch(f"/api/accounts/users/{other_user.pk}/", {"first_name": "Hacked"}, format="json")
    assert r.status_code == 403
    other_user.refresh_from_db()
    assert other_user.first_name != "Hacked"


@pytest.mark.django_db
def test_technician_cannot_write_privileged_fields_on_self(technician):
    c = _client(technician)
    r = c.patch(f"/api/accounts/users/{technician.pk}/", {
        "role": "super_admin",
        "is_active": False,
        "employee_id": "EMP-9999",
        "cnic": "99999-9999999-9",
        "join_date": "2020-01-01",
        "leaving_date": "2020-01-02",
        "username": "acc-tech-renamed",
    }, format="json")
    # Privileged fields are read-only for non-admins: silently ignored.
    assert r.status_code == 200, r.content
    technician.refresh_from_db()
    assert technician.role == "technician"
    assert technician.is_active is True
    assert technician.employee_id == ""
    assert technician.cnic == "11111-1111111-1"
    assert technician.join_date is None
    assert technician.leaving_date is None
    assert technician.username == "acc-tech"


@pytest.mark.django_db
def test_technician_can_update_own_profile_fields(technician):
    # Mirrors the web settings + mobile settings PATCH payloads.
    c = _client(technician)
    r = c.patch(f"/api/accounts/users/{technician.pk}/", {
        "first_name": "Tariq",
        "last_name": "Mehmood",
        "email": "tariq@example.com",
        "phone": "0300-1234567",
    }, format="json")
    assert r.status_code == 200, r.content
    technician.refresh_from_db()
    assert technician.last_name == "Mehmood"
    assert technician.email == "tariq@example.com"
    assert technician.phone == "0300-1234567"


@pytest.mark.django_db
def test_technician_cannot_read_others_cnic(technician, other_user):
    c = _client(technician)
    # List: own cnic visible, everyone else's nulled.
    r = c.get("/api/accounts/users/")
    assert r.status_code == 200
    rows = r.data.get("results", r.data)
    by_username = {row["username"]: row for row in rows}
    assert by_username["acc-tech"]["cnic"] == "11111-1111111-1"
    assert by_username["acc-other"]["cnic"] is None
    # Retrieve someone else: cnic nulled.
    r = c.get(f"/api/accounts/users/{other_user.pk}/")
    assert r.status_code == 200
    assert r.data["cnic"] is None
    # /me/ still returns own cnic.
    r = c.get("/api/accounts/users/me/")
    assert r.status_code == 200
    assert r.data["cnic"] == "11111-1111111-1"


@pytest.mark.django_db
def test_super_admin_retains_full_read_write(admin, technician):
    c = _client(admin)
    r = c.patch(f"/api/accounts/users/{technician.pk}/", {
        "role": "supervisor",
        "employee_id": "EMP-0042",
        "cnic": "33333-3333333-3",
        "leaving_date": "2027-06-30",
        "is_active": False,
    }, format="json")
    assert r.status_code == 200, r.content
    technician.refresh_from_db()
    assert technician.role == "supervisor"
    assert technician.employee_id == "EMP-0042"
    assert technician.cnic == "33333-3333333-3"
    assert str(technician.leaving_date) == "2027-06-30"
    assert technician.is_active is False
    # Admin can read anyone's cnic.
    r = c.get(f"/api/accounts/users/{technician.pk}/")
    assert r.status_code == 200
    assert r.data["cnic"] == "33333-3333333-3"
