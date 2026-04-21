from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AuditLogViewSet, UserViewSet

router = DefaultRouter()
router.register("users", UserViewSet)
router.register("audit-logs", AuditLogViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
