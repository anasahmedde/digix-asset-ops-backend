from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TicketAttachmentViewSet, TicketViewSet

router = DefaultRouter()
router.register("", TicketViewSet, basename="ticket")
router.register("attachments", TicketAttachmentViewSet, basename="ticket-attachment")

urlpatterns = [
    path("", include(router.urls)),
]
