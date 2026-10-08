from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import WarrantyClaimViewSet, WarrantyViewSet

router = DefaultRouter()
router.register("claims", WarrantyClaimViewSet, basename="warranty-claim")
router.register("", WarrantyViewSet, basename="warranty")

urlpatterns = [
    path("", include(router.urls)),
]
