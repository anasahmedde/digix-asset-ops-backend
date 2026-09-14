from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PurchaseOrderItemViewSet, PurchaseOrderViewSet

router = DefaultRouter()
# Legacy paths — kept for the mobile app.
router.register("orders", PurchaseOrderViewSet, basename="order")
router.register("order-items", PurchaseOrderItemViewSet, basename="order-item")
# Canonical paths used by the web dashboard.
router.register("purchase-orders", PurchaseOrderViewSet, basename="purchase-order")
router.register("purchase-order-items", PurchaseOrderItemViewSet, basename="purchase-order-item")

urlpatterns = [
    path("", include(router.urls)),
]
