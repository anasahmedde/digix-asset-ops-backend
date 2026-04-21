from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DeviceInstallationViewSet, InstallationPhotoViewSet, SiteViewSet, SiteZoneViewSet

router = DefaultRouter()
router.register("sites", SiteViewSet, basename="site")
router.register("zones", SiteZoneViewSet)
router.register("installations", DeviceInstallationViewSet)
router.register("installation-photos", InstallationPhotoViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
