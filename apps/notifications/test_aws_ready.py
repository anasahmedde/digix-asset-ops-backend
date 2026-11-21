"""What running behind AWS's load balancer, and a rehearsal on copied data, rely on."""

import pytest

from apps.accounts.models import User
from apps.notifications import push
from apps.notifications.models import PushToken


@pytest.mark.django_db
def test_health_answers_by_container_address_and_needs_the_database(client, settings):
    settings.ALLOWED_HOSTS = ["ops.wizioners.com"]
    # The load balancer calls by the task's private address, not the site name.
    r = client.get("/api/health/", HTTP_HOST="10.0.1.23:8000")
    assert (r.status_code, r.content) == (200, b"ok")
    # Anything else from that address is still refused by the host check.
    assert client.get("/api/", HTTP_HOST="10.0.1.23:8000").status_code == 400


@pytest.mark.django_db
def test_push_off_sends_nothing(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_deliver", lambda *a: sent.append(a))
    monkeypatch.setattr(push.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    u = User.objects.create_user(username="push-u", password="x", role="technician")
    PushToken.objects.create(user=u, token="ExponentPushToken[qa]")

    settings.PUSH_ENABLED = False
    push.send_push_to_user(u.pk, "Job assigned", "Site A")
    assert sent == []

    settings.PUSH_ENABLED = True
    push.send_push_to_user(u.pk, "Job assigned", "Site A")
    assert len(sent) == 1
