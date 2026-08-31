import io
from datetime import timedelta

import pytest
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.suppliers.models import Supplier
from apps.tickets.models import Ticket, TicketIssueType, add_business_days
from apps.tickets.tasks import escalate_overdue_tickets


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    buf.seek(0)
    buf.name = "proof.png"
    return buf


@pytest.fixture
def people(db):
    return {
        "ops": User.objects.create_user(username="wf-ops", password="x", role="ops_manager"),
        "marketing": User.objects.create_user(username="wf-mkt", password="x", role="marketing"),
        "tech": User.objects.create_user(username="wf-tech", password="x", role="technician"),
    }


@pytest.mark.django_db
def test_issue_types_seeded():
    assert TicketIssueType.objects.count() >= 19
    assert TicketIssueType.objects.filter(name="Module Burnt").exists()


@pytest.mark.django_db
def test_occurrence_and_sla(people):
    from apps.assets.models import Brand, Device, DeviceModel

    model = DeviceModel.objects.create(brand=Brand.objects.create(name="WFB"), name="WF-55")
    device = Device.objects.create(device_model=model, serial_number="WF-SN-1")

    c = _client(people["marketing"])
    ids = []
    for i in range(2):
        r = c.post("/api/tickets/", {"title": f"t{i}", "priority": "critical", "device": str(device.id)}, format="json")
        assert r.status_code == 201, r.content
        ids.append(r.json())
    assert ids[0]["occurrence"] == 1 and ids[1]["occurrence"] == 2
    # critical SLA = 4h
    t = Ticket.objects.get(pk=ids[0]["id"])
    assert t.response_due_at and abs((t.response_due_at - t.created_at) - timedelta(hours=4)) < timedelta(minutes=1)


@pytest.mark.django_db
def test_assignment_is_operations_only(people):
    c_mkt = _client(people["marketing"])
    r = c_mkt.post("/api/tickets/", {"title": "no self-assign", "assigned_to": str(people["tech"].id)}, format="json")
    assert r.status_code == 400  # marketing cannot assign at creation

    r = c_mkt.post("/api/tickets/", {"title": "raise only"}, format="json")
    tid = r.json()["id"]
    r = c_mkt.post(f"/api/tickets/{tid}/assign/", {"assigned_to": str(people["tech"].id)}, format="json")
    assert r.status_code == 403

    c_ops = _client(people["ops"])
    vendor = Supplier.objects.create(name="WF Vendor")
    r = c_ops.post(f"/api/tickets/{tid}/assign/", {"assigned_to": str(people["tech"].id), "assigned_vendor": str(vendor.id)}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["assigned_to"] == str(people["tech"].id)
    assert body["assigned_vendor"] == str(vendor.id)


@pytest.mark.django_db
def test_status_not_writable_via_patch(people):
    c = _client(people["ops"])
    tid = c.post("/api/tickets/", {"title": "patch-guard"}, format="json").json()["id"]
    r = c.patch(f"/api/tickets/{tid}/", {"status": "closed"}, format="json")
    assert r.status_code == 200
    assert Ticket.objects.get(pk=tid).status == "open"  # silently ignored (read-only)


@pytest.mark.django_db
def test_full_meeting_workflow(people):
    ops, mkt, tech = people["ops"], people["marketing"], people["tech"]
    c_ops, c_mkt, c_tech = _client(ops), _client(mkt), _client(tech)

    # Marketing raises; Operations assigns.
    tid = c_mkt.post("/api/tickets/", {"title": "screen down", "priority": "high"}, format="json").json()["id"]
    assert c_ops.post(f"/api/tickets/{tid}/assign/", {"assigned_to": str(tech.id)}, format="json").status_code == 200

    # Technician visits and starts work.
    assert c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json").status_code == 200

    # Finds a bigger issue → seeks ops approval (notes required).
    r = c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "pending_ops_approval"}, format="json")
    assert r.status_code == 400  # notes required
    r = c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "pending_ops_approval", "notes": "Module burnt, needs replacement"}, format="json")
    assert r.status_code == 200

    # Technician cannot self-approve; Ops routes to client approval.
    assert c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json").status_code == 403
    assert c_ops.post(f"/api/tickets/{tid}/transition/", {"status": "pending_client_approval"}, format="json").status_code == 200

    # Technician cannot relay the client decision; Marketing approves the expense.
    assert c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json").status_code == 403
    assert c_mkt.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json").status_code == 200

    # Rectified: completion with parts + proof photo (multipart).
    r = c_tech.post(
        f"/api/tickets/{tid}/submit-completion/",
        {"completion_notes": "Replaced module", "parts_used": "1x P6 module, 2x ribbon cables", "images": [_png()]},
        format="multipart",
    )
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["parts_used"].startswith("1x P6")
    assert any(a["attachment_type"] == "completion" for a in body["attachments"])

    # Ops reviews the rectification; Marketing closes after client sign-off.
    assert c_ops.post(f"/api/tickets/{tid}/review/", {"action": "approve"}, format="json").status_code == 200
    assert c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "closed"}, format="json").status_code == 403
    assert c_mkt.post(f"/api/tickets/{tid}/transition/", {"status": "closed"}, format="json").status_code == 200
    assert Ticket.objects.get(pk=tid).status == "closed"


