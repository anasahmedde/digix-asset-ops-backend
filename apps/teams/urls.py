from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ProjectBOMLineViewSet,
    ProjectCostLineViewSet,
    ProjectMemberViewSet,
    ProjectMilestoneViewSet,
    ProjectScopeItemViewSet,
    ProjectViewSet,
)

router = DefaultRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("members", ProjectMemberViewSet)
router.register("scope-items", ProjectScopeItemViewSet)
router.register("milestones", ProjectMilestoneViewSet)
router.register("bom-lines", ProjectBOMLineViewSet)
router.register("cost-lines", ProjectCostLineViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
