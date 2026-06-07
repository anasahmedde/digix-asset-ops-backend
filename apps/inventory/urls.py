from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import InventoryItemViewSet, StockMovementViewSet

router = DefaultRouter()
router.register("items", InventoryItemViewSet, basename="item")
router.register("movements", StockMovementViewSet, basename="movement")

urlpatterns = [
    path("", include(router.urls)),
]
