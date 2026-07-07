from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import WorkOrderViewSet

router = DefaultRouter()
router.register("", WorkOrderViewSet, basename="work-order")

urlpatterns = [
    path("", include(router.urls)),
]