@pytest.mark.django_db
def test_client_decline_goes_on_hold_then_closable(people):
    ops, mkt, tech = people["ops"], people["marketing"], people["tech"]
    c_ops, c_mkt, c_tech = _client(ops), _client(mkt), _client(tech)

    tid = c_mkt.post("/api/tickets/", {"title": "declined path"}, format="json").json()["id"]
    c_ops.post(f"/api/tickets/{tid}/assign/", {"assigned_to": str(tech.id)}, format="json")
    c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json")
    c_tech.post(f"/api/tickets/{tid}/transition/", {"status": "pending_ops_approval", "notes": "needs parts"}, format="json")
    c_ops.post(f"/api/tickets/{tid}/transition/", {"status": "pending_client_approval"}, format="json")
    r = c_mkt.post(f"/api/tickets/{tid}/transition/", {"status": "on_hold", "notes": "Client declined the expense"}, format="json")
    assert r.status_code == 200, r.content
    r = c_mkt.post(f"/api/tickets/{tid}/transition/", {"status": "closed"}, format="json")
    assert r.status_code == 200
    assert Ticket.objects.get(pk=tid).status == "closed"


@pytest.mark.django_db
def test_escalation_task(people):
    c = _client(people["marketing"])
    tid = c.post("/api/tickets/", {"title": "stale ticket", "priority": "critical"}, format="json").json()["id"]
    Ticket.objects.filter(pk=tid).update(response_due_at=timezone.now() - timedelta(hours=1))

    assert escalate_overdue_tickets() == 1
    t = Ticket.objects.get(pk=tid)
    assert t.escalated and t.escalated_at
    from apps.notifications.models import Notification

    assert Notification.objects.filter(ticket=t, notification_type="ticket_escalated", recipient=people["ops"]).exists()
    # Second run is a no-op (escalate once).
    assert escalate_overdue_tickets() == 0


@pytest.mark.django_db
def test_comment_with_image(people):
    c = _client(people["marketing"])
    tid = c.post("/api/tickets/", {"title": "img comment"}, format="json").json()["id"]
    r = c.post(f"/api/tickets/{tid}/comments/", {"content": "see photo", "image": _png()}, format="multipart")
    assert r.status_code == 201, r.content
    assert r.json()["image"], "image URL missing"
    # text-only and image-only still work; empty rejected
    assert c.post(f"/api/tickets/{tid}/comments/", {"content": "plain"}, format="json").status_code == 201
    r = c.post(f"/api/tickets/{tid}/comments/", {"image": _png()}, format="multipart")
    assert r.status_code == 201
    assert c.post(f"/api/tickets/{tid}/comments/", {"content": ""}, format="json").status_code == 400


@pytest.mark.django_db
def test_technician_sees_only_own_tickets(people):
    ops, mkt, tech = people["ops"], people["marketing"], people["tech"]
    other = User.objects.create_user(username="wf-tech2", password="x", role="technician")

    c_mkt = _client(mkt)
    mine = c_mkt.post("/api/tickets/", {"title": "for tech"}, format="json").json()["id"]
    theirs = c_mkt.post("/api/tickets/", {"title": "for other"}, format="json").json()["id"]
    c_ops = _client(ops)
    c_ops.post(f"/api/tickets/{mine}/assign/", {"assigned_to": str(tech.id)}, format="json")
    c_ops.post(f"/api/tickets/{theirs}/assign/", {"assigned_to": str(other.id)}, format="json")

    c_tech = _client(tech)
    ids = [t["id"] for t in c_tech.get("/api/tickets/").json()["results"]]
    assert mine in ids and theirs not in ids
    # detail access to someone else's ticket is denied too
    assert c_tech.get(f"/api/tickets/{theirs}/").status_code == 404
    # tickets a technician raises themselves stay visible
    raised = c_tech.post("/api/tickets/", {"title": "raised by tech"}, format="json").json()["id"]
    ids = [t["id"] for t in c_tech.get("/api/tickets/").json()["results"]]
    assert raised in ids

    # oversight + marketing see everything
    for c in (c_ops, c_mkt):
        ids = [t["id"] for t in c.get("/api/tickets/").json()["results"]]
        assert mine in ids and theirs in ids


