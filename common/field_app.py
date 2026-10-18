"""Who may use the field app: the "Use the field app" right.

The phone app says who it is with an ``X-Client: field-app`` header; builds
before that header still announce themselves through Android's network
client (``okhttp/...`` user agent), which no browser sends.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

FIELD_APP_RIGHT = "use_field_app"
REFUSAL = (
    "This account is not set up for the field app. "
    'Ask your administrator to give you "Use the field app" in Roles & Rights.'
)


def from_field_app(request) -> bool:
    agent = request.META.get("HTTP_USER_AGENT", "")
    return request.headers.get("X-Client") == "field-app" or agent.startswith("okhttp")


def may_use_field_app(user) -> bool:
    return bool(getattr(user, "is_superuser", False)) or user.can(FIELD_APP_RIGHT)


class FieldAppJWTAuthentication(JWTAuthentication):
    """The usual token check; from the field app, the right is checked too -
    so taking it away stops a phone that is already signed in."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None and from_field_app(request) and not may_use_field_app(result[0]):
            raise PermissionDenied(REFUSAL)
        return result
