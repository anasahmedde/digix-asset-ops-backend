from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    SupplierContactViewSet,
    SupplierServiceCategoryViewSet,
    SupplierViewSet,
)

router = DefaultRouter()
# Specific prefixes must be registered before the empty-prefix supplier route
# so they are not shadowed by the supplier detail pattern.
router.register("service-categories", SupplierServiceCategoryViewSet, basename="supplier-service-category")
router.register("contacts", SupplierContactViewSet, basename="supplier-contact")
router.register("", SupplierViewSet, basename="supplier")

urlpatterns = [
    path("", include(router.urls)),
]
