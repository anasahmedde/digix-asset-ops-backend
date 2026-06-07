from django.contrib import admin

from .models import Project, ProjectBottleneck, ProjectMember


class ProjectMemberInline(admin.TabularInline):
    model = ProjectMember
    extra = 0


class ProjectBottleneckInline(admin.TabularInline):
    model = ProjectBottleneck
    extra = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "status", "progress", "start_date", "target_date", "manager"]
    list_filter = ["status"]
    search_fields = ["name", "location"]
    inlines = [ProjectMemberInline, ProjectBottleneckInline]


@admin.register(ProjectBottleneck)
class ProjectBottleneckAdmin(admin.ModelAdmin):
    list_display = ["project", "title", "severity", "is_resolved"]
    list_filter = ["severity", "is_resolved"]


@admin.register(ProjectMember)
class ProjectMemberAdmin(admin.ModelAdmin):
    list_display = ["project", "user", "role"]
    list_filter = ["role"]
