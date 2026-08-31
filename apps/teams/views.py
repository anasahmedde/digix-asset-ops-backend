from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import Project, ProjectBottleneck, ProjectMember, ProjectMilestone, ProjectScopeItem
from .serializers import (
    ProjectBottleneckSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectMemberSerializer,
    ProjectMilestoneSerializer,
    ProjectScopeItemSerializer,
)


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "phase", "contract_type", "client", "site", "manager"]
    search_fields = ["name", "location", "description"]
    ordering_fields = ["created_at", "start_date", "target_date", "progress"]

    def get_queryset(self):
        return (
            Project.objects
            .select_related("client", "site", "manager")
            .annotate(bottleneck_count=Count("bottlenecks", filter=Q(bottlenecks__is_resolved=False)))
            .prefetch_related("scope_items", "milestones")
            .all()
        )

    def get_serializer_class(self):
        if self.action == "list":
            return ProjectListSerializer
        return ProjectDetailSerializer

    @action(detail=False, methods=["get"])
    def dashboard_stats(self, request):
        qs = Project.objects.all()
        total = qs.count()
        by_status = dict(qs.values_list("status").annotate(c=Count("id")).values_list("status", "c"))
        flagged = [
            {
                "id": str(p.id),
                "name": p.name,
                "progress": p.computed_progress(),
                "status": p.status,
                "bottleneck_count": p.bottleneck_count,
            }
            for p in (
                qs.filter(status__in=["at_risk", "delayed"])
                .annotate(bottleneck_count=Count("bottlenecks", filter=Q(bottlenecks__is_resolved=False)))
                .prefetch_related("milestones")[:8]
            )
        ]
        top_bottlenecks = list(
            ProjectBottleneck.objects
            .filter(is_resolved=False)
            .values("title")
            .annotate(project_count=Count("project", distinct=True))
            .order_by("-project_count")[:5]
        )
        return Response({
            "total": total,
            "on_track": by_status.get("on_track", 0),
            "at_risk": by_status.get("at_risk", 0),
            "delayed": by_status.get("delayed", 0),
            "completed": by_status.get("completed", 0),
            "flagged_projects": flagged,
            "top_bottlenecks": top_bottlenecks,
        })


class ProjectBottleneckViewSet(viewsets.ModelViewSet):
    queryset = ProjectBottleneck.objects.select_related("project").all()
    serializer_class = ProjectBottleneckSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["project", "severity", "is_resolved"]


class ProjectMemberViewSet(viewsets.ModelViewSet):
    queryset = ProjectMember.objects.select_related("project", "user").all()
    serializer_class = ProjectMemberSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["project", "user", "role"]


class ProjectScopeItemViewSet(viewsets.ModelViewSet):
    queryset = ProjectScopeItem.objects.select_related(
        "project", "device", "component", "site"
    ).all()
    serializer_class = ProjectScopeItemSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["project", "device", "site"]


class ProjectMilestoneViewSet(viewsets.ModelViewSet):
    queryset = ProjectMilestone.objects.select_related("project").all()
    serializer_class = ProjectMilestoneSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["project"]
    ordering_fields = ["order", "due_date"]
