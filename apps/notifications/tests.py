"""Email channel tests (XC-02, Wave 4)."""
import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.models import Notification
from apps.notifications.tasks import queue_notification_email, send_notification_email


@pytest.fixture
def mailed_tech(db):
    return User.objects.create_user(
        username="mail-tech", password="x", role="technician", email="tech@example.com"
    )


@pytest.mark.django_db
def test_send_notification_email_delivers(mailed_tech):
    notif = Notification.objects.create(
        recipient=mailed_tech,
        notification_type="ticket_escalated",
        title="Ticket escalated: TKT-0001",
        message="Screen down at Mall Entrance — no response within SLA.",
    )
    assert send_notification_email(str(notif.id)) is True
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.subject == "Ticket escalated: TKT-0001"
    assert sent.to == ["tech@example.com"]
    assert "no response within SLA" in sent.body
    assert "http" in sent.body  # plain link line appended


@pytest.mark.django_db
def test_send_notification_email_skips_recipient_without_email(db):
    user = User.objects.create_user(username="mail-none", password="x", role="technician")
    notif = Notification.objects.create(recipient=user, title="Hello", message="m")
    assert send_notification_email(str(notif.id)) is False
    assert mail.outbox == []


@pytest.mark.django_db
def test_queue_helper_never_raises_when_broker_down(mailed_tech, monkeypatch):
    from apps.notifications import tasks

    notif = Notification.objects.create(recipient=mailed_tech, title="Hello", message="m")

    def boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(tasks.send_notification_email, "delay", boom)
    queue_notification_email(notif)  # must swallow the failure


@pytest.mark.django_db
def test_assignment_notifications_queue_email(monkeypatch, mailed_tech):
    """ticket_assigned + installation_assigned notifications hit the email queue."""
    from apps.assets.models import Brand, Device, DeviceModel
    from apps.sites.models import DeviceInstallation, Site
    from apps.tickets.models import Ticket

    queued = []
    monkeypatch.setattr(
        "apps.notifications.tasks.queue_notification_email",
        lambda n: queued.append(n.notification_type),
    )

    Ticket.objects.create(title="Assigned ticket", description="d", assigned_to=mailed_tech)
    assert "ticket_assigned" in queued

    site = Site.objects.create(name="Mail Site", city="Karachi")
    brand = Brand.objects.create(name="MailBrand")
    dm = DeviceModel.objects.create(brand=brand, name="MB-1")
    device = Device.objects.create(device_model=dm, asset_code="AST-MAIL-1", serial_number="MAIL-1")
    DeviceInstallation.objects.create(
        device=device, site=site, installed_by=mailed_tech, installed_at=timezone.now()
    )
    assert "installation_assigned" in queued


@pytest.mark.django_db
def test_calendar_holds_planned_events_and_project_dates_and_reminds_once():
    """A planned event sits beside the project's delivery date for the month,
    and the reminder task tells the owner once, the set days ahead."""
    from datetime import timedelta

    from django.utils import timezone
    from rest_framework.test import APIClient

    from apps.accounts.models import User
    from apps.notifications.models import Notification
    from apps.notifications.tasks import remind_calendar_dates
    from apps.teams.models import Project

    today = timezone.localdate()
    ops = User.objects.create_user(username="cal-ops", password="x", role="ops_manager")
    project = Project.objects.create(name="Cal Project", status="on_track", manager=ops, target_date=today + timedelta(days=2))
    c = APIClient()
    c.force_authenticate(ops)
    r = c.post("/api/notifications/calendar/", {
        "title": "Client walkthrough", "date": (today + timedelta(days=1)).isoformat(),
        "time": "10:30", "project": str(project.pk), "remind_days_before": 1,
    }, format="json")
    assert r.status_code == 201, r.content

    r = c.get("/api/notifications/calendar/agenda/", {"month": today.strftime("%Y-%m")})
    kinds = {(i["kind"], i["title"]) for i in r.data["items"]}
    # Both dates may fall into next month at the month's end; check what fits.
    if (today + timedelta(days=1)).month == today.month:
        assert ("event", "Client walkthrough") in kinds
    if (today + timedelta(days=2)).month == today.month:
        assert ("project_target", "Cal Project - delivery") in kinds

    assert remind_calendar_dates() == 2
    assert Notification.objects.filter(recipient=ops, title__startswith="Reminder: Client walkthrough").count() == 1
    assert Notification.objects.filter(recipient=ops, title__startswith="Cal Project is due").count() == 1
    assert remind_calendar_dates() == 0  # told once