# ── Escalation engine (policy-driven, three triggers) ─────────────────

@pytest.fixture
def heads(db):
    return {
        "group_head": User.objects.create_user(username="esc-gh", password="x", role="group_head"),
        "ops": User.objects.create_user(username="esc-ops", password="x", role="ops_manager"),
        "mkt": User.objects.create_user(username="esc-mkt", password="x", role="marketing"),
    }


def _mk_ticket(reporter, **kw):
    defaults = dict(title="Esc test", description="d", priority="medium", reported_by=reporter)
    defaults.update(kw)
    return Ticket.objects.create(**defaults)


@pytest.mark.django_db
def test_response_sla_escalates_to_group_head(heads):
    from apps.notifications.models import Notification

    t = _mk_ticket(heads["mkt"])
    Ticket.objects.filter(pk=t.pk).update(response_due_at=timezone.now() - timedelta(hours=1))

    assert escalate_overdue_tickets() >= 1
    t.refresh_from_db()
    assert t.escalated is True
    assert Notification.objects.filter(recipient=heads["group_head"], ticket=t, notification_type="ticket_escalated").exists()
    assert Notification.objects.filter(recipient=heads["ops"], ticket=t).exists()

    # one-shot: a second run must not re-escalate
    assert escalate_overdue_tickets() == 0


@pytest.mark.django_db
def test_assignment_sla_escalates_after_24h(heads):
    from apps.notifications.models import Notification

    t = _mk_ticket(heads["mkt"])
    # park it unassigned for 25h; keep response SLA satisfied so only the
    # assignment trigger fires
    Ticket.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=25),
        response_due_at=timezone.now() + timedelta(hours=24),
    )

    assert escalate_overdue_tickets() == 1
    t.refresh_from_db()
    assert t.assignment_escalated is True
    assert t.escalated is False
    assert Notification.objects.filter(recipient=heads["group_head"], ticket=t).exists()


@pytest.mark.django_db
def test_assigned_ticket_not_assignment_escalated(heads, people):
    t = _mk_ticket(heads["mkt"], assigned_to=people["tech"])
    Ticket.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=48),
        response_due_at=timezone.now() + timedelta(hours=24),
    )
    escalate_overdue_tickets()
    t.refresh_from_db()
    assert t.assignment_escalated is False


@pytest.mark.django_db
def test_due_date_escalates_active_only(heads):
    t_active = _mk_ticket(heads["mkt"], due_date=timezone.now().date() - timedelta(days=1))
    t_closed = _mk_ticket(heads["mkt"], due_date=timezone.now().date() - timedelta(days=1))
    Ticket.objects.filter(pk__in=[t_active.pk, t_closed.pk]).update(
        created_at=timezone.now(), response_due_at=timezone.now() + timedelta(hours=24)
    )
    Ticket.objects.filter(pk=t_closed.pk).update(status="closed")

    escalate_overdue_tickets()
    t_active.refresh_from_db(); t_closed.refresh_from_db()
    assert t_active.due_date_escalated is True
    assert t_closed.due_date_escalated is False


@pytest.mark.django_db
def test_assign_action_stamps_assigned_at(heads, people):
    t = _mk_ticket(heads["mkt"])
    c = _client(heads["ops"])
    r = c.post(f"/api/tickets/{t.pk}/assign/", {"assigned_to": str(people["tech"].pk)}, format="json")
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.assigned_to == people["tech"]
    assert t.assigned_at is not None


@pytest.mark.django_db
def test_group_head_can_assign(heads, people):
    t = _mk_ticket(heads["mkt"])
    c = _client(heads["group_head"])
    r = c.post(f"/api/tickets/{t.pk}/assign/", {"assigned_to": str(people["tech"].pk)}, format="json")
    assert r.status_code == 200


# ── TK-01 refinements: resolution SLA due dates + reopen window ────────

