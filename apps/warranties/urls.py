from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import WarrantyViewSet

router = DefaultRouter()
router.register("", WarrantyViewSet, basename="warranty")

urlpatterns = [
    path("", include(router.urls)),
]
