from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status as drf_status
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import FinanceWriteElseRead, PurchaseOrderActionElseRead

from .lines import describe_asset, describe_component, line_text
from .models import PurchaseOrder, PurchaseOrderItem
from .serializers import (
    PurchaseOrderFromShortageSerializer,
    PurchaseOrderItemDetailSerializer,
    PurchaseOrderReceiveSerializer,
    PurchaseOrderSerializer,
    PurchaseOrderTransitionSerializer,
)
from .services import receive_against_po


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseOrder.objects.select_related("supplier", "ordered_by", "approved_by")
        .prefetch_related(
            "items", "items__asset_type", "items__device_model", "items__material_type"
        )
        .all()
    )
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["status", "supplier"]
    search_fields = ["po_number"]
    ordering_fields = ["created_at", "order_date", "total_amount"]

    def perform_create(self, serializer):
        serializer.save(ordered_by=self.request.user)

    @action(detail=True, methods=["get"], url_path="document")
    def document(self, request, pk=None):
        """The order as a PDF, ready to send to the supplier."""
        from .documents import render_purchase_order_pdf

        purchase_order = self.get_object()
        pdf = render_purchase_order_pdf(purchase_order)
        response = HttpResponse(pdf, content_type="application/pdf")
        name = purchase_order.po_number or "purchase-order"
        response["Content-Disposition"] = f'attachment; filename="{name}.pdf"'
        return response

    @action(detail=False, methods=["post"], url_path="from-shortage")
    def from_shortage(self, request):
        """Raise a draft PO covering a project's BOM shortages (WF-03).

        One item per BOM line with shortage > 0 (optionally restricted to
        ``line_ids``): quantity = shortage, unit price and typed FKs copied
        from the line, ``bom_line`` back-reference set.
        """
        ser = PurchaseOrderFromShortageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        supplier = ser.validated_data["supplier"]
        shortage_lines = ser.validated_data["shortage_lines"]

        po_kwargs = {"supplier": supplier, "ordered_by": request.user}
        if "currency" in ser.validated_data:
            po_kwargs["currency"] = ser.validated_data["currency"]

        with transaction.atomic():
            purchase_order = PurchaseOrder.objects.create(**po_kwargs)
            for line in shortage_lines:
                PurchaseOrderItem.objects.create(
                    purchase_order=purchase_order,
                    asset_type=line.asset_type,
                    device_model=line.device_model,
                    material_type=line.material_type,
                    bom_line=line,
                    description=line.description,
                    quantity=line.shortage,
                    unit_price=line.unit_price,
                )
            purchase_order.recalc_total()

        return Response(
            PurchaseOrderSerializer(purchase_order, context={"request": request}).data,
            status=drf_status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        """Record a goods receipt against this PO (WF-04).

        Serialized lines spawn one Device per serial number (batch + supplier
        + price captured); consumable lines top up warehouse stock with a
        StockMovement IN. The PO auto-advances to partially_received/received.
        """
        purchase_order = self.get_object()
        ser = PurchaseOrderReceiveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        payload = receive_against_po(
            purchase_order,
            user=request.user,
            lines=ser.validated_data["lines"],
            reference=ser.validated_data.get("reference", ""),
            notes=ser.validated_data.get("notes", ""),
        )
        return Response(payload, status=drf_status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def requisitions(self, request):
        """Asset requirements the project flagged to be bought.

        These are the lines a user chose to procure rather than take from
        stock — including lines the warehouse could have covered, because that
        choice is theirs. Turn them into a purchase order with ``raise-po``.
        """
        from apps.assets.models import AssetComponent

        components = (
            AssetComponent.objects.filter(fulfilment=AssetComponent.Fulfilment.PROCUREMENT)
            .select_related(
                "device", "device__project", "inventory_item__material_type",
                "inventory_unit_type", "purchase_order_item__purchase_order",
            )
            .order_by("device__project__name", "device__asset_code", "name")
        )
        from apps.teams.models import ProjectScopeItem

        # An asset belongs to a project by its own field OR by a Scope row, so
        # both have to be honoured here as well.
        scope_by_device = {
            row["device_id"]: (row["project_id"], row["project__name"])
            for row in ProjectScopeItem.objects.values(
                "device_id", "project_id", "project__name"
            )
        }

        project_id = request.query_params.get("project")
        if project_id:
            scoped_ids = ProjectScopeItem.objects.filter(
                project_id=project_id
            ).values("device_id")
            components = components.filter(
                Q(device__project_id=project_id) | Q(device_id__in=scoped_ids)
            )
        if request.query_params.get("unordered") in ("1", "true", "True"):
            components = components.filter(purchase_order_item__isnull=True)

        def project_of(device):
            if device.project_id:
                return str(device.project_id), device.project.name
            scoped = scope_by_device.get(device.id)
            return (str(scoped[0]), scoped[1]) if scoped else (None, None)

        rows = []
        # Vendor-built assets: the whole asset is what gets bought.
        from apps.assets.models import Device

        devices = (
            Device.objects.filter(
                source__in=(Device.Source.VENDOR_SUPPLIED, Device.Source.VENDOR_TURNKEY),
                status=Device.Status.PROCURED,
            )
            # On a project the buy is decided in Execution; a standalone asset
            # is simply bought.
            .filter(
                Q(procurement_requested_at__isnull=False)
                | Q(procurement_item__isnull=False)
                | Q(project__isnull=True, project_scope_items__isnull=True)
            )
            .select_related("project", "asset_type", "procurement_item__purchase_order")
            .distinct()
            .order_by("asset_code")
        )
        if project_id:
            devices = devices.filter(Q(project_id=project_id) | Q(pk__in=scoped_ids))
        if request.query_params.get("unordered") in ("1", "true", "True"):
            devices = devices.filter(procurement_item__isnull=True)
        for d in devices:
            project_pk, project_name = project_of(d)
            label = d.display_name or (d.asset_type.name if d.asset_type_id else d.asset_code)
            rows.append({
                "kind": "asset",
                "component": None,
                "device": str(d.pk),
                "name": f"{label} (complete asset)",
                "asset": str(d.pk),
                "asset_code": d.asset_code,
                "project": project_pk,
                "project_name": project_name,
                "required_quantity": 1,
                "outstanding_quantity": 0 if d.procurement_item_id else 1,
                "available_quantity": None,
                "unit_price": d.purchase_price,
                "supply_vendor_name": d.supply_vendor_name,
                "inventory_item": None,
                "inventory_unit_type": None,
                "purchase_order_item": str(d.procurement_item_id) if d.procurement_item_id else None,
                "po_number": (
                    d.procurement_item.purchase_order.po_number if d.procurement_item_id else None
                ),
            })

        def _to_buy(c):
            # Each Procure decision is an awaiting request; a line flagged before
            # quantities were recorded falls back to everything outstanding.
            decided = c.procure_quantity
            return decided if decided else (c.outstanding_quantity if not c._open_requests() else 0)

        for c in components:
            if c.outstanding_quantity <= 0 or (_to_buy(c) <= 0 and not c.purchase_order_item_id):
                continue
            project_pk, project_name = project_of(c.device)
            rows.append({
                "kind": "component",
                "device": None,
                "component": str(c.pk),
                "name": c.name,
                "asset": str(c.device_id),
                "asset_code": c.device.asset_code,
                "project": project_pk,
                "project_name": project_name,
                "required_quantity": c.quantity,
                "outstanding_quantity": _to_buy(c),
                "available_quantity": c.available_quantity,
                "inventory_item": str(c.inventory_item_id) if c.inventory_item_id else None,
                "inventory_unit_type": (
                    str(c.inventory_unit_type_id) if c.inventory_unit_type_id else None
                ),
                "purchase_order_item": (
                    str(c.purchase_order_item_id) if c.purchase_order_item_id else None
                ),
                "po_number": (
                    c.purchase_order_item.purchase_order.po_number
                    if c.purchase_order_item_id else None
                ),
            })
        return Response({"count": len(rows), "results": rows})

    @action(detail=False, methods=["post"], url_path="raise-po")
    def raise_po(self, request):
        """Create one purchase order covering the given requirements.

        Body: supplier, components [ids], devices [ids], prices {id: amount},
        and optionally currency, expected_delivery, terms, notes.
        Each requirement becomes a PO line for its outstanding quantity and is
        linked back, so receiving the goods closes the loop. A vendor-built
        asset is bought as one line. Prices default to the last known figure;
        the buyer changes them before the order is placed.
        """
        from apps.assets.models import AssetComponent, Device
        from apps.suppliers.models import Supplier

        supplier_id = request.data.get("supplier")
        component_ids = request.data.get("components") or []
        device_ids = request.data.get("devices") or []
        prices = request.data.get("prices") or {}
        if not supplier_id:
            return Response({"supplier": ["Choose the supplier to buy from."]}, status=400)
        if not isinstance(component_ids, list) or not isinstance(device_ids, list):
            return Response({"components": ["Send lists of requirement and asset ids."]}, status=400)
        if not component_ids and not device_ids:
            return Response({"components": ["Pick at least one requirement."]}, status=400)
        if not Supplier.objects.filter(pk=supplier_id).exists():
            return Response({"supplier": ["Unknown supplier."]}, status=400)

        components = list(
            AssetComponent.objects.select_related("inventory_item", "inventory_unit_type")
            .filter(pk__in=component_ids)
        )
        missing = set(map(str, component_ids)) - {str(c.pk) for c in components}
        if missing:
            return Response(
                {"components": [f"Unknown requirement(s): {', '.join(sorted(missing))}"]},
                status=400,
            )
        already = [c.name for c in components if c.purchase_order_item_id]
        if already:
            return Response(
                {"components": [f"Already on a purchase order: {', '.join(already)}"]},
                status=400,
            )

        devices = list(Device.objects.filter(pk__in=device_ids)) if device_ids else []
        bought = [d.asset_code for d in devices if d.procurement_item_id]
        if bought:
            return Response(
                {"devices": [f"Already on a purchase order: {', '.join(bought)}"]}, status=400
            )

        def price_for(key, fallback):
            raw = prices.get(str(key))
            if raw in (None, ""):
                return fallback or 0
            try:
                return Decimal(str(raw))
            except (InvalidOperation, ValueError):
                return fallback or 0

        from .documents import DEFAULT_TERMS

        with transaction.atomic():
            purchase_order = PurchaseOrder.objects.create(
                supplier_id=supplier_id,
                currency=request.data.get("currency", PurchaseOrder.Currency.PKR),
                expected_delivery=request.data.get("expected_delivery") or None,
                terms=(request.data.get("terms") or "").strip() or DEFAULT_TERMS,
                notes=(request.data.get("notes") or "").strip(),
                ordered_by=request.user,
            )
            for device in devices:
                item = PurchaseOrderItem.objects.create(
                    purchase_order=purchase_order,
                    description=line_text(*describe_asset(device)),
                    quantity=1,
                    unit_price=price_for(device.pk, device.purchase_price),
                    device_model=device.device_model,
                    asset_type=device.asset_type,
                )
                device.procurement_item = item
                device.save(update_fields=["procurement_item", "updated_at"])
            for component in components:
                decided = component.procure_quantity
                quantity = decided if decided else component.outstanding_quantity
                if quantity < 1:
                    continue
                item = PurchaseOrderItem.objects.create(
                    purchase_order=purchase_order,
                    description=line_text(*describe_component(component)),
                    quantity=quantity,
                    unit_price=price_for(component.pk, (
                        component.inventory_unit_type.unit_cost
                        if component.inventory_unit_type_id
                        else (component.inventory_item.unit_cost if component.inventory_item_id else None)
                    )),
                    material_type=(
                        component.inventory_item.material_type
                        if component.inventory_item_id else None
                    ),
                    # Replenish the exact row the requirement points at.
                    inventory_item=component.inventory_item,
                    inventory_unit_type=component.inventory_unit_type,
                )
                component.purchase_order_item = item
                component.save(update_fields=["purchase_order_item", "updated_at"])
            purchase_order.recalc_total()

        return Response(
            PurchaseOrderSerializer(purchase_order, context={"request": request}).data, status=drf_status.HTTP_201_CREATED
        )

    def get_permissions(self):
        if getattr(self, "action", None) == "transition":
            return [IsAuthenticated(), PurchaseOrderActionElseRead()]
        return super().get_permissions()

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        purchase_order = self.get_object()
        if getattr(request.user, "role", "") == "group_head" and request.data.get("status") != "approved":
            return Response(
                {"detail": "The Group Head signs purchase orders off; Operations move them otherwise."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        ser = PurchaseOrderTransitionSerializer(
            data=request.data, context={"purchase_order": purchase_order}
        )
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "").strip()

        # Placing an order commits money: Operations raise it, the Group Head
        # signs it off (or the Super Admin).
        if new_status == PurchaseOrder.Status.APPROVED and getattr(
            request.user, "role", ""
        ) not in ("super_admin", "group_head"):
            return Response(
                {"detail": "Purchase orders are approved by the Group Head."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        update_fields = ["status", "updated_at"]
        old_status_display = purchase_order.get_status_display()
        purchase_order.status = new_status

        if new_status == PurchaseOrder.Status.APPROVED:
            purchase_order.approved_by = request.user
            update_fields += ["approved_by"]
            # The order date is the day the Group Head approved it — stamped,
            # never typed. That approval is what commits the company.
            if not purchase_order.order_date:
                purchase_order.order_date = timezone.localdate()
                update_fields += ["order_date"]

        # An order that somehow reached placement without approval on record
        # still gets a date the day it is placed.
        if new_status == PurchaseOrder.Status.ORDERED and not purchase_order.order_date:
            purchase_order.order_date = timezone.localdate()
            update_fields += ["order_date"]

        # A cancelled order bought nothing: every requirement it was covering
        # goes back to "to procure" so it can be raised again, and any asset it
        # was buying is unlinked.
        if new_status == PurchaseOrder.Status.CANCELLED:
            from apps.assets.models import AssetComponent, Device

            AssetComponent.objects.filter(
                purchase_order_item__purchase_order=purchase_order
            ).update(purchase_order_item=None, fulfilment=AssetComponent.Fulfilment.PROCUREMENT)
            Device.objects.filter(
                procurement_item__purchase_order=purchase_order
            ).update(procurement_item=None)

        if notes:
            stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
            who = request.user.get_full_name() or request.user.username
            line = (
                f"[{stamp}] {who}: "
                f"{old_status_display} → {purchase_order.get_status_display()} — {notes}"
            )
            purchase_order.notes = f"{purchase_order.notes}\n{line}" if purchase_order.notes else line
            update_fields += ["notes"]

        purchase_order.save(update_fields=update_fields)

        return Response(PurchaseOrderSerializer(purchase_order, context={"request": request}).data)


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrderItem.objects.select_related(
        "purchase_order", "asset_type", "device_model", "material_type"
    ).all()
    serializer_class = PurchaseOrderItemDetailSerializer
    permission_classes = [IsAuthenticated, FinanceWriteElseRead]
    filterset_fields = ["purchase_order"]

    EDITABLE = ("draft", "pending_approval")

    def _refuse_if_fixed(self, purchase_order):
        """Item 30: prices and lines can change up to approval, not after."""
        if purchase_order.status not in self.EDITABLE:
            raise serializers.ValidationError(
                {"detail": f"{purchase_order.po_number} is {purchase_order.get_status_display()}: "
                           "its lines and prices are fixed once approved."}
            )

    def perform_create(self, serializer):
        self._refuse_if_fixed(serializer.validated_data["purchase_order"])
        item = serializer.save()
        item.purchase_order.recalc_total()

    def perform_update(self, serializer):
        old_parent = serializer.instance.purchase_order
        self._refuse_if_fixed(old_parent)
        item = serializer.save()
        item.purchase_order.recalc_total()
        if item.purchase_order.pk != old_parent.pk:
            old_parent.recalc_total()

    def perform_destroy(self, instance):
        purchase_order = instance.purchase_order
        self._refuse_if_fixed(purchase_order)
        instance.delete()
        purchase_order.recalc_total()
