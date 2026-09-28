from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    MaintenancePartRequestViewSet,
    MaintenanceRecordPhotoViewSet,
    MaintenanceRecordViewSet,
    MaintenanceScheduleViewSet,
    MaintenanceVisitViewSet,
)

router = DefaultRouter()
router.register("schedules", MaintenanceScheduleViewSet, basename="schedule")
router.register("records", MaintenanceRecordViewSet, basename="record")
router.register("record-photos", MaintenanceRecordPhotoViewSet, basename="record-photo")
router.register("part-requests", MaintenancePartRequestViewSet, basename="part-request")
router.register("visits", MaintenanceVisitViewSet, basename="visit")

urlpatterns = [
    path("", include(router.urls)),
]
