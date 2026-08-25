from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    MaintenanceRecordPhotoViewSet,
    MaintenanceRecordViewSet,
    MaintenanceScheduleViewSet,
)

router = DefaultRouter()
router.register("schedules", MaintenanceScheduleViewSet, basename="schedule")
router.register("records", MaintenanceRecordViewSet, basename="record")
router.register("record-photos", MaintenanceRecordPhotoViewSet, basename="record-photo")

urlpatterns = [
    path("", include(router.urls)),
]
