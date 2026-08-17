from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CompanyViewSet,
    EscalationPolicyViewSet,
    NumberingSchemeViewSet,
    PaymentTermsViewSet,
    TermsTemplateViewSet,
    WarrantyPeriodPresetViewSet,
)

router = DefaultRouter()
router.register("company", CompanyViewSet, basename="company")
router.register("numbering-schemes", NumberingSchemeViewSet, basename="numbering-scheme")
router.register("payment-terms", PaymentTermsViewSet, basename="payment-terms")
router.register("terms-templates", TermsTemplateViewSet, basename="terms-template")
router.register("warranty-periods", WarrantyPeriodPresetViewSet, basename="warranty-period")
router.register("escalation-policies", EscalationPolicyViewSet, basename="escalation-policy")

urlpatterns = [
    path("", include(router.urls)),
]
