from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead, WarehouseWriteElseRead

from .models import (
    BOMAllocation,
    Project,
    ProjectBOMLine,
    ProjectBottleneck,
    ProjectMember,
    ProjectMilestone,
    ProjectScopeItem,
)
from .serializers import (
    ProjectBOMLineSerializer,
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

    @action(detail=True, methods=["get"], url_path="bom-summary")
    def bom_summary(self, request, pk=None):
        """Per-line fulfilment figures + project-level totals for the BOM tab."""
        project = self.get_object()
        lines = []
        totals = {"required": 0, "allocated": 0, "issued": 0, "shortage": 0}
        for line in project.bom_lines.prefetch_related("allocations").all():
            allocated = line.allocated_quantity
            issued = line.issued_quantity
            shortage = line.shortage
            lines.append({
                "id": str(line.id),
                "description": line.description,
                "quantity": line.quantity,
                "allocated_quantity": allocated,
                "issued_quantity": issued,
                "shortage": shortage,
                "unit_price": line.unit_price,
            })
            totals["required"] += line.quantity
            totals["allocated"] += allocated
            totals["issued"] += issued
            totals["shortage"] += shortage
        return Response({"lines": lines, "totals": totals})

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


class ProjectBOMLineViewSet(viewsets.ModelViewSet):
    """BOM lines + their fulfilment actions (allocate / issue).

    Allocation and issuance are warehouse work, so the warehouse role can
    write here even though the rest of the teams app is manager-only.
    """

    queryset = (
        ProjectBOMLine.objects
        .select_related("project", "asset_type", "device_model", "material_type")
        .prefetch_related("allocations")
        .all()
    )
    serializer_class = ProjectBOMLineSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["project"]
    search_fields = ["description"]
    ordering_fields = ["created_at"]

    def _line_response(self, line_pk):
        line = self.get_queryset().get(pk=line_pk)
        return Response(self.get_serializer(line).data)

    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        """Reserve a unique device or a slice of warehouse stock for this line.

        Exactly one of ``device`` / ``inventory_item`` must be given. Devices
        must be in stock and are moved through the status machine
        (in_stock → assigned); stock allocations are guarded against
        over-allocation across every line reserving the same item.
        """
        from apps.assets.models import Device
        from apps.inventory.models import InventoryItem

        line = self.get_object()
        device_id = request.data.get("device")
        item_id = request.data.get("inventory_item")
        if bool(device_id) == bool(item_id):
            return Response(
                {"detail": "Provide exactly one of device or inventory_item."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if device_id:
                try:
                    device = Device.objects.select_for_update().get(pk=device_id)
                except (Device.DoesNotExist, ValueError, ValidationError):
                    return Response({"device": "Device not found."}, status=drf_status.HTTP_400_BAD_REQUEST)
                if device.status != Device.Status.IN_STOCK:
                    return Response(
                        {"device": f"Device must be in stock to allocate (currently {device.status})."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
                # Flip through the Wave-1 status machine so the lifecycle
                # journal + audit trail record who allocated it and why.
                device.status = Device.Status.ASSIGNED
                device.project = line.project
                device._transition_user = request.user
                device._transition_reason = f"Allocated to project {line.project.name}"
                device.save(update_fields=["status", "project", "updated_at"])
                BOMAllocation.objects.create(
                    bom_line=line, device=device, quantity=1, allocated_by=request.user
                )
            else:
                try:
                    item = InventoryItem.objects.select_for_update().get(pk=item_id)
                except (InventoryItem.DoesNotExist, ValueError, ValidationError):
                    return Response(
                        {"inventory_item": "Inventory item not found."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
                try:
                    quantity = int(request.data.get("quantity"))
                except (TypeError, ValueError):
                    return Response(
                        {"quantity": "Quantity is required for stock allocations."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
                if quantity <= 0:
                    return Response(
                        {"quantity": "Quantity must be a positive integer."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
                # Un-issued allocations across ALL lines still reserve stock;
                # issued ones already decremented the physical quantity.
                reserved = (
                    BOMAllocation.objects
                    .filter(inventory_item=item, status=BOMAllocation.Status.ALLOCATED)
                    .aggregate(total=Sum("quantity"))["total"] or 0
                )
                available = item.quantity - reserved
                if quantity > available:
                    return Response(
                        {"quantity": f"Only {max(0, available)} unit(s) available (in stock minus reservations)."},
                        status=drf_status.HTTP_400_BAD_REQUEST,
                    )
                BOMAllocation.objects.create(
                    bom_line=line, inventory_item=item, quantity=quantity, allocated_by=request.user
                )

        return self._line_response(line.pk)

    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        """Issue a stock allocation out of the warehouse to the project.

        Creates an Issuance through the shared atomic decrement +
        StockMovement OUT path and flips the allocation to ``issued``.
        Device allocations are rejected — devices are issued through
        installation.
        """
        from apps.inventory.models import InventoryItem, Issuance
        from apps.inventory.services import apply_issuance_stock_out

        line = self.get_object()
        alloc_id = request.data.get("allocation")
        if not alloc_id:
            return Response({"allocation": "This field is required."}, status=drf_status.HTTP_400_BAD_REQUEST)
        try:
            allocation = line.allocations.get(pk=alloc_id)
        except (BOMAllocation.DoesNotExist, ValueError, ValidationError):
            return Response(
                {"allocation": "Allocation not found on this BOM line."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if allocation.device_id:
            return Response(
                {"detail": "devices are issued through installation"},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if allocation.status != BOMAllocation.Status.ALLOCATED:
            return Response(
                {"allocation": f"Allocation is already {allocation.status}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if not allocation.inventory_item_id:
            return Response(
                {"allocation": "Allocation has no inventory item."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            item = InventoryItem.objects.select_for_update().get(pk=allocation.inventory_item_id)
            if allocation.quantity > item.quantity:
                return Response(
                    {"quantity": f"Only {item.quantity} unit(s) of {item} in stock."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            issuance = Issuance.objects.create(
                item=item,
                quantity=allocation.quantity,
                issued_to_project=line.project,
                bom_line=line,
                issued_to_site=line.project.site,
                issued_by=request.user,
                reason=f"BOM issue for project {line.project.name}",
            )
            apply_issuance_stock_out(issuance, request.user)
            allocation.status = BOMAllocation.Status.ISSUED
            allocation.save(update_fields=["status", "updated_at"])

        return self._line_response(line.pk)
