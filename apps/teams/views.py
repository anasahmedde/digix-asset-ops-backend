from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import Project, ProjectBottleneck, ProjectMember
from .serializers import (
    ProjectBottleneckSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectMemberSerializer,
)


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "client", "site", "manager"]
    search_fields = ["name", "location", "description"]
    ordering_fields = ["created_at", "start_date", "target_date", "progress"]

    def get_queryset(self):
        return (
            Project.objects
            .select_related("client", "site", "manager")
            .annotate(bottleneck_count=Count("bottlenecks", filter=Q(bottlenecks__is_resolved=False)))
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
        flagged = list(
            qs.filter(status__in=["at_risk", "delayed"])
            .annotate(bottleneck_count=Count("bottlenecks", filter=Q(bottlenecks__is_resolved=False)))
            .values("id", "name", "progress", "status", "bottleneck_count")[:8]
        )
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