@pytest.mark.django_db
def test_due_date_auto_set_per_priority(people):
    c = _client(people["marketing"])
    now = timezone.now()
    expectations = {
        "critical": (now + timedelta(hours=24)).date(),
        "high": (now + timedelta(hours=48)).date(),
        "medium": add_business_days(now.date(), 5),
        "low": add_business_days(now.date(), 10),
    }
    for priority, expected in expectations.items():
        r = c.post("/api/tickets/", {"title": f"sla {priority}", "priority": priority}, format="json")
        assert r.status_code == 201, r.content
        t = Ticket.objects.get(pk=r.json()["id"])
        assert t.due_date == expected, priority


@pytest.mark.django_db
def test_explicit_due_date_not_overridden(people):
    c = _client(people["marketing"])
    explicit = (timezone.now() + timedelta(days=30)).date()
    r = c.post(
        "/api/tickets/",
        {"title": "explicit due", "priority": "critical", "due_date": explicit.isoformat()},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert Ticket.objects.get(pk=r.json()["id"]).due_date == explicit


def test_add_business_days_skips_weekends():
    from datetime import date

    # Friday + 1 business day → Monday; Monday + 5 → next Monday.
    assert add_business_days(date(2026, 8, 28), 1) == date(2026, 8, 31)
    assert add_business_days(date(2026, 8, 31), 5) == date(2026, 9, 7)


def _closed_ticket(people):
    """Marketing raises, Operations closes — returns the ticket id."""
    c_mkt, c_ops = _client(people["marketing"]), _client(people["ops"])
    tid = c_mkt.post("/api/tickets/", {"title": "close me"}, format="json").json()["id"]
    r = c_ops.post(f"/api/tickets/{tid}/transition/", {"status": "closed"}, format="json")
    assert r.status_code == 200, r.content
    return tid


@pytest.mark.django_db
def test_closed_at_stamped_on_close(people):
    tid = _closed_ticket(people)
    t = Ticket.objects.get(pk=tid)
    assert t.status == "closed"
    assert t.closed_at is not None
    assert abs(timezone.now() - t.closed_at) < timedelta(minutes=1)


@pytest.mark.django_db
def test_reopen_within_window_by_ops(people):
    tid = _closed_ticket(people)
    c_ops = _client(people["ops"])

    # Reopen reason is required.
    r = c_ops.post(f"/api/tickets/{tid}/transition/", {"status": "in_progress"}, format="json")
    assert r.status_code == 400

    r = c_ops.post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "Fault recurred on site"},
        format="json",
    )
    assert r.status_code == 200, r.content
    t = Ticket.objects.get(pk=tid)
    assert t.status == "in_progress"
    assert t.closed_at is None  # cleared on reopen
    comment = t.comments.filter(comment_type="status_change").last()
    assert "reopen" in comment.content.lower()
    assert comment.old_status == "closed" and comment.new_status == "in_progress"


@pytest.mark.django_db
def test_reopen_by_reporter_within_window(people):
    tid = _closed_ticket(people)  # reported by marketing
    c_mkt = _client(people["marketing"])
    r = c_mkt.post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "Client says issue is back"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert Ticket.objects.get(pk=tid).status == "in_progress"


@pytest.mark.django_db
def test_reopen_after_window_rejected(people):
    tid = _closed_ticket(people)
    Ticket.objects.filter(pk=tid).update(closed_at=timezone.now() - timedelta(days=8))
    r = _client(people["ops"]).post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "too late"},
        format="json",
    )
    assert r.status_code == 400
    assert Ticket.objects.get(pk=tid).status == "closed"


@pytest.mark.django_db
def test_reopen_window_applies_to_legacy_closed_tickets(people):
    """Rows closed before closed_at existed must not fail open — the window
    falls back to updated_at."""
    tid = _closed_ticket(people)
    Ticket.objects.filter(pk=tid).update(
        closed_at=None, updated_at=timezone.now() - timedelta(days=30)
    )
    r = _client(people["ops"]).post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "way too late"},
        format="json",
    )
    assert r.status_code == 400
    assert Ticket.objects.get(pk=tid).status == "closed"


@pytest.mark.django_db
def test_legacy_closed_ticket_reopens_within_updated_at_window(people):
    tid = _closed_ticket(people)
    Ticket.objects.filter(pk=tid).update(
        closed_at=None, updated_at=timezone.now() - timedelta(days=2)
    )
    r = _client(people["ops"]).post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "fault recurred, legacy row"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert Ticket.objects.get(pk=tid).status == "in_progress"


