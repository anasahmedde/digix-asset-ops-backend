from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from common.permissions import (
    WAREHOUSE_ROLES,
    InspectionWriteElseRead,
    WarehouseWriteElseRead,
)

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    InventoryCategory,
    InventoryItem,
    InventoryUnit,
    InventoryUnitType,
    Issuance,
    IssuanceRequest,
    StockMovement,
)
from .serializers import (
    GoodsReceiptLineInspectSerializer,
    GoodsReceiptLineSerializer,
    GoodsReceiptSerializer,
    InventoryCategorySerializer,
    InventoryItemSerializer,
    InventoryUnitBulkSerializer,
    InventoryUnitSerializer,
    InventoryUnitTransitionSerializer,
    InventoryUnitTypeSerializer,
    IssuanceRequestIssueSerializer,
    IssuanceRequestSerializer,
    IssuanceSerializer,
    StockMovementSerializer,
)
from .services import apply_issuance_stock_out, issue_against_request, stock_inspected_line


class InventoryCategoryViewSet(viewsets.ModelViewSet):
    queryset = InventoryCategory.objects.all()
    serializer_class = InventoryCategorySerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class InventoryItemViewSet(viewsets.ModelViewSet):
    # Coalesce so unpriced items sort as zero value instead of NULLs-first.
    queryset = (
        InventoryItem.objects.select_related("material_type", "category")
        .annotate(
            total_value=Coalesce(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                Value(0),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )
        .all()
    )
    serializer_class = InventoryItemSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["location", "category", "material_type"]
    search_fields = ["sku", "material_type__name", "category__name"]
    ordering_fields = ["quantity", "total_value", "material_type__name", "created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        # ?low_stock=true|false — quantity at/below the min stock level (or
        # its complement). Handled here so list AND export share it; unknown
        # values are ignored.
        low_stock = self.request.query_params.get("low_stock")
        if low_stock is not None:
            value = low_stock.strip().lower()
            if value in ("true", "1"):
                qs = qs.filter(quantity__lte=F("min_stock_level"))
            elif value in ("false", "0"):
                qs = qs.filter(quantity__gt=F("min_stock_level"))
        return qs

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of in-hand stock — filter-aware (XC-01)."""
        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "SKU", "Material", "Category", "Quantity", "Min Stock Level",
            "Location", "Unit Cost", "Low Stock",
        ]
        rows = []
        for item in qs:
            rows.append([
                item.sku,
                item.material_type.name if item.material_type_id else "",
                item.category.name if item.category_id else "",
                item.quantity,
                item.min_stock_level,
                item.get_location_display(),
                item.unit_cost,
                item.is_low_stock,
            ])
        log_export(request.user, "inventory_item", len(rows), export_params(request))
        return xlsx_response("inventory", "Inventory", columns, rows)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """In-hand stock totals for dashboard widgets.

        ``total_value`` only counts items with a known unit_cost; ``unpriced_items``
        tells the client how many items are excluded from the valuation.
        """
        agg = InventoryItem.objects.aggregate(
            items=Count("id"),
            total_quantity=Sum("quantity"),
            total_value=Sum(
                ExpressionWrapper(
                    F("quantity") * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                filter=Q(unit_cost__isnull=False),
            ),
            unpriced_items=Count("id", filter=Q(unit_cost__isnull=True)),
            low_stock=Count("id", filter=Q(quantity__lte=F("min_stock_level"))),
        )
        return Response({
            "items": agg["items"] or 0,
            "total_quantity": agg["total_quantity"] or 0,
            "total_value": agg["total_value"] or 0,
            "unpriced_items": agg["unpriced_items"] or 0,
            "low_stock": agg["low_stock"] or 0,
        })


class InventoryUnitTypeViewSet(viewsets.ModelViewSet):
    """Unique products: opened here with their details, stocked later."""

    queryset = InventoryUnitType.objects.select_related(
        "material_type", "category", "brand", "supplier"
    ).annotate(
        stock_count=Count("units", filter=Q(units__status=InventoryUnit.Status.IN_STOCK))
    ).all()
    serializer_class = InventoryUnitTypeSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = [
        "is_active", "category", "material_type", "brand", "supplier", "is_high_value",
    ]
    search_fields = ["type_code", "name", "model_name", "brand__name", "material_type__name"]
    ordering_fields = ["name", "created_at", "stock_count"]

    @action(detail=True, methods=["get"])
    def units(self, request, pk=None):
        """The physical units registered against this product."""
        product = self.get_object()
        qs = product.units.select_related("supplier", "goods_receipt_line__receipt").all()
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(InventoryUnitSerializer(page, many=True).data)
        return Response(InventoryUnitSerializer(qs, many=True).data)


class InventoryUnitViewSet(viewsets.ModelViewSet):
    """Unique (serialized) inventory items — the counterpart to InventoryItem."""

    queryset = InventoryUnit.objects.select_related(
        "material_type", "category", "brand", "supplier", "converted_device", "unit_type",
        "goods_receipt_line__receipt__purchase_order",
        # Where the unit ended up — one query, not one per row.
        "asset_component__device",
    ).all()
    serializer_class = InventoryUnitSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["status", "location", "category", "material_type", "brand", "supplier", "has_warranty", "unit_type"]
    search_fields = ["unit_code", "serial_number", "model_name", "batch_number", "material_type__name", "brand__name"]
    ordering_fields = ["created_at", "serial_number", "purchase_date", "warranty_end"]

    def get_queryset(self):
        qs = super().get_queryset()
        # ?warranty=active|expired|none — computed from warranty_end, so it is
        # applied here rather than via filterset_fields.
        warranty = self.request.query_params.get("warranty")
        if warranty:
            today = timezone.now().date()
            value = warranty.strip().lower()
            if value == "active":
                qs = qs.filter(has_warranty=True, warranty_end__gte=today)
            elif value == "expired":
                qs = qs.filter(has_warranty=True, warranty_end__lt=today)
            elif value == "none":
                qs = qs.filter(Q(has_warranty=False) | Q(warranty_end__isnull=True))
        return qs

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk(self, request):
        """Register N identical unique items at once — one row per unit."""
        serializer = InventoryUnitBulkSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            units = serializer.save()
        return Response(
            {"created": len(units), "units": InventoryUnitSerializer(units, many=True).data},
            status=201,
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Guarded status change — mirrors the Device/Ticket transition action."""
        unit = self.get_object()
        serializer = InventoryUnitTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]

        if new_status == unit.status:
            return Response({"detail": "Unit is already in that status."}, status=400)
        if not unit.can_transition_to(new_status):
            allowed = ", ".join(unit.VALID_TRANSITIONS.get(unit.status, ())) or "none"
            return Response(
                {"detail": f"Cannot move from '{unit.status}' to '{new_status}'. Allowed: {allowed}."},
                status=400,
            )

        unit.status = new_status
        if serializer.validated_data.get("notes"):
            unit.notes = serializer.validated_data["notes"]
        unit.save(update_fields=["status", "notes", "updated_at"])
        return Response(InventoryUnitSerializer(unit).data)

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of unique items — filter-aware (XC-01)."""
        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Unit Code", "Serial No", "Material", "Category", "Make", "Model",
            "Status", "Location", "Supplier", "Purchase Date", "Purchase Price",
            "Batch No", "Warranty", "Warranty Type", "Warranty Start", "Warranty End",
        ]
        rows = []
        for unit in qs:
            rows.append([
                unit.unit_code,
                unit.serial_number,
                unit.material_type.name if unit.material_type_id else "",
                unit.category.name if unit.category_id else "",
                unit.brand.name if unit.brand_id else "",
                unit.model_name,
                unit.get_status_display(),
                unit.get_location_display(),
                unit.supplier.name if unit.supplier_id else "",
                unit.purchase_date,
                unit.purchase_price,
                unit.batch_number,
                unit.warranty_state,
                unit.get_warranty_type_display() if unit.warranty_type else "",
                unit.warranty_start,
                unit.warranty_end,
            ])
        log_export(request.user, "inventory_unit", len(rows), export_params(request))
        return xlsx_response("unique-items", "Unique Items", columns, rows)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the Unique Items tab header."""
        today = timezone.now().date()
        agg = InventoryUnit.objects.aggregate(
            units=Count("id"),
            in_stock=Count("id", filter=Q(status=InventoryUnit.Status.IN_STOCK)),
            issued=Count("id", filter=Q(status=InventoryUnit.Status.ISSUED)),
            under_warranty=Count("id", filter=Q(has_warranty=True, warranty_end__gte=today)),
            warranty_expired=Count("id", filter=Q(has_warranty=True, warranty_end__lt=today)),
            no_warranty=Count("id", filter=Q(has_warranty=False) | Q(warranty_end__isnull=True)),
            total_value=Sum("purchase_price", filter=Q(purchase_price__isnull=False)),
        )
        return Response({key: agg[key] or 0 for key in agg})


class GoodsReceiptLineViewSet(viewsets.ReadOnlyModelViewSet):
    """Received lines and their inspection gate.

    Goods land here on receipt and wait for a technician to inspect them —
    nothing is in the warehouse until ``inspect`` routes it there.
    """

    queryset = (
        GoodsReceiptLine.objects.select_related(
            "receipt", "receipt__purchase_order", "receipt__purchase_order__supplier",
            "po_item", "po_item__material_type", "po_item__device_model",
            "inventory_item__material_type", "inspected_by",
        )
        .prefetch_related("units")
        .all()
    )
    serializer_class = GoodsReceiptLineSerializer
    permission_classes = [IsAuthenticated, InspectionWriteElseRead]
    filterset_fields = ["inspection_status", "routed_to", "receipt", "po_item"]
    search_fields = [
        "batch_number", "receipt__grn_number", "po_item__description",
        "receipt__purchase_order__po_number",
    ]
    ordering_fields = ["created_at", "inspected_at"]

    @action(detail=False, methods=["get"], url_path="pending")
    def pending(self, request):
        """The technician's inspection queue."""
        qs = self.filter_queryset(
            self.get_queryset().filter(inspection_status=GoodsReceiptLine.Inspection.PENDING)
        )
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=True, methods=["post"])
    def inspect(self, request, pk=None):
        """Record the verdict and route accepted goods into inventory."""
        line = self.get_object()
        serializer = GoodsReceiptLineInspectSerializer(
            data=request.data, context={"line": line, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            result = stock_inspected_line(
                line,
                user=request.user,
                route=data.get("route", ""),
                accepted_quantity=data["accepted_quantity"],
                rejected_quantity=data.get("rejected_quantity", 0),
                notes=data.get("notes", ""),
                generic=data.get("generic"),
                units=data.get("units"),
            )

        line.refresh_from_db()
        return Response({
            "line": GoodsReceiptLineSerializer(line).data,
            "stocked_item": (
                InventoryItemSerializer(result["inventory_item"]).data
                if result["inventory_item"] else None
            ),
            "stocked_units": InventoryUnitSerializer(result["units"], many=True).data,
        })


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.select_related(
        "item", "performed_by", "goods_receipt_line__receipt__purchase_order__supplier",
    ).all()
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "movement_type"]
    ordering_fields = ["created_at"]


class GoodsReceiptViewSet(viewsets.ModelViewSet):
    queryset = (
        GoodsReceipt.objects.select_related("item", "work_order", "purchase_order", "received_by")
        .prefetch_related("lines", "lines__po_item", "lines__inventory_item__material_type")
        .all()
    )
    serializer_class = GoodsReceiptSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["item", "work_order", "purchase_order"]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        with transaction.atomic():
            receipt = serializer.save(received_by=self.request.user)
            item = InventoryItem.objects.select_for_update().get(pk=receipt.item_id)
            item.quantity += receipt.quantity
            item.save(update_fields=["quantity", "updated_at"])
            StockMovement.objects.create(
                item=item,
                movement_type=StockMovement.MovementType.IN,
                quantity=receipt.quantity,
                reference=receipt.grn_number,
                performed_by=self.request.user,
                notes=f"Goods receipt {receipt.grn_number}",
            )


class IssuanceViewSet(viewsets.ModelViewSet):
    queryset = Issuance.objects.select_related(
        "item", "item__material_type", "issued_to_site", "issued_to_work_order",
        "issued_to_project", "issued_to_user", "issued_by",
    ).all()
    serializer_class = IssuanceSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = [
        "item", "issued_to_site", "issued_to_work_order", "issued_to_project",
        "issued_to_user", "bom_line",
    ]
    ordering_fields = ["created_at"]

    def perform_create(self, serializer):
        with transaction.atomic():
            issuance = serializer.save(issued_by=self.request.user)
            apply_issuance_stock_out(issuance, self.request.user)


class IssuanceRequestViewSet(viewsets.ModelViewSet):
    """The store's queue: everything waiting to be handed out.

    Anyone running work can ask for material; only the warehouse hands it over,
    so raising a request is open and issuing is not.
    """

    queryset = IssuanceRequest.objects.select_related(
        "item", "item__material_type", "unit_type", "project",
        "asset_component", "asset_component__device",
        "maintenance_schedule", "requested_by", "issued_by",
    ).all()
    serializer_class = IssuanceRequestSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "source", "project", "item", "unit_type", "asset_component"]
    search_fields = ["request_number", "purpose", "item__sku", "unit_type__name"]
    ordering_fields = ["created_at", "status"]

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user)

    def _store_only(self, request):
        if getattr(request.user, "role", "") not in WAREHOUSE_ROLES:
            return Response(
                {"detail": "Only the warehouse can issue material."}, status=403
            )
        return None

    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        """Hand over some or all of what was asked for."""
        denied = self._store_only(request)
        if denied is not None:
            return denied

        serializer = IssuanceRequestIssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issuance_request = self.get_object()

        with transaction.atomic():
            result = issue_against_request(
                issuance_request,
                request.user,
                serializer.validated_data["quantity"],
                received_by=serializer.validated_data.get("received_by", ""),
                notes=serializer.validated_data.get("notes", ""),
            )

        issuance_request.refresh_from_db()
        return Response({
            "request": IssuanceRequestSerializer(issuance_request).data,
            "issued": result["quantity"],
            "serials": result["serials"],
        })

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Close a request that is no longer needed; issued stock is untouched."""
        denied = self._store_only(request)
        if denied is not None:
            return denied

        issuance_request = self.get_object()
        issuance_request.status = IssuanceRequest.Status.CANCELLED
        issuance_request.save(update_fields=["status", "updated_at"])
        return Response(IssuanceRequestSerializer(issuance_request).data)

    @action(detail=True, methods=["get"])
    def slip(self, request, pk=None):
        """The issue slip: what went out, what for, and who handled it."""
        from .documents import render_issue_slip_pdf

        issuance_request = self.get_object()
        pdf = render_issue_slip_pdf(issuance_request)
        response = HttpResponse(pdf, content_type="application/pdf")
        name = issuance_request.request_number or "issue-slip"
        response["Content-Disposition"] = f'attachment; filename="{name}.pdf"'
        return response
