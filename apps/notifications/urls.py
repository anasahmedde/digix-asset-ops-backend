from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import NotificationViewSet, WebhookEndpointViewSet
from .webhook_views import webhook_ingest

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")
router.register("webhooks", WebhookEndpointViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("webhook/ingest/", webhook_ingest, name="webhook-ingest"),
]
