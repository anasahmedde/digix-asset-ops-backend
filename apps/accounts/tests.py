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
        # An order leaving draft says when the goods are needed by.
        "expected_delivery": (timezone.now().date() + timedelta(days=14)).isoformat(),
        "payment_terms_note": "100% on delivery",
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
    """Complete rights are not a licence to approve your own submission - and
    whoever signs budgets does not submit one for their own signature."""
    from apps.teams.models import Project, ProjectCostLine

    project = Project.objects.create(name="Self Approval Project")
    ProjectCostLine.objects.create(project=project, cost_type="Travelling", quantity=1, unit_cost=1000)
    c = _client(group_head)
    r = c.post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json")
    assert r.status_code == 403 and "approve budgets" in str(r.data)

    ops = User.objects.create_user(username="acc-ops-4eyes", password="x", role="ops_manager")
    assert _client(ops).post(f"/api/teams/projects/{project.id}/submit-budget/", {}, format="json").status_code == 200
    r = _client(ops).post(f"/api/teams/projects/{project.id}/approve-budget/", {"notes": "mine"}, format="json")
    assert r.status_code == 403



def _org():
    """A small reporting line: head -> lead -> tech, and a lead elsewhere."""
    head = User.objects.create_user(username="org-head", password="x", role="group_head")
    lead = User.objects.create_user(username="org-lead", password="x", role="supervisor", reports_to=head)
    tech = User.objects.create_user(username="org-tech", password="x", role="technician", reports_to=lead)
    other = User.objects.create_user(username="org-other-lead", password="x", role="supervisor", reports_to=head)
    return head, lead, tech, other


@pytest.mark.django_db
def test_an_account_is_removed_by_the_super_admin_once_nobody_hangs_from_it(admin):
    head, lead, tech, other = _org()

    # Removing logins is the Super Admin's, as creating them is.
    assert _client(head).delete(f"/api/accounts/users/{tech.pk}/").status_code == 403
    # Nobody removes themselves.
    assert _client(admin).delete(f"/api/accounts/users/{admin.pk}/").status_code == 403
    # Not while somebody still reports to them.
    r = _client(admin).delete(f"/api/accounts/users/{lead.pk}/")
    assert r.status_code == 400, r.content
    assert "direct report" in str(r.data)
    # Somebody with nobody under them: gone.
    assert _client(admin).delete(f"/api/accounts/users/{tech.pk}/").status_code == 204
    assert not User.objects.filter(pk=tech.pk).exists()
    # And now the lead can go too.
    assert _client(admin).delete(f"/api/accounts/users/{lead.pk}/").status_code == 204


@pytest.mark.django_db
def test_only_a_superuser_removes_a_superuser(admin):
    root = User.objects.create_superuser(username="org-root", password="x", email="r@x.pk")
    # A super_admin by role, but not a superuser: not their call.
    assert _client(admin).delete(f"/api/accounts/users/{root.pk}/").status_code == 403
    assert User.objects.filter(pk=root.pk).exists()


@pytest.mark.django_db
def test_the_reporting_line_stays_a_tree_and_is_moved_by_managers():
    head, lead, tech, other = _org()

    # A loop: the lead cannot be hung under their own report.
    r = _client(head).patch(f"/api/accounts/users/{lead.pk}/", {"reports_to": str(tech.pk)}, format="json")
    assert r.status_code == 400, r.content
    assert "loop" in str(r.data["reports_to"])
    # Nobody reports to themselves.
    r = _client(head).patch(f"/api/accounts/users/{lead.pk}/", {"reports_to": str(lead.pk)}, format="json")
    assert r.status_code == 400
    # A supervisor does not hold manage_team, so cannot re-hang anyone.
    r = _client(other).patch(f"/api/accounts/users/{tech.pk}/", {"reports_to": str(other.pk)}, format="json")
    assert r.status_code == 403, r.content
    # Somebody who does hold it is still scoped to the people under them.
    # Given the right to manage people, the other lead still moves only
    # their own team - everybody's chart needs ``act_across_teams``.
    from .models import UserCapability
    UserCapability.objects.create(user=other, capability="manage_team", allowed=True)
    r = _client(other).patch(f"/api/accounts/users/{tech.pk}/", {"reports_to": str(other.pk)}, format="json")
    assert r.status_code == 400, r.content
    assert "report to you" in str(r.data["reports_to"])
    # The head moves anyone.
    r = _client(head).patch(f"/api/accounts/users/{tech.pk}/", {"reports_to": str(other.pk)}, format="json")
    assert r.status_code == 200, r.content
    tech.refresh_from_db()
    assert tech.reports_to_id == other.pk
    # And nobody picks their own manager.
    r = _client(tech).patch(f"/api/accounts/users/{tech.pk}/", {"reports_to": str(head.pk)}, format="json")
    tech.refresh_from_db()
    assert tech.reports_to_id == other.pk


