from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    GoodsReceiptLineViewSet,
    GoodsReceiptViewSet,
    InventoryCategoryViewSet,
    InventoryItemViewSet,
    InventoryUnitTypeViewSet,
    InventoryUnitViewSet,
    IssuanceRequestViewSet,
    LowStockView,
    ReorderRequestViewSet,
    IssuanceViewSet,
    StockMovementViewSet,
)

router = DefaultRouter()
router.register("categories", InventoryCategoryViewSet, basename="inventory-category")
router.register("items", InventoryItemViewSet, basename="item")
router.register("products", InventoryUnitTypeViewSet, basename="inventory-product")
router.register("units", InventoryUnitViewSet, basename="inventory-unit")
router.register("movements", StockMovementViewSet, basename="movement")
router.register("receipts", GoodsReceiptViewSet, basename="goods-receipt")
router.register("receipt-lines", GoodsReceiptLineViewSet, basename="goods-receipt-line")
router.register("issuances", IssuanceViewSet, basename="issuance")
router.register("issuance-requests", IssuanceRequestViewSet, basename="issuance-request")
router.register("reorder-requests", ReorderRequestViewSet, basename="reorder-request")

urlpatterns = [
    path("low-stock/", LowStockView.as_view(), name="low-stock"),
    path("", include(router.urls)),
]
