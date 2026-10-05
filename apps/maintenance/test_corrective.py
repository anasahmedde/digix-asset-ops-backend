"""The corrective lifecycle: who may move it, and what each move needs.

A fault is raised as a ticket and worked as a job. These cover the four
things the overhaul was for: the moves are legal only from the right state,
only the right person may make them, a visit cannot be started or finished
without its evidence, and a ticket cannot be raised on an asset that is not
in service.
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import AssetType, Device
from apps.maintenance.models import MaintenanceSchedule, MaintenanceVisit
from apps.tickets.models import Ticket, TicketIssueType


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _png():
    # A one-pixel PNG: enough for ImageField to accept it.
    return SimpleUploadedFile(
        "shot.png",
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
         b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
         b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"),
        content_type="image/png",
    )


@pytest.fixture
def team(db):
    return {
        "office": User.objects.create_user(username="cm-ops", password="x", role="ops_manager"),
        "tech": User.objects.create_user(username="cm-tech", password="x", role="technician", is_field_staff=True),
        "other": User.objects.create_user(username="cm-other", password="x", role="technician", is_field_staff=True),
    }


@pytest.fixture
def live_asset(db):
    kind = AssetType.objects.create(name="CM Screen")
    return Device.objects.create(
        asset_type=kind, asset_code="AST-CM-LIVE", status=Device.Status.ACTIVE,
    )


@pytest.fixture
def fault(db, team, live_asset):
    """A ticket raised on a working asset, and the job it caused."""
    kind, _ = TicketIssueType.objects.get_or_create(name="CM Fault")
    r = _client(team["office"]).post("/api/tickets/", {
        "title": "Panel dark", "device": str(live_asset.id),
        "issue_type": str(kind.id), "category": "repair", "priority": "high",
    }, format="json")
    assert r.status_code == 201, r.content
    ticket = Ticket.objects.get(pk=r.data["id"])
    job = MaintenanceSchedule.objects.get(ticket=ticket, maintenance_type="corrective")
    return {"ticket": ticket, "job": job, **team}


# ── Raising ──────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_a_ticket_is_raised_on_an_asset_that_is_in_service(team, live_asset):
    kind, _ = TicketIssueType.objects.get_or_create(name="CM Fault")
    body = {"title": "x", "issue_type": str(kind.id), "category": "repair", "priority": "high"}
    c = _client(team["office"])

    for state in (Device.Status.PROCURED, Device.Status.IN_STOCK, Device.Status.DECOMMISSIONED):
        live_asset.status = state
        live_asset.save(update_fields=["status"])
        r = c.post("/api/tickets/", {**body, "device": str(live_asset.id)}, format="json")
        assert r.status_code == 400, (state, r.content)
        assert "in use" in str(r.data["device"])

    live_asset.status = Device.Status.ACTIVE
    live_asset.save(update_fields=["status"])
    first = c.post("/api/tickets/", {**body, "device": str(live_asset.id)}, format="json")
    assert first.status_code == 201, first.content

    # Out of service for the fault that is already open: a second report of
    # the same thing is refused, and told where the first one is.
    live_asset.refresh_from_db()
    assert live_asset.status == Device.Status.UNDER_MAINTENANCE
    again = c.post("/api/tickets/", {**body, "device": str(live_asset.id)}, format="json")
    assert again.status_code == 400, again.content
    assert first.data["ticket_number"] in str(again.data["device"])

    # But under maintenance on its own is no bar: a scheduled clean does not
    # mean the screen is not broken. With the fault closed, it reports again.
    from apps.tickets.models import Ticket as _T

    t = _T.objects.get(pk=first.data["id"])
    t.status = _T.Status.CLOSED
    t.save(update_fields=["status"])
    live_asset.status = Device.Status.UNDER_MAINTENANCE
    live_asset.save(update_fields=["status"])
    assert c.post("/api/tickets/", {**body, "device": str(live_asset.id)},
                  format="json").status_code == 201


@pytest.mark.django_db
def test_raising_a_ticket_opens_one_job_in_step_with_it(fault):
    """The job used to be born In Process while the ticket said Open."""
    assert fault["ticket"].status == Ticket.Status.OPEN
    assert fault["job"].status == MaintenanceSchedule.Status.PENDING
    assert fault["job"].ticket_id == fault["ticket"].pk


# ── The state machine, and who may turn it ───────────────────────────────

@pytest.mark.django_db
def test_only_the_office_assigns_and_only_from_the_right_state(fault):
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    url = f"/api/maintenance/schedules/{job.id}/assign/"
    body = {"technician": str(tech.id), "due_date": str(timezone.localdate())}

    assert _client(tech).post(url, body, format="json").status_code == 403

    r = _client(office).post(url, body, format="json")
    assert r.status_code == 200, r.content
    assert r.data["sequence"] == 1 and r.data["status"] == "planned"
    fault["ticket"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.ASSIGNED

    # Nobody has set off, so assigning again moves the same visit.
    r = _client(office).post(url, {**body, "technician": str(fault["other"].id)}, format="json")
    assert r.status_code == 200 and r.data["sequence"] == 1
    assert job.visits.count() == 1


@pytest.mark.django_db
def test_a_visit_cannot_start_without_its_technician_or_a_before_photo(fault):
    job, tech, office, other = fault["job"], fault["tech"], fault["office"], fault["other"]
    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.get()
    start = f"/api/maintenance/visits/{visit.id}/start/"

    # Somebody else's visit.
    assert _client(other).post(start, {}, format="json").status_code == 403
    # No photograph of what was found.
    r = _client(tech).post(start, {}, format="json")
    assert r.status_code == 400 and "BEFORE photo" in str(r.data["detail"])

    up = _client(tech).post(
        f"/api/maintenance/visits/{visit.id}/photos/",
        {"kind": "before", "image": _png()}, format="multipart",
    )
    assert up.status_code == 201, up.content
    assert str(up.data[0]["taken_by"]) == str(tech.id) and up.data[0]["taken_at"]

    r = _client(tech).post(start, {}, format="json")
    assert r.status_code == 200, r.content
    fault["ticket"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.IN_PROGRESS


@pytest.mark.django_db
def test_a_visit_cannot_complete_without_an_after_photo_or_feedback(fault):
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.get()
    photos = f"/api/maintenance/visits/{visit.id}/photos/"
    done = f"/api/maintenance/visits/{visit.id}/complete/"

    # Not started: "Complete" used to be clickable here. The state machine
    # turns it away before the visit's own checks are even reached.
    r = _client(tech).post(done, {"resolved": True}, format="json")
    assert r.status_code == 400
    assert "not a state to complete from" in str(r.data["detail"])

    _client(tech).post(photos, {"kind": "before", "image": _png()}, format="multipart")
    _client(tech).post(f"/api/maintenance/visits/{visit.id}/start/", {}, format="json")

    r = _client(tech).post(done, {"resolved": True}, format="json")
    assert r.status_code == 400 and "AFTER photo" in str(r.data["detail"])

    _client(tech).post(photos, {"kind": "after", "image": _png()}, format="multipart")
    # "Not fixed" has to say why.
    r = _client(tech).post(done, {"resolved": False}, format="json")
    assert r.status_code == 400 and "remarks" in r.data

    r = _client(tech).post(done, {"resolved": True, "remarks": "Driver board swapped"}, format="json")
    assert r.status_code == 200, r.content
    fault["ticket"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.PENDING_REVIEW
    visit.refresh_from_db()
    assert visit.resolved is True and visit.status == MaintenanceVisit.Status.AWAITING_REVIEW


@pytest.mark.django_db
def test_a_super_admin_may_do_what_the_technician_does(fault):
    """It holds every role's rights, so it was odd to be locked out of this.

    The work still belongs to the technician it was given to — this is only
    about who is allowed to press the button.
    """
    job, tech = fault["job"], fault["tech"]
    boss = User.objects.create_user(username="cm-boss", password="x", role="super_admin")
    _client(fault["office"]).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.get()
    photos = f"/api/maintenance/visits/{visit.id}/photos/"

    assert _client(boss).post(photos, {"kind": "before", "image": _png()},
                              format="multipart").status_code == 201
    assert _client(boss).post(f"/api/maintenance/visits/{visit.id}/start/",
                              {}, format="json").status_code == 200
    assert _client(boss).post(photos, {"kind": "after", "image": _png()},
                              format="multipart").status_code == 201
    r = _client(boss).post(f"/api/maintenance/visits/{visit.id}/complete/",
                           {"resolved": True, "remarks": "seen to"}, format="json")
    assert r.status_code == 200, r.content

    visit.refresh_from_db()
    assert visit.status == MaintenanceVisit.Status.AWAITING_REVIEW
    assert visit.assigned_to_id == tech.pk, "the visit is still the technician's"


def _to_review(fault):
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.order_by("-sequence").first()
    p = f"/api/maintenance/visits/{visit.id}/photos/"
    _client(tech).post(p, {"kind": "before", "image": _png()}, format="multipart")
    _client(tech).post(f"/api/maintenance/visits/{visit.id}/start/", {}, format="json")
    _client(tech).post(p, {"kind": "after", "image": _png()}, format="multipart")
    _client(tech).post(
        f"/api/maintenance/visits/{visit.id}/complete/",
        {"resolved": True, "remarks": "done"}, format="json",
    )
    return visit


@pytest.mark.django_db
def test_the_office_accepts_and_the_ticket_closes(fault):
    visit = _to_review(fault)
    url = f"/api/maintenance/visits/{visit.id}/review/"

    assert _client(fault["tech"]).post(url, {"decision": "accepted"}, format="json").status_code == 403

    r = _client(fault["office"]).post(url, {"decision": "accepted"}, format="json")
    assert r.status_code == 200, r.content
    fault["ticket"].refresh_from_db()
    fault["job"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.CLOSED
    assert fault["job"].status == MaintenanceSchedule.Status.COMPLETED


@pytest.mark.django_db
def test_unresolved_opens_the_next_visit_on_the_same_ticket(fault):
    visit = _to_review(fault)
    url = f"/api/maintenance/visits/{visit.id}/review/"
    office, other = fault["office"], fault["other"]

    # The reason, the technician and the date are one decision.
    for missing in (
        {"decision": "unresolved"},
        {"decision": "unresolved", "reason": "Driver board is on order"},
        {"decision": "unresolved", "reason": "Driver board is on order", "technician": str(other.id)},
    ):
        assert _client(office).post(url, missing, format="json").status_code == 400
    # The reason is written out, so whitespace is not a reason.
    assert _client(office).post(url, {
        "decision": "unresolved", "reason": "   ",
        "technician": str(other.id), "next_due": str(timezone.localdate()),
    }, format="json").status_code == 400

    r = _client(office).post(url, {
        "decision": "unresolved", "reason": "Driver board is on order",
        "technician": str(other.id), "next_due": str(timezone.localdate()),
    }, format="json")
    assert r.status_code == 200, r.content
    assert r.data["sequence"] == 2 and str(r.data["assigned_to"]) == str(other.id)

    fault["ticket"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.ASSIGNED
    assert fault["job"].visits.count() == 2
    visit.refresh_from_db()
    assert visit.review_decision == MaintenanceVisit.Review.UNRESOLVED
    assert visit.review_reason == "Driver board is on order" and visit.reviewed_by_id == office.pk


@pytest.mark.django_db
def test_the_clients_are_told_what_may_be_done(fault):
    """Web and mobile render from this rather than keeping their own list."""
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    detail = f"/api/maintenance/schedules/{job.id}/"

    assert _client(office).get(detail).data["allowed_actions"] == ["assign", "cancel"]
    assert _client(tech).get(detail).data["allowed_actions"] == []

    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.get()
    # Still no before photo, so the technician is offered nothing.
    assert "start" not in _client(tech).get(detail).data["allowed_actions"]
    _client(tech).post(
        f"/api/maintenance/visits/{visit.id}/photos/",
        {"kind": "before", "image": _png()}, format="multipart",
    )
    assert "start" in _client(tech).get(detail).data["allowed_actions"]


@pytest.mark.django_db
def test_a_ticket_no_longer_carries_its_own_copy_of_the_work(fault):
    """Assigning on the ticket is what let one repair hold two assignees."""
    r = _client(fault["office"]).post(
        f"/api/tickets/{fault['ticket'].id}/assign/",
        {"assigned_to": str(fault["tech"].id)}, format="json",
    )
    assert r.status_code == 409, r.content
    assert "maintenance job" in str(r.data["detail"])


@pytest.mark.django_db
def test_cancelling_needs_a_reason_and_releases_the_asset(fault):
    job, office = fault["job"], fault["office"]
    url = f"/api/maintenance/schedules/{job.id}/cancel/"

    assert _client(fault["tech"]).post(url, {"reason": "no"}, format="json").status_code == 403
    assert _client(office).post(url, {}, format="json").status_code == 400

    r = _client(office).post(url, {"reason": "Duplicate of an earlier call"}, format="json")
    assert r.status_code == 200, r.content
    fault["ticket"].refresh_from_db()
    assert fault["ticket"].status == Ticket.Status.CANCELLED
    job.device.refresh_from_db()
    assert job.device.status == Device.Status.ACTIVE


@pytest.mark.django_db
def test_a_round_is_worked_the_same_way_without_a_ticket(team):
    """One panel, one set of steps — and no ticket state machine behind it.

    A round is assigned, photographed, started, handed in and accepted
    exactly as a breakdown is. What it does not have is a ticket: nothing
    here moves one, and the two moves that only make sense for a fault —
    sending work back, and cancelling the job — are never offered.
    """
    kind = AssetType.objects.create(name="PM Kind")
    device = Device.objects.create(asset_type=kind, asset_code="AST-PM-1", status=Device.Status.ACTIVE)
    job = MaintenanceSchedule.objects.create(
        title="Monthly clean", device=device, maintenance_type="preventive",
        frequency="monthly", start_date=timezone.localdate(), next_due=timezone.localdate(),
        assigned_to=team["tech"],
    )
    visit = job.open_visit()
    assert visit.status == MaintenanceVisit.Status.PLANNED
    office, tech = team["office"], team["tech"]
    url = f"/api/maintenance/visits/{visit.id}"

    def offered(who):
        return _client(who).get(f"/api/maintenance/schedules/{job.id}/").data["allowed_actions"]

    # Nobody has been given it yet, so only the office has a move.
    assert offered(office) == ["assign"]
    r = _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id)}, format="json")
    assert r.status_code == 200, r.content

    # Starting needs the photograph, the same as a breakdown.
    bare = _client(tech).post(f"{url}/start/", {}, format="json")
    assert bare.status_code == 400 and "BEFORE photo" in str(bare.data["detail"])
    _client(tech).post(f"{url}/photos/", {"kind": "before", "image": _png()}, format="multipart")
    assert offered(tech) == ["start"]
    assert _client(tech).post(f"{url}/start/", {}, format="json").status_code == 200

    # Handing it in needs the other photograph, and goes to the office.
    assert _client(tech).post(f"{url}/complete/", {}, format="json").status_code == 400
    _client(tech).post(f"{url}/photos/", {"kind": "after", "image": _png()}, format="multipart")
    r = _client(tech).post(f"{url}/complete/", {"remarks": "Cleaned and checked"}, format="json")
    assert r.status_code == 200, r.content
    visit.refresh_from_db()
    assert visit.status == MaintenanceVisit.Status.AWAITING_REVIEW

    # The office can send it back, with the same three things a breakdown
    # needs: a reason, somebody to go, and a day to go on.
    assert offered(office) == ["accept", "unresolved"]
    bare = _client(office).post(f"{url}/review/", {"decision": "unresolved"}, format="json")
    assert bare.status_code == 400 and "reason" in bare.data

    # Accepting writes the register and rolls the schedule on.
    was_due = job.next_due
    r = _client(office).post(f"{url}/review/", {"decision": "accepted"}, format="json")
    assert r.status_code == 200, r.content
    visit.refresh_from_db(); job.refresh_from_db()
    assert visit.status == MaintenanceVisit.Status.COMPLETED
    assert visit.record_id is not None and visit.record.notes == "Cleaned and checked"
    assert job.next_due > was_due, "the next round falls due after this one"
    assert job.visits.count() == 2, "and it is open to plan"


@pytest.mark.django_db
def test_a_visit_waits_for_the_parts_it_asked_for(fault):
    """Setting off without the part means a second trip and a half-done job."""
    line_id = _issued_line(fault, quantity=2, issue=False)
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.get()
    url = f"/api/maintenance/visits/{visit.id}"
    _client(tech).post(f"{url}/photos/", {"kind": "before", "image": _png()}, format="multipart")

    def offered():
        return _client(tech).get(f"/api/maintenance/schedules/{job.id}/").data["allowed_actions"]

    # Approved but still on the store's queue: not in hand, so not offered.
    assert "start" not in offered()
    r = _client(tech).post(f"{url}/start/", {}, format="json")
    assert r.status_code == 400 and "not issued yet" in str(r.data["detail"])

    # Once the store hands it over, the visit can go.
    from apps.maintenance.models import MaintenancePartRequest

    issuance = MaintenancePartRequest.objects.get(pk=line_id).issuance_request
    issuance.quantity_issued = 2
    issuance.sync_status()
    issuance.save()
    assert "start" in offered()
    assert _client(tech).post(f"{url}/start/", {}, format="json").status_code == 200


@pytest.mark.django_db
def test_a_round_the_office_is_not_happy_with_goes_back(team):
    """An unsatisfactory round is unsatisfactory whatever raised it."""
    from datetime import timedelta

    kind = AssetType.objects.create(name="PM Back")
    device = Device.objects.create(asset_type=kind, asset_code="AST-PM-2", status=Device.Status.ACTIVE)
    job = MaintenanceSchedule.objects.create(
        title="Quarterly clean", device=device, maintenance_type="preventive",
        frequency="monthly", start_date=timezone.localdate(), next_due=timezone.localdate(),
    )
    office, tech, other = team["office"], team["tech"], team["other"]
    _client(office).post(f"/api/maintenance/schedules/{job.id}/assign/",
                         {"technician": str(tech.id)}, format="json")
    visit = job.open_visit()
    url = f"/api/maintenance/visits/{visit.id}"
    _client(tech).post(f"{url}/photos/", {"kind": "before", "image": _png()}, format="multipart")
    _client(tech).post(f"{url}/start/", {}, format="json")
    _client(tech).post(f"{url}/photos/", {"kind": "after", "image": _png()}, format="multipart")
    _client(tech).post(f"{url}/complete/", {"remarks": "Wiped over"}, format="json")

    was_due = job.next_due
    again = timezone.localdate() + timedelta(days=3)
    r = _client(office).post(f"{url}/review/", {
        "decision": "unresolved", "reason": "Back of the panel was not touched",
        "technician": str(other.id), "next_due": str(again),
    }, format="json")
    assert r.status_code == 200, r.content
    assert r.data["sequence"] == 2 and str(r.data["assigned_to"]) == str(other.id)

    visit.refresh_from_db(); job.refresh_from_db()
    assert visit.review_decision == MaintenanceVisit.Review.UNRESOLVED
    assert visit.review_reason == "Back of the panel was not touched"
    assert visit.record_id is None, "nothing is written to the register until it is accepted"
    assert job.next_due == again and job.next_due != was_due, (
        "the cycle has not been served, so it has not rolled"
    )
    assert job.visits.count() == 2


# ── What the store handed over comes back ────────────────────────────────

def _issued_line(fault, *, quantity, serials=None, unique=False, issue=True):
    """A line asked for, released, and — unless told otherwise — handed over."""
    from apps.assets.models import MaterialType
    from apps.inventory.models import InventoryItem

    body = {"schedule": str(fault["job"].id), "quantity_requested": quantity,
            "name": "Spare"}
    if unique:
        from apps.inventory.models import InventoryUnitType

        body["unit_type"] = str(InventoryUnitType.objects.create(
            type_code="CM-UNIT", name="CM Unit", unit="piece",
        ).pk)
    else:
        material = MaterialType.objects.get_or_create(name="CM Cable", unit="meter")[0]
        body["item"] = str(InventoryItem.objects.create(
            material_type=material, quantity=100).pk)

    asked = _client(fault["tech"]).post(
        "/api/maintenance/part-requests/", body, format="json")
    assert asked.status_code == 201, asked.content
    answered = _client(fault["office"]).post(
        f"/api/maintenance/part-requests/{asked.data['id']}/decide/",
        {"approve": True, "quantity": quantity}, format="json")
    assert answered.status_code == 200, answered.content

    from apps.maintenance.models import MaintenancePartRequest

    line = MaintenancePartRequest.objects.get(pk=asked.data["id"])
    if not issue:
        return str(line.pk)
    issuance = line.issuance_request
    issuance.quantity_issued = quantity
    issuance.issued_serials = serials or []
    issuance.sync_status()
    issuance.save()
    return str(line.pk)


def _ready_to_finish(fault):
    """A visit started and photographed at both ends, waiting to be closed."""
    job, tech, office = fault["job"], fault["tech"], fault["office"]
    _client(office).post(
        f"/api/maintenance/schedules/{job.id}/assign/",
        {"technician": str(tech.id), "due_date": str(timezone.localdate())}, format="json",
    )
    visit = job.visits.order_by("-sequence").first()
    p = f"/api/maintenance/visits/{visit.id}/photos/"
    _client(tech).post(p, {"kind": "before", "image": _png()}, format="multipart")
    _client(tech).post(f"/api/maintenance/visits/{visit.id}/start/", {}, format="json")
    _client(tech).post(p, {"kind": "after", "image": _png()}, format="multipart")
    return visit


@pytest.mark.django_db
def test_a_visit_cannot_be_finished_leaving_issued_parts_unaccounted(fault):
    """The difference between issued and used is not nothing — it is missing."""
    _issued_line(fault, quantity=10)
    visit = _ready_to_finish(fault)

    bare = _client(fault["tech"]).post(
        f"/api/maintenance/visits/{visit.id}/complete/",
        {"resolved": True, "remarks": "done"}, format="json")
    assert bare.status_code == 400, bare.content
    assert "issued component" in str(bare.data["parts_settlement"])


@pytest.mark.django_db
def test_what_a_corrective_visit_did_not_use_goes_back_to_receiving(fault):
    from apps.inventory.models import GoodsReceipt, GoodsReceiptLine
    from apps.maintenance.models import MaintenancePartRequest

    line_id = _issued_line(fault, quantity=10)
    visit = _ready_to_finish(fault)

    r = _client(fault["tech"]).post(
        f"/api/maintenance/visits/{visit.id}/complete/",
        {"resolved": True, "remarks": "Swapped the cable",
         "parts_settlement": [{"part_request": line_id, "used": 6}]},
        format="json")
    assert r.status_code == 200, r.content

    line = MaintenancePartRequest.objects.get(pk=line_id)
    assert (line.quantity_used, line.quantity_returned) == (6, 4)
    assert line.visit_id == visit.pk

    receipt = GoodsReceipt.objects.get(source=GoodsReceipt.Source.MAINTENANCE_RETURN)
    assert r.data["return_grn"] == receipt.grn_number, "the technician is told where it went"
    assert line.return_reference == receipt.grn_number
    back = receipt.lines.get()
    assert back.quantity == 4
    assert back.inspection_status == GoodsReceiptLine.Inspection.PENDING, (
        "a returned part waits for inspection like any delivery"
    )


@pytest.mark.django_db
def test_returned_units_come_back_by_serial_number(fault):
    """Four of a kind is a count; four tracked units are four things."""
    from apps.maintenance.models import MaintenancePartRequest

    line_id = _issued_line(fault, quantity=3, serials=["SN-1", "SN-2", "SN-3"], unique=True)
    visit = _ready_to_finish(fault)
    url = f"/api/maintenance/visits/{visit.id}/complete/"
    body = {"resolved": True, "remarks": "One fitted"}

    # Two are coming back, so two have to be named.
    vague = _client(fault["tech"]).post(
        url, {**body, "parts_settlement": [{"part_request": line_id, "used": 1}]},
        format="json")
    assert vague.status_code == 400
    assert "serial number" in str(vague.data["parts_settlement"])

    # And they have to be ones that actually went out.
    stray = _client(fault["tech"]).post(
        url, {**body, "parts_settlement": [
            {"part_request": line_id, "used": 1, "serials": ["SN-2", "SN-9"]}]},
        format="json")
    assert stray.status_code == 400
    assert "SN-9" in str(stray.data["parts_settlement"])

    ok = _client(fault["tech"]).post(
        url, {**body, "parts_settlement": [
            {"part_request": line_id, "used": 1, "serials": ["SN-2", "SN-3"]}]},
        format="json")
    assert ok.status_code == 200, ok.content
    line = MaintenancePartRequest.objects.get(pk=line_id)
    assert (line.quantity_used, line.quantity_returned) == (1, 2)
