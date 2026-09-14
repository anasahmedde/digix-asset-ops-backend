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