@pytest.mark.django_db
def test_migration_backfills_closed_at_from_updated_at(people):
    import importlib

    from django.apps import apps as global_apps
    from django.db import connection

    migration = importlib.import_module("apps.tickets.migrations.0012_backfill_closed_at")
    backfill_closed_at = migration.backfill_closed_at

    tid_closed = _closed_ticket(people)
    stamp = timezone.now() - timedelta(days=10)
    Ticket.objects.filter(pk=tid_closed).update(closed_at=None, updated_at=stamp)
    t_open = _mk_ticket(people["marketing"])

    # A real schema_editor can't open inside the test transaction on SQLite;
    # the backfill only reads schema_editor.connection.alias.
    schema_editor_stub = type("SchemaEditorStub", (), {"connection": connection})
    backfill_closed_at(global_apps, schema_editor_stub)

    assert Ticket.objects.get(pk=tid_closed).closed_at == stamp
    assert Ticket.objects.get(pk=t_open.pk).closed_at is None


@pytest.mark.django_db
def test_reopen_denied_for_technician(people):
    c_mkt, c_ops = _client(people["marketing"]), _client(people["ops"])
    tid = c_mkt.post("/api/tickets/", {"title": "close me"}, format="json").json()["id"]
    # Assign the technician so the ticket stays visible to them, then close.
    c_ops.post(f"/api/tickets/{tid}/assign/", {"assigned_to": str(people["tech"].id)}, format="json")
    assert c_ops.post(f"/api/tickets/{tid}/transition/", {"status": "closed"}, format="json").status_code == 200

    # Even the assigned technician cannot reopen — only Operations or the reporter.
    r = _client(people["tech"]).post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "let me back in"},
        format="json",
    )
    assert r.status_code == 403
    assert Ticket.objects.get(pk=tid).status == "closed"

    # A completely unrelated technician cannot even see the ticket.
    other = User.objects.create_user(username="wf-tech-x", password="x", role="technician")
    r = _client(other).post(
        f"/api/tickets/{tid}/transition/",
        {"status": "in_progress", "notes": "n"},
        format="json",
    )
    assert r.status_code == 404


@pytest.fixture
def warranty_device(db):
    from apps.assets.models import Brand, Device, DeviceModel

    brand = Brand.objects.create(name="WtyBrand")
    dm = DeviceModel.objects.create(brand=brand, name="W-1")
    return Device.objects.create(device_model=dm, asset_code="AST-WTY-1", serial_number="WTY-1")


def _make_warranty(device, wtype="client", months=12, start_offset_days=0):
    from datetime import timedelta as td

    from apps.warranties.models import Warranty

    start = timezone.localdate() - td(days=start_offset_days)
    return Warranty.objects.create(
        device=device, warranty_type=wtype, status="active",
        start_date=start, end_date=start + td(days=30 * months), months=months,
    )


