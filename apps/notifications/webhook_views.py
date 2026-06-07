from __future__ import annotations

import hmac
import hashlib
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.analytics.models import Alert
from apps.accounts.models import User

from .models import Notification

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = getattr(settings, "WEBHOOK_INGEST_SECRET", "")


def _verify_signature(request) -> bool:
    if not WEBHOOK_SECRET:
        return True
    provided = request.headers.get("X-Webhook-Secret", "")
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


@csrf_exempt
@require_POST
def webhook_ingest(request):
    if not _verify_signature(request):
        return JsonResponse({"error": "Invalid signature"}, status=403)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    event = body.get("event")
    title = body.get("title", "")
    message = body.get("message", "")
    severity = body.get("severity", Alert.Severity.INFO)
    data = body.get("data", {})

    if not event:
        return JsonResponse({"error": "Missing 'event' field"}, status=400)

    if event == "alert.create":
        alert = Alert.objects.create(
            title=title,
            message=message,
            severity=severity,
        )
        return JsonResponse({"status": "ok", "alert_id": str(alert.id)})

    if event == "notification.broadcast":
        recipients = User.objects.filter(is_active=True)
        notifications = [
            Notification(
                recipient=user,
                notification_type=Notification.Type.SYSTEM,
                title=title,
                message=message,
                data=data,
            )
            for user in recipients
        ]
        Notification.objects.bulk_create(notifications)
        return JsonResponse({"status": "ok", "count": len(notifications)})

    return JsonResponse({"error": f"Unknown event: {event}"}, status=400)
