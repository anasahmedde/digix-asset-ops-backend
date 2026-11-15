from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import FinancialViewSet, InvoiceViewSet, PaymentViewSet

router = DefaultRouter()
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("payments", PaymentViewSet, basename="payment")

urlpatterns = [
    path("overview/", FinancialViewSet.as_view({"get": "overview"}), name="finance-overview"),
    path("payables/", FinancialViewSet.as_view({"get": "payables"}), name="finance-payables"),
    path("payables/<uuid:pk>/due-date/", FinancialViewSet.as_view({"post": "due_date"}), name="finance-payable-due"),
    path("receivables/", FinancialViewSet.as_view({"get": "receivables"}), name="finance-receivables"),
    path("inventory/", FinancialViewSet.as_view({"get": "inventory"}), name="finance-inventory"),
    path("inventory/hold/", FinancialViewSet.as_view({"post": "hold"}), name="finance-inventory-hold"),
    path("", include(router.urls)),
]
