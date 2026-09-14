from __future__ import annotations

import hashlib
import hmac
import json
import logging

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Notification, WebhookEndpoint, WebhookLog

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT = 10


# ── Email channel (XC-02) ─────────────────────────────────────────────

def _notifications_link():
    """Plain link to the web app's notifications page for email bodies."""
    base = getattr(settings, "FRONTEND_URL", "") or (
        settings.CORS_ALLOWED_ORIGINS[0]
        if getattr(settings, "CORS_ALLOWED_ORIGINS", None)
        else "http://localhost:3000"
    )
    return f"{base.rstrip('/')}/notifications"


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_notification_email(self, notification_id: str):
    """Deliver one in-app Notification over email.

    Skips when the recipient has no email address. Transient send failures
    retry (3 × 60s, like webhook delivery); the caller is never broken.
    """
    try:
        notification = Notification.objects.select_related("recipient").get(id=notification_id)
    except Notification.DoesNotExist:
        logger.warning("Notification %s vanished before email delivery", notification_id)
        return False

    email = (notification.recipient.email or "").strip()
    if not email:
        return False

    body = f"{notification.message}\n\nView in DIGIX: {_notifications_link()}"
    try:
        send_mail(
            subject=notification.title,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception as exc:
        logger.exception("Failed to email notification %s to %s", notification_id, email)
        raise self.retry(exc=exc)
    return True


def queue_notification_email(notification):
    """Queue the email for a Notification — never raises and never blocks.

    Publishing goes through a daemon thread (mirroring push.py): with the
    broker down, kombu's connection retries would otherwise stall the calling
    request/beat thread for many seconds per notification.
    """
    import threading

    notification_id = str(notification.id)

    def _publish():
        try:
            send_notification_email.delay(notification_id)
        except Exception:
            logger.exception("Could not queue email for notification %s", notification_id)

    threading.Thread(target=_publish, daemon=True).start()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_webhook_delivery(self, webhook_id: str, event: str, payload: dict):
    try:
        webhook = WebhookEndpoint.objects.get(id=webhook_id, is_active=True)
    except WebhookEndpoint.DoesNotExist:
        logger.warning("Webhook %s not found or inactive, skipping delivery", webhook_id)
        return

    body = json.dumps(payload, default=str)
    headers = {"Content-Type": "application/json"}

    if webhook.secret:
        signature = hmac.new(
            webhook.secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = signature

    response_status = None
    response_body = ""
    success = False

    try:
        resp = requests.post(webhook.url, data=body, headers=headers, timeout=DELIVERY_TIMEOUT)
        response_status = resp.status_code
        response_body = resp.text[:2000]
        success = 200 <= resp.status_code < 300
    except requests.RequestException as exc:
        response_body = str(exc)[:2000]
        logger.exception("Webhook delivery failed for %s", webhook.name)

    WebhookLog.objects.create(
        webhook=webhook,
        event=event,
        payload=payload,
        response_status=response_status,
        response_body=response_body,
        success=success,
    )

    if success:
        WebhookEndpoint.objects.filter(id=webhook_id).update(
            last_triggered=timezone.now(),
            failure_count=0,
        )
    else:
        WebhookEndpoint.objects.filter(id=webhook_id).update(
            failure_count=webhook.failure_count + 1,
        )
        try:
            self.retry()
        except self.MaxRetriesExceededError:
            logger.error("Max retries exceeded for webhook %s", webhook.name)
