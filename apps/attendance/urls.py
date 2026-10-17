from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AttendanceRecordViewSet, DutyRecordViewSet

router = DefaultRouter()
router.register("records", AttendanceRecordViewSet, basename="attendance-record")
router.register("duty", DutyRecordViewSet, basename="duty-record")

urlpatterns = [
    path("", include(router.urls)),
]
