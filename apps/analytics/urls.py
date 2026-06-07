from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AlertViewSet, SavedReportViewSet

router = DefaultRouter()
router.register("alerts", AlertViewSet)
router.register("reports", SavedReportViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
