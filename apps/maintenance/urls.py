from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import MaintenanceRecordViewSet, MaintenanceScheduleViewSet

router = DefaultRouter()
router.register("schedules", MaintenanceScheduleViewSet, basename="schedule")
router.register("records", MaintenanceRecordViewSet, basename="record")

urlpatterns = [
    path("", include(router.urls)),
]
