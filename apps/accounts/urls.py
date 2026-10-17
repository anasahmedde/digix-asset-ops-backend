from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AuditLogViewSet,
    CapabilityCatalogueView,
    MenuOrderView,
    RoleDefinitionViewSet,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet)
router.register("audit-logs", AuditLogViewSet)
router.register("roles", RoleDefinitionViewSet, basename="role")

urlpatterns = [
    path("capabilities/", CapabilityCatalogueView.as_view(), name="capability-catalogue"),
    path("menu-order/", MenuOrderView.as_view(), name="menu-order"),
    path("", include(router.urls)),
]
