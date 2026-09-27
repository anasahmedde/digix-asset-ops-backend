from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AuditLogViewSet,
    CapabilityCatalogueView,
    RoleDefinitionViewSet,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet)
router.register("audit-logs", AuditLogViewSet)
router.register("roles", RoleDefinitionViewSet, basename="role")

urlpatterns = [
    path("capabilities/", CapabilityCatalogueView.as_view(), name="capability-catalogue"),
    path("", include(router.urls)),
]