@pytest.mark.django_db
def test_account_administration_stays_with_the_super_admin(group_head, technician):
    """Nobody but the Super Admin switches a login on or off, renames it, or
    resets its password; and manage_logins cannot be handed out through a
    role or an adjustment."""
    from apps.accounts.models import RoleDefinition
    from apps.accounts.roles import ensure_seeded

    c = _client(group_head)
    # The Group Head manages people, but the login itself does not move.
    r = c.patch(f"/api/accounts/users/{technician.pk}/", {"is_active": False, "username": "renamed"}, format="json")
    technician.refresh_from_db()
    assert technician.is_active and technician.username == "acc-tech", r.content
    r = c.post(f"/api/accounts/users/{technician.pk}/reset-password/", {"new_password": "Str0ng-Pass!23"}, format="json")
    assert r.status_code == 403

    # Not through a role...
    ensure_seeded()
    role = RoleDefinition.objects.get(key="group_head")
    r = c.patch(f"/api/accounts/roles/{role.pk}/", {"capabilities": sorted(set(role.capabilities) | {"manage_logins"})}, format="json")
    assert r.status_code == 400 and "Administrator" in str(r.content)
    # ...nor through an adjustment.
    r = c.put(f"/api/accounts/users/{technician.pk}/capabilities/", {
        "overrides": [{"capability": "manage_logins", "allowed": True, "reason": "try"}],
    }, format="json")
    assert r.status_code == 403
    assert not technician.capability_overrides.filter(capability="manage_logins").exists()

    admin = User.objects.create_user(username="acc-sa2", password="x", role="super_admin")
    a = _client(admin)
    r = a.patch(f"/api/accounts/users/{technician.pk}/", {"is_active": False, "username": "renamed"}, format="json")
    assert r.status_code == 200, r.content
    technician.refresh_from_db()
    assert not technician.is_active and technician.username == "renamed"
    r = a.post(f"/api/accounts/users/{technician.pk}/reset-password/", {"new_password": "Str0ng-Pass!23"}, format="json")
    assert r.status_code == 200
    technician.refresh_from_db()
    assert technician.check_password("Str0ng-Pass!23")
    assert "last_login" in a.get(f"/api/accounts/users/{technician.pk}/").data


