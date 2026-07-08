from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    GoodsReceiptViewSet,
    InventoryCategoryViewSet,
    InventoryItemViewSet,
    IssuanceViewSet,
    StockMovementViewSet,
)

router = DefaultRouter()
router.register("categories", InventoryCategoryViewSet, basename="inventory-category")
router.register("items", InventoryItemViewSet, basename="item")
router.register("movements", StockMovementViewSet, basename="movement")
router.register("receipts", GoodsReceiptViewSet, basename="goods-receipt")
router.register("issuances", IssuanceViewSet, basename="issuance")

urlpatterns = [
    path("", include(router.urls)),
]
