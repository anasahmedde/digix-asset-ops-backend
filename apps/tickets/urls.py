from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TicketAttachmentViewSet, TicketIssueTypeViewSet, TicketViewSet

router = DefaultRouter()
# Specific prefixes must be registered before the empty-prefix ticket route.
router.register("issue-types", TicketIssueTypeViewSet, basename="ticket-issue-type")
router.register("attachments", TicketAttachmentViewSet, basename="ticket-attachment")
router.register("", TicketViewSet, basename="ticket")

urlpatterns = [
    path("", include(router.urls)),
]
