from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ProjectBottleneckViewSet, ProjectMemberViewSet, ProjectViewSet

router = DefaultRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("bottlenecks", ProjectBottleneckViewSet)
router.register("members", ProjectMemberViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
