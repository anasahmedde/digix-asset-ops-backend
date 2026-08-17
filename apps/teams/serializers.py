from rest_framework import serializers

from .models import Project, ProjectBottleneck, ProjectMember


class ProjectBottleneckSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectBottleneck
        fields = [
            "id", "project", "title", "severity", "is_resolved", "resolved_at", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ProjectMemberSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)

    class Meta:
        model = ProjectMember
        fields = ["id", "project", "user", "user_name", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class ProjectListSerializer(serializers.ModelSerializer):
    assets_count = serializers.IntegerField(source="devices.count", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottleneck_count = serializers.IntegerField(read_only=True, default=0)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "name", "location", "image", "client", "client_name",
            "site", "site_name", "status", "status_display", "progress",
            "start_date", "target_date", "completed_date",
            "manager", "manager_name", "bottleneck_count", "created_at",
         "assets_count",]


class ProjectDetailSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    manager_name = serializers.CharField(source="manager.get_full_name", read_only=True, default=None)
    bottlenecks = ProjectBottleneckSerializer(many=True, read_only=True)
    members = ProjectMemberSerializer(many=True, read_only=True)

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "location", "image",
            "client", "client_name", "site", "site_name",
            "status", "progress", "start_date", "target_date", "completed_date",
            "manager", "manager_name", "budget", "notes",
            "bottlenecks", "members", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
