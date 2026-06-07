from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AssetCodeViewSet,
    BrandViewSet,
    DeviceImageViewSet,
    DeviceLifecycleEventViewSet,
    DeviceModelViewSet,
    DeviceViewSet,
    MaterialTypeViewSet,
)

router = DefaultRouter()
router.register("brands", BrandViewSet)
router.register("device-models", DeviceModelViewSet)
router.register("material-types", MaterialTypeViewSet)
router.register("devices", DeviceViewSet)
router.register("device-images", DeviceImageViewSet)
router.register("lifecycle-events", DeviceLifecycleEventViewSet)
router.register("asset-codes", AssetCodeViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
