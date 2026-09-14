"""Expo push-notification delivery.

Sends OS-level push notifications to a user's registered devices via the Expo
push service. Delivery runs in a background thread so it never blocks the
request/response cycle, and failures are logged rather than raised.
"""
from __future__ import annotations

import logging
import threading

import requests

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _deliver(tokens: list[str], title: str, body: str, data: dict) -> None:
    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "data": data,
            "sound": "default",
            "channelId": "default",
            "priority": "high",
        }
        for token in tokens
    ]
    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning("Expo push failed (%s): %s", resp.status_code, resp.text[:500])
        else:
            logger.info("Expo push sent to %d device(s)", len(tokens))
    except Exception:  # pragma: no cover - network best-effort
        logger.exception("Expo push request errored")


def send_push_to_user(user_id, title: str, body: str, data: dict | None = None) -> None:
    """Fire-and-forget: push to every device registered for ``user_id``."""
    # Imported here to avoid app-loading order issues.
    from .models import PushToken

    if not user_id:
        return
    tokens = list(
        PushToken.objects.filter(user_id=user_id).values_list("token", flat=True)
    )
    if not tokens:
        return
    threading.Thread(
        target=_deliver,
        args=(tokens, title, body or "", data or {}),
        daemon=True,
    ).start()