@pytest.mark.django_db
def test_tagging_someone_on_a_team_note_reaches_them_with_a_link():
    from rest_framework.test import APIClient

    from apps.notifications.models import Notification

    author = User.objects.create_user(username="note-author", password="x", role="ops_manager")
    tagged = User.objects.create_user(username="note-tagged", password="x", role="supervisor")
    c = APIClient()
    c.force_authenticate(author)
    r = c.post("/api/notifications/sticky-notes/", {"scope": "team", "body": "@Tagged make the standee", "mentions": [str(tagged.pk)]}, format="json")
    assert r.status_code == 201, r.content
    n = Notification.objects.get(recipient=tagged, ref=f"note:{r.data['id']}")
    assert "tagged you" in n.title and n.link == "/"


@pytest.mark.django_db
def test_opening_something_important_informs_the_line_above():
    """An operations head opens a project: the group head above hears of it
    (for information, nothing asked), the head's peers and the opener do not."""
    from rest_framework.test import APIClient

    from apps.notifications.models import Notification

    top = User.objects.create_user(username="fyi-top", password="x", role="group_head")
    peer = User.objects.create_user(username="fyi-peer", password="x", role="ops_manager", reports_to=top)
    ops = User.objects.create_user(username="fyi-ops", password="x", role="ops_manager", reports_to=top)
    c = APIClient()
    c.force_authenticate(ops)
    r = c.post("/api/teams/projects/", {"name": "FYI Project", "status": "planning"}, format="json")
    assert r.status_code == 201, r.content
    fyi = Notification.objects.filter(ref=f"fyi-project:{r.data['id']}")
    assert [n.recipient.username for n in fyi] == ["fyi-top"]
    n = fyi.get()
    assert n.title == "Project opened: FYI Project" and not n.is_actionable and n.link == f"/projects?project={r.data['id']}"
    assert not Notification.objects.filter(recipient=peer, ref__startswith="fyi-").exists()


@pytest.mark.django_db
def test_waiting_on_names_whose_desk_a_request_is_on():
    """Filed under a ref: the people still holding it; answered: nobody.
    Nothing filed: whoever holds the deciding right."""
    from rest_framework.test import APIClient

    from apps.notifications.service import ask, resolve

    head = User.objects.create_user(username="desk-head", password="x", role="group_head")
    ops = User.objects.create_user(username="desk-ops", password="x", role="ops_manager")
    ask("approve_budget", exclude=[ops], title="Budget needs you", ref="budget:desk-test")
    c = APIClient()
    c.force_authenticate(ops)
    r = c.get("/api/notifications/notifications/waiting-on/", {"ref": "budget:desk-test"})
    assert [p["name"] for p in r.data] == ["desk-head"]
    resolve("budget:desk-test")
    r = c.get("/api/notifications/notifications/waiting-on/", {"ref": "budget:desk-test"})
    assert r.data == []
    r = c.get("/api/notifications/notifications/waiting-on/", {"capability": "approve_po"})
    assert "desk-head" in [p["name"] for p in r.data]


@pytest.mark.django_db
def test_a_note_can_be_ticked_off_by_anyone_on_its_board():
    from rest_framework.test import APIClient

    author = User.objects.create_user(username="tick-author", password="x", role="ops_manager")
    other = User.objects.create_user(username="tick-other", password="x", role="supervisor")
    a = APIClient(); a.force_authenticate(author)
    o = APIClient(); o.force_authenticate(other)
    note = a.post("/api/notifications/sticky-notes/", {"scope": "team", "body": "Order the cable"}, format="json").data
    assert note["done"] is False
    r = o.patch(f"/api/notifications/sticky-notes/{note['id']}/", {"done": True}, format="json")
    assert r.status_code == 200 and r.data["done"] is True and r.data["done_at"]
    r = o.patch(f"/api/notifications/sticky-notes/{note['id']}/", {"body": "changed"}, format="json")
    assert r.status_code == 403
    r = a.patch(f"/api/notifications/sticky-notes/{note['id']}/", {"done": False}, format="json")
    assert r.status_code == 200 and r.data["done"] is False
