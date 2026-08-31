from django.contrib import admin

from .models import BOMAllocation, Project, ProjectBOMLine, ProjectBottleneck, ProjectMember


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


class BOMAllocationInline(admin.TabularInline):
    model = BOMAllocation
    extra = 0


@admin.register(ProjectBOMLine)
class ProjectBOMLineAdmin(admin.ModelAdmin):
    list_display = ["project", "description", "quantity", "unit_price"]
    search_fields = ["description", "project__name"]
    inlines = [BOMAllocationInline]


@admin.register(BOMAllocation)
class BOMAllocationAdmin(admin.ModelAdmin):
    list_display = ["bom_line", "device", "inventory_item", "quantity", "status"]
    list_filter = ["status"]
