from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DeviceInstallationViewSet,
    InstallationDelayViewSet,
    InstallationPhotoViewSet,
    InstallationStepViewSet,
    SiteContactViewSet,
    SiteViewSet,
    SiteZoneViewSet,
)

router = DefaultRouter()
router.register("sites", SiteViewSet, basename="site")
router.register("site-contacts", SiteContactViewSet, basename="site-contact")
router.register("zones", SiteZoneViewSet)
router.register("installations", DeviceInstallationViewSet)
router.register("installation-steps", InstallationStepViewSet)
router.register("installation-photos", InstallationPhotoViewSet)
router.register("installation-delays", InstallationDelayViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
