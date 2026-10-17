"""When something happened, as opposed to when the server heard about it.

The field app works without signal: it keeps each act on the phone with the
phone's time and sends it when it can. Such a request carries
``X-Recorded-At``; the middleware below validates it and, for the length of
the request, ``now()`` answers that time instead of the server's clock. The
places that stamp an act - a check-in, a scan, a photo, a start, a finish -
ask ``now()``, so a visit done at 10:30 and uploaded at 14:05 says 10:30.

``X-Idempotency-Key`` makes a resend harmless: the app retries whatever it
did not hear back about, and a key already answered gets the same answer
again instead of a second check-in or a second photo.
"""
from __future__ import annotations

import contextvars
import json
from datetime import timedelta

from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

_recorded = contextvars.ContextVar("recorded_at", default=None)

# How far back an offline act may be dated, and how far a phone clock may run ahead.
OFFLINE_WINDOW = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)


def now():
    """The time of the act being recorded: the phone's, for an offline upload."""
    return _recorded.get() or timezone.now()


def offline() -> bool:
    """Is this request an act recorded on the phone earlier, without signal?"""
    return _recorded.get() is not None


def offline_note() -> str:
    """' (recorded offline, uploaded 17 Oct 14:05)' - or nothing, online."""
    if not offline():
        return ""
    return f" (recorded offline, uploaded {timezone.localtime(timezone.now()):%d %b %H:%M})"


class FieldSyncMiddleware:
    """Recorded-at times and replay protection for the field app's outbox."""

    WRITES = ("POST", "PUT", "PATCH", "DELETE")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in self.WRITES:
            return self.get_response(request)
        key = (request.headers.get("X-Idempotency-Key") or "").strip()[:64]
        if key:
            from apps.accounts.models import RequestReceipt

            seen = RequestReceipt.objects.filter(key=key).first()
            if seen is not None:
                return HttpResponse(seen.body, status=seen.status_code, content_type="application/json")
        token = None
        stamp = (request.headers.get("X-Recorded-At") or "").strip()
        if stamp:
            when = parse_datetime(stamp)
            if when is None:
                return JsonResponse({"detail": "X-Recorded-At is not a date and time."}, status=400)
            if timezone.is_naive(when):
                when = timezone.make_aware(when, timezone.utc)
            server = timezone.now()
            if when > server + CLOCK_SKEW:
                return JsonResponse({"detail": (
                    "This was recorded with the phone's clock ahead of the real time. "
                    "Set the phone's date and time to automatic and try again."
                )}, status=400)
            if when < server - OFFLINE_WINDOW:
                return JsonResponse({"detail": (
                    "This was recorded offline more than 7 days ago and can no longer be uploaded. "
                    "Ask the office to enter it."
                )}, status=400)
            token = _recorded.set(min(when, server))
        try:
            response = self.get_response(request)
        finally:
            if token is not None:
                _recorded.reset(token)
        if key and 200 <= response.status_code < 300:
            from apps.accounts.models import RequestReceipt

            try:
                body = response.content.decode("utf-8", "replace")
                json.loads(body or "null")
            except (ValueError, AttributeError):
                body = "{}"
            try:
                RequestReceipt.objects.create(key=key, path=request.path[:300], status_code=response.status_code, body=body)
            except IntegrityError:
                pass
        return response
