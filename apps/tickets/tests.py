import io
from datetime import timedelta

import pytest
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.suppliers.models import Supplier
from apps.tickets.models import Ticket, TicketIssueType
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
