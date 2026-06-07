import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

django_asgi_app = get_asgi_application()

from apps.chat.routing import websocket_urlpatterns as chat_ws  # noqa: E402
from apps.notifications.routing import websocket_urlpatterns as notification_ws  # noqa: E402
from apps.teams.routing import websocket_urlpatterns as teams_ws  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(URLRouter(teams_ws + notification_ws + chat_ws)),
    }
)