def test_billability_defaults_under_client_warranty(people, warranty_device):
    warranty = _make_warranty(warranty_device)
    c = _client(people["marketing"])
    r = c.post("/api/tickets/", {
        "title": "Panel flicker", "category": "repair",
        "device": str(warranty_device.pk), "priority": "high",
    }, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["is_billable"] is False
    assert body["charge_to"] == "company"
    assert body["warranty"] == str(warranty.pk)
    assert body["warranty_info"]["status"] == "active"


def test_billability_defaults_when_no_warranty(people, warranty_device):
    c = _client(people["marketing"])
    r = c.post("/api/tickets/", {
        "title": "Out of cover repair", "category": "repair",
        "device": str(warranty_device.pk),
    }, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["is_billable"] is True
    assert body["charge_to"] == "client"
    assert body["warranty"] is None


def test_billability_vendor_when_supplier_warranty_active(people, warranty_device):
    _make_warranty(warranty_device, wtype="client")
    _make_warranty(warranty_device, wtype="supplier")
    c = _client(people["marketing"])
    r = c.post("/api/tickets/", {
        "title": "Module burnt", "category": "warranty_claim",
        "device": str(warranty_device.pk),
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["charge_to"] == "vendor"


def test_explicit_billability_overrides_defaults(people, warranty_device):
    _make_warranty(warranty_device)
    c = _client(people["marketing"])
    r = c.post("/api/tickets/", {
        "title": "Client-caused damage", "category": "repair",
        "device": str(warranty_device.pk),
        "is_billable": True, "charge_to": "client",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["is_billable"] is True
    assert r.json()["charge_to"] == "client"


def test_warranty_claim_sets_claimed_and_close_restores(people, warranty_device):
    from apps.warranties.models import Warranty

    warranty = _make_warranty(warranty_device)
    c = _client(people["ops"])
    r = c.post("/api/tickets/", {
        "title": "Claim: dead pixels", "category": "warranty_claim",
        "device": str(warranty_device.pk),
    }, format="json")
    assert r.status_code == 201, r.content
    warranty.refresh_from_db()
    assert warranty.status == Warranty.Status.CLAIMED

    ticket_id = r.json()["id"]
    close = c.post(f"/api/tickets/{ticket_id}/transition/", {"status": "closed"}, format="json")
    assert close.status_code == 200, close.content
    warranty.refresh_from_db()
    assert warranty.status == Warranty.Status.ACTIVE


def test_multi_asset_ticket(people, warranty_device):
    from apps.assets.models import Brand, Device, DeviceModel

    brand = Brand.objects.create(name="MABrand")
    dm = DeviceModel.objects.create(brand=brand, name="MA-1")
    second = Device.objects.create(device_model=dm, asset_code="AST-MA-2", serial_number="MA-2")
    c = _client(people["ops"])
    r = c.post("/api/tickets/", {
        "title": "Preventive: adapters batch", "category": "preventive_maintenance",
        "devices": [str(warranty_device.pk), str(second.pk)],
    }, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    # Primary derived from the first linked asset; both linked.
    assert body["device"] == str(warranty_device.pk)
    codes = {d["asset_code"] for d in body["devices_info"]}
    assert codes == {"AST-WTY-1", "AST-MA-2"}

    # ?device= matches via the M2M for the second asset too.
    listing = c.get("/api/tickets/", {"device": str(second.pk)})
    assert listing.status_code == 200
    assert listing.json()["count"] == 1


def test_warranty_must_belong_to_ticket_device(people, warranty_device):
    from apps.assets.models import Brand, Device, DeviceModel

    foreign_brand = Brand.objects.create(name="ForeignBrand")
    foreign_dm = DeviceModel.objects.create(brand=foreign_brand, name="F-1")
    foreign_device = Device.objects.create(
        device_model=foreign_dm, asset_code="AST-F-1", serial_number="F-1"
    )
    foreign_warranty = _make_warranty(foreign_device)
    c = _client(people["tech"])
    r = c.post("/api/tickets/", {
        "title": "Hijack attempt", "category": "warranty_claim",
        "device": str(warranty_device.pk), "warranty": str(foreign_warranty.pk),
    }, format="json")
    assert r.status_code == 400
    assert "warranty" in r.json()
    foreign_warranty.refresh_from_db()
    assert foreign_warranty.status == "active"


def test_device_filter_rejects_malformed_uuid(people):
    r = _client(people["ops"]).get("/api/tickets/", {"device": "not-a-uuid"})
    assert r.status_code == 400


def test_reopen_reclaims_warranty(people, warranty_device):
    from apps.warranties.models import Warranty

    warranty = _make_warranty(warranty_device)
    c = _client(people["ops"])
    ticket_id = c.post("/api/tickets/", {
        "title": "Claim cycle", "category": "warranty_claim",
        "device": str(warranty_device.pk),
    }, format="json").json()["id"]
    c.post(f"/api/tickets/{ticket_id}/transition/", {"status": "closed"}, format="json")
    warranty.refresh_from_db()
    assert warranty.status == Warranty.Status.ACTIVE
    r = c.post(f"/api/tickets/{ticket_id}/transition/", {
        "status": "in_progress", "notes": "closed by mistake",
    }, format="json")
    assert r.status_code == 200, r.content
    warranty.refresh_from_db()
    assert warranty.status == Warranty.Status.CLAIMED


def test_update_keeps_primary_device_linked(people, warranty_device):
    from apps.assets.models import Brand, Device, DeviceModel

    c = _client(people["ops"])
    ticket_id = c.post("/api/tickets/", {
        "title": "Sync check", "category": "inspection",
        "device": str(warranty_device.pk),
    }, format="json").json()["id"]
    brand = Brand.objects.create(name="SyncBrand")
    dm = DeviceModel.objects.create(brand=brand, name="S-1")
    new_primary = Device.objects.create(device_model=dm, asset_code="AST-S-1", serial_number="S-1")
    r = c.patch(f"/api/tickets/{ticket_id}/", {"device": str(new_primary.pk)}, format="json")
    assert r.status_code == 200, r.content
    codes = {d["asset_code"] for d in r.json()["devices_info"]}
    assert "AST-S-1" in codes
