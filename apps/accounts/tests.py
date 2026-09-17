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


# ── Vendor access (XC-04): supplier link is admin-only write ──────────


@pytest.mark.django_db
def test_supplier_field_admin_only_write(admin):
    from apps.suppliers.models import Supplier

    supplier = Supplier.objects.create(name="Portal Vendor Co")
    other = Supplier.objects.create(name="Another Vendor Co")
    c_admin = _client(admin)

    # Admin can create a vendor login with a supplier link.
    r = c_admin.post("/api/accounts/users/", {
        "username": "acc-vendor",
        "password": "password123",
        "role": "vendor",
        "supplier": str(supplier.id),
    }, format="json")
    assert r.status_code == 201, r.content
    vendor = User.objects.get(username="acc-vendor")
    assert vendor.supplier_id == supplier.id

    # Serializer exposes supplier + read-only supplier_name.
    r = c_admin.get(f"/api/accounts/users/{vendor.pk}/")
    assert str(r.data["supplier"]) == str(supplier.id)
    assert r.data["supplier_name"] == "Portal Vendor Co"

    # The vendor cannot re-point their own supplier link (silently read-only).
    c_vendor = _client(vendor)
    r = c_vendor.patch(f"/api/accounts/users/{vendor.pk}/", {
        "supplier": str(other.id), "first_name": "Vera",
    }, format="json")
    assert r.status_code == 200, r.content
    vendor.refresh_from_db()
    assert vendor.supplier_id == supplier.id  # unchanged
    assert vendor.first_name == "Vera"  # self-writable field still applied

    # Admin CAN re-point it.
    r = c_admin.patch(f"/api/accounts/users/{vendor.pk}/", {"supplier": str(other.id)}, format="json")
    assert r.status_code == 200, r.content
    vendor.refresh_from_db()
    assert vendor.supplier_id == other.id


# ---------------------------------------------------------------------------
# Authority matrix, as signed off with the client organogram: the Group Head
# is the apex of the organisation and holds complete rights. There is no
# Finance position on the org chart, so finance authority sits with them too.
# ---------------------------------------------------------------------------
@pytest.fixture
def group_head(db):
    return User.objects.create_user(username="acc-gh", password="x", role="group_head")


@pytest.mark.django_db
def test_user_administration_is_the_super_admins_alone(group_head):
    """Client's signed matrix, gate 9: logins and roles are Super Admin only."""
    c = _client(group_head)
    r = c.post("/api/accounts/users/", {
        "username": "gh-created", "password": "Str0ng-Pass!23", "role": "technician",
    }, format="json")
    assert r.status_code == 403, r.content
    assert not User.objects.filter(username="gh-created").exists()

    admin = User.objects.create_user(username="acc-sa", password="x", role="super_admin")
    r = _client(admin).post("/api/accounts/users/", {
        "username": "sa-created", "password": "Str0ng-Pass!23", "role": "technician",
    }, format="json")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_operations_raise_purchase_orders_and_the_group_head_signs_them(group_head):
    """Client decision 2: procurement rights sit with Operations; the Group
    Head's part is the sign-off. Neither can do the other's half."""
    from apps.suppliers.models import Supplier

    supplier = Supplier.objects.create(name="GH Authority Supplier")
    payload = {
        "supplier": str(supplier.id),
        "items": [{"description": "Cable", "quantity": 2, "unit_price": "100.00"}],
    }
    # The Group Head does not raise orders…
    r = _client(group_head).post("/api/procurement/purchase-orders/", payload, format="json")
    assert r.status_code == 403, r.content

    ops = User.objects.create_user(username="acc-ops-po", password="x", role="ops_manager")
    r = _client(ops).post("/api/procurement/purchase-orders/", payload, format="json")
    assert r.status_code == 201, r.content
    po_id = r.data["id"]
    r = _client(ops).post(f"/api/procurement/purchase-orders/{po_id}/transition/",
                          {"status": "pending_approval"}, format="json")
    assert r.status_code == 200, r.content

    # …and Operations do not approve them.
    r = _client(ops).post(f"/api/procurement/purchase-orders/{po_id}/transition/",
                          {"status": "approved"}, format="json")
    assert r.status_code == 403, r.content
    r = _client(group_head).post(f"/api/procurement/purchase-orders/{po_id}/transition/",
                                 {"status": "approved"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "approved"


@pytest.mark.django_db
def test_group_head_approves_a_budget_someone_else_submitted(group_head):
    """With no Finance position, budget sign-off rests with the Group Head."""
    from apps.teams.models import Project, ProjectBudget, ProjectCostLine

    ops = User.objects.create_user(username="acc-ops", password="x", role="ops_manager")
    project = Project.objects.create(name="Authority Matrix Project")
    # An estimate of zero has nothing to approve.
    ProjectCostLine.objects.create(project=project, cost_type="Travelling", quantity=1, unit_cost=1000)

    r = _client(ops).post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json")
    assert r.status_code == 200, r.content

    r = _client(group_head).post(
        f"/api/teams/projects/{project.id}/approve-budget/", {"notes": "ok"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert ProjectBudget.objects.get(project=project).status == "approved"


@pytest.mark.django_db
def test_four_eyes_rule_still_applies_to_the_group_head(group_head):
    """Complete rights are not a licence to approve your own submission."""
    from apps.teams.models import Project, ProjectCostLine

    project = Project.objects.create(name="Self Approval Project")
    ProjectCostLine.objects.create(project=project, cost_type="Travelling", quantity=1, unit_cost=1000)
    c = _client(group_head)
    assert c.post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json").status_code == 200

    r = c.post(f"/api/teams/projects/{project.id}/approve-budget/", {"notes": "mine"}, format="json")
    assert r.status_code == 403
    assert "someone else has to approve" in str(r.data)