@pytest.mark.django_db
def test_sign_in_forgives_a_phone_keyboard():
    """A capital first letter and a trailing space are how a phone keyboard
    types a username; the account is still found, and a real mistake still
    says which half was wrong."""
    User.objects.create_user(username="field-sharjeel", password="Pass-1234", role="technician")
    c = APIClient()
    url = "/api/auth/token/"

    for typed in ("field-sharjeel", "Field-Sharjeel ", "  FIELD-SHARJEEL"):
        r = c.post(url, {"username": typed, "password": "Pass-1234"}, format="json")
        assert r.status_code == 200 and r.data["access"] and r.data["refresh"], (typed, r.content)

    r = c.post(url, {"username": "Field-Sharjeel", "password": "wrong"}, format="json")
    assert r.status_code == 401 and r.data["detail"] == "Incorrect password."
    r = c.post(url, {"username": "nobody-here", "password": "Pass-1234"}, format="json")
    assert r.status_code == 401 and r.data["detail"] == "No account found with this username."

    # Two accounts that differ only by case: the typed spelling must be exact.
    User.objects.create_user(username="Field-Sharjeel", password="Other-999", role="technician")
    r = c.post(url, {"username": "FIELD-sharjeel", "password": "Pass-1234"}, format="json")
    assert r.status_code == 401
    r = c.post(url, {"username": "Field-Sharjeel", "password": "Other-999"}, format="json")
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_the_administrator_arranges_the_menu_for_everyone():
    from rest_framework.test import APIClient

    admin = User.objects.create_superuser(username="menu-admin", password="x")
    tech = User.objects.create_user(username="menu-tech", password="x", role="technician")

    def client(u):
        c = APIClient()
        c.force_authenticate(u)
        return c

    assert client(tech).get("/api/accounts/menu-order/").data == {"order": []}, "ships in the default order"
    order = ["/", "/maintenance", "/warranties", "/assets"]
    assert client(tech).put("/api/accounts/menu-order/", {"order": order}, format="json").status_code == 403
    assert client(admin).put("/api/accounts/menu-order/", {"order": "nope"}, format="json").status_code == 400
    r = client(admin).put("/api/accounts/menu-order/", {"order": order + ["/maintenance"]}, format="json")
    assert r.status_code == 200 and r.data["order"] == order, "duplicates dropped"
    assert client(tech).get("/api/accounts/menu-order/").data["order"] == order, "everyone gets it"


@pytest.mark.django_db
def test_a_right_brings_the_prices_it_decides_on():
    """Approving budgets sees project costs; approving orders sees prices -
    saved that way on the role, and held even if withdrawn on its own."""
    from apps.accounts.models import UserCapability
    from apps.accounts.roles import forget

    admin = User.objects.create_superuser(username="req-admin", password="x", role="super_admin")
    c = APIClient()
    c.force_authenticate(admin)
    try:
        r = c.post("/api/accounts/roles/", {"label": "Budget signer", "capabilities": ["approve_budget", "approve_po"]},
                   format="json")
        assert r.status_code == 201, r.content
        assert {"view_projects", "view_project_costs", "view_prices"} <= set(r.data["capabilities"]), "saved with what they need"
        from apps.accounts.capabilities import with_requirements

        assert with_requirements({"record_client_warranty"}) == {"record_client_warranty", "view_projects"}
        assert "view_projects" in with_requirements({"view_project_costs"}), "followed through"
        forget()

        signer = User.objects.create_user(username="req-signer", password="x", role=r.data["key"])
        UserCapability.objects.create(user=signer, capability="view_project_costs", allowed=False)
        assert signer.can("approve_budget") and signer.can("view_project_costs"), "cannot be taken away while approving budgets"
    finally:
        # The rights map is cached; leave it to be read again from a clean database.
        forget()


@pytest.mark.django_db
def test_the_storekeeper_can_name_who_collects_without_the_directory():
    """Issuing stock asks who takes it away. A storekeeper without "See team"
    still gets the names (and only the names); a client login gets nothing."""
    from rest_framework.test import APIClient

    from apps.accounts.models import RoleDefinition

    RoleDefinition.objects.update_or_create(key="qa_store", defaults={"label": "QA Store", "capabilities": ["issue_stock"]})
    keeper = User.objects.create_user(username="names-keeper", password="x", role="qa_store")
    User.objects.create_user(username="names-tech", password="x", role="technician", first_name="Waseem")
    client_login = User.objects.create_user(username="names-client", password="x", role="client_viewer")
    from apps.accounts import roles
    try:
        roles.forget()
        c = APIClient()
        c.force_authenticate(keeper)
        assert c.get("/api/accounts/users/").status_code == 403
        r = c.get("/api/accounts/users/names/")
        assert r.status_code == 200, r.content
        assert {"name": "Waseem", "role": "Technician"}.items() <= next(p for p in r.data if p["name"] == "Waseem").items()
        assert set(r.data[0]) == {"id", "name", "role"}
        assert "names-client" not in [p["name"] for p in r.data]
        c.force_authenticate(client_login)
        assert c.get("/api/accounts/users/names/").status_code == 403
    finally:
        roles.forget()
