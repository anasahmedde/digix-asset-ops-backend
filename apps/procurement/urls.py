from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PurchaseOrderItemViewSet, PurchaseOrderViewSet

router = DefaultRouter()
router.register("orders", PurchaseOrderViewSet, basename="order")
router.register("order-items", PurchaseOrderItemViewSet, basename="order-item")

urlpatterns = [
    path("", include(router.urls)),
]
