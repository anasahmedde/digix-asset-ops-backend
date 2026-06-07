from __future__ import annotations

import hashlib
import hmac
import json
import logging

import requests
from celery import shared_task
from django.utils import timezone

from .models import WebhookEndpoint, WebhookLog

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT = 10


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
