from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from common.noops import RefusesSilentNoOps
from common.dates import refuse_past
from rest_framework import status as drf_status
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import CapabilityGate, can, can_for_project
from apps.notifications import service as notices

from .lines import describe_asset, describe_component, line_text
from .models import PurchaseOrder, PurchaseOrderItem
from .serializers import (
    PurchaseOrderFromShortageSerializer,
    PurchaseOrderItemDetailSerializer,
    PurchaseOrderItemSerializer,
    PurchaseOrderReceiveSerializer,
    PurchaseOrderSerializer,
    PurchaseOrderTransitionSerializer,
)
from .services import receive_against_po


class PurchaseOrderViewSet(RefusesSilentNoOps, viewsets.ModelViewSet):
    # Reading an order is not the same as seeing its money: the store
    # reads orders with every figure masked. No read gate here — the
    # price masking is the control, and scoping does the rest.
    queryset = (
        PurchaseOrder.objects.select_related("supplier", "ordered_by", "approved_by")
        .prefetch_related(
            "items", "items__asset_type", "items__device_model", "items__material_type"
        )
        .all()
    )
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    # Reading an order is open to anyone who may see procurement; the money
    # on it is masked separately by view_prices. Every move is a capability.
    read_capability = "view_procurement"
    write_capability = "raise_po"
    action_capabilities = {
        # A delivery is booked in by whoever raised the order or runs the
        # store; checking it over is the inspector's step, in Inventory.
        "receive": ("raise_po", "receive_goods"),
        "requisitions": "view_procurement",
        "send_back_requisition": "raise_po",
        # Agreeing a price is deliberately not a procurement right: the whole
        # point is that somebody outside Procurement says yes. Who exactly is
        # checked against the line's own owner, inside the action.
        "price_variance": None,
        "price_variances": None,
        # Approving and cancelling are checked inside, per the move asked for.
        "transition": ("raise_po", "approve_po", "cancel_po"),
    }
    filterset_fields = ["status", "supplier"]
    search_fields = ["po_number"]
    ordering_fields = ["created_at", "order_date", "total_amount"]

    def perform_create(self, serializer):
        serializer.save(ordered_by=self.request.user)

    @action(detail=False, methods=["get"], url_path="default-terms")
    def default_terms(self, request):
        """The house standard terms, shown on a new order for editing."""
        from .documents import DEFAULT_TERMS

        return Response({"terms": DEFAULT_TERMS})

    @action(detail=True, methods=["get"], url_path="document")
    def document(self, request, pk=None):
        """The order as a PDF, ready to send to the supplier."""
        from .documents import render_purchase_order_pdf

        from common.money import viewer_sees_prices

        purchase_order = self.get_object()
        # The PDF is a copy of the screen, and the screen masks prices for
        # readers without the capability. A download that did not would be
        # the easiest way around the control.
        pdf = render_purchase_order_pdf(
            purchase_order, show_prices=viewer_sees_prices({"request": request})
        )
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

    @action(detail=False, methods=["post"], url_path="requisitions/send-back")
    def send_back_requisition(self, request):
        """Hand a To-Procure line back to the project.

        Body: component or device, and a reason. The Procure decision is
        undone — the line is 'Not decided' again in Execution — and the reason
        is journalled on the asset. A line already on a purchase order stays;
        cancel the order first.
        """
        from apps.assets.models import AssetComponent, Device, DeviceLifecycleEvent
        from apps.inventory.models import IssuanceRequest

        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"reason": ["Say why it is going back."]}, status=400)

        from apps.inventory.models import ReorderRequest

        reorder = ReorderRequest.objects.filter(pk=request.data.get("reorder")).first() if request.data.get("reorder") else None
        if reorder is not None:
            # A stock reorder has no project; it is simply withdrawn, reason on record.
            if reorder.status == ReorderRequest.Status.ORDERED:
                return Response({"detail": f"'{reorder.name}' is already on {reorder.purchase_order_item.purchase_order.po_number} — cancel that order first."}, status=400)
            from django.utils import timezone as _tz

            reorder.status = ReorderRequest.Status.CANCELLED
            reorder.notes = (reorder.notes + "\n" if reorder.notes else "") + f"Sent back by Procurement: {reason}"
            reorder.declined_reason = reason
            reorder.declined_at = _tz.now()
            reorder.declined_by = request.user
            reorder.save(update_fields=[
                "status", "notes", "declined_reason", "declined_at", "declined_by", "updated_at",
            ])
            return Response({
                "detail": (
                    f"The reorder of {reorder.name} goes back to the store — it is off the buying "
                    f"list, and Inventory sees why under Low Stock."
                )
            })

        component = AssetComponent.objects.filter(pk=request.data.get("component")).select_related("device").first()
        device = Device.objects.filter(pk=request.data.get("device")).first() if not component else None
        if component is None and device is None:
            return Response({"detail": "Pick the line to send back."}, status=400)

        with transaction.atomic():
            if component is not None:
                po_item = component.purchase_order_item
                if po_item is not None and po_item.purchase_order.status != PurchaseOrder.Status.CANCELLED:
                    return Response({"detail": (
                        f"'{component.name}' is already on {po_item.purchase_order.po_number} — cancel that order first."
                    )}, status=400)
                bought = component.procure_quantity
                # Only the buy is undone; anything the store was asked for stays asked.
                component.issuance_requests.filter(awaiting_procurement=True).exclude(
                    status=IssuanceRequest.Status.CANCELLED
                ).update(status=IssuanceRequest.Status.CANCELLED)
                component.purchase_order_item = None
                component.refresh_from_db(fields=["purchase_order_item"])
                component.fulfilment = (
                    AssetComponent.Fulfilment.FROM_STOCK if component.stock_requested_quantity
                    else AssetComponent.Fulfilment.PENDING
                )
                component.save(update_fields=["fulfilment", "purchase_order_item", "updated_at"])
                asset, what = component.device, f"'{component.name}' × {bought or component.outstanding_quantity}"
            else:
                if device.procurement_item_id and device.procurement_item.purchase_order.status != PurchaseOrder.Status.CANCELLED:
                    return Response({"detail": (
                        f"{device.asset_code} is already on {device.procurement_item.purchase_order.po_number} — cancel that order first."
                    )}, status=400)
                device.procurement_requested_at = None
                device.save(update_fields=["procurement_requested_at", "updated_at"])
                asset, what = device, f"the complete asset {device.asset_code}"
            DeviceLifecycleEvent.objects.create(
                device=asset,
                event_type=DeviceLifecycleEvent.EventType.NOTE,
                description=f"Procurement sent {what} back to the project: {reason}",
                performed_by=request.user,
                metadata={"sent_back": True, "reason": reason,
                          **({"component": str(component.pk)} if component is not None else {})},
            )
        return Response({"detail": f"{what[0].upper() + what[1:]} is back with the project to decide."})

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
            # Newest first: the line raised today is the one being acted on.
            # Project and asset stay as the tiebreak so one asset's lines sit
            # together when they were raised together.
            .order_by("-created_at", "device__project__name", "device__asset_code", "name")
        )
        from apps.teams.models import ProjectScopeItem

        # An asset belongs to a project by its own field OR by a Scope row, so
        # both have to be honoured here as well.
        scope_by_device = {
            row["device_id"]: (row["project_id"], row["project__name"], row["project__target_date"])
            for row in ProjectScopeItem.objects.values(
                "device_id", "project_id", "project__name", "project__target_date"
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
            """(id, name, target date) of the project this asset is being bought for.

            The target date is what the buyer needs it by, so the order can be
            dated from the plan instead of from memory.
            """
            if device.project_id:
                return str(device.project_id), device.project.name, device.project.target_date
            scoped = scope_by_device.get(device.id)
            return (str(scoped[0]), scoped[1], scoped[2]) if scoped else (None, None, None)

        rows = []
        # Which part of the business asked for this, and from which screen.
        # The buyer reads the queue top to bottom without knowing the history
        # of each line, so every row says where it came from.
        ORIGIN_PROJECT = ("Project", "Execution › Build Requirements")
        ORIGIN_ASSET = ("Asset registry", "Vendor-supplied asset")
        ORIGIN_INVENTORY = ("Inventory", "Low Stock")

        # What the line should cost, to measure the quote against. A project
        # line is held to the figure its budget was approved on; anything the
        # store or a maintenance job asks for is held to the last price paid,
        # because nobody planned it.
        from apps.teams.costing import component_unit_price, last_procured_price
        from apps.teams.models import ProjectBudget

        budget_status = {
            str(pk): st
            for pk, st in ProjectBudget.objects.values_list("project_id", "status")
        }

        def reference(unit_price, label, quantity):
            """The money figure a row is measured against, and where it is from."""
            if unit_price is None:
                return {"reference_unit_price": None, "reference_amount": None,
                        "reference_label": label}
            return {
                "reference_unit_price": unit_price,
                "reference_amount": unit_price * (quantity or 0),
                "reference_label": label,
            }

        def planned_label(project_pk):
            if project_pk is None:
                return "Listed price"
            if budget_status.get(str(project_pk)) == ProjectBudget.Status.APPROVED:
                return "Planned · approved"
            return "Planned · not approved yet"

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
            project_pk, project_name, project_due = project_of(d)
            label = d.display_name or (d.asset_type.name if d.asset_type_id else d.asset_code)
            # A vendor asset on a project was decided in Execution; one with no
            # project behind it was simply registered and bought.
            origin, origin_detail = (
                (ORIGIN_PROJECT[0], "Vendor-supplied asset") if project_pk else ORIGIN_ASSET
            )
            rows.append({
                "kind": "asset",
                "origin": origin,
                "origin_detail": origin_detail,
                # A whole asset is priced on the asset itself, so that figure
                # is the one the project was costed on.
                **reference(d.purchase_price, planned_label(project_pk), 1),
                "component": None,
                "device": str(d.pk),
                "name": f"{label} (complete asset)",
                "asset": str(d.pk),
                "asset_code": d.asset_code,
                "project": project_pk,
                "project_name": project_name,
                "project_target_date": project_due,
                "required_quantity": 1,
                "unit": "asset",
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

        # Stock that fell to its reorder level, asked to be bought from Inventory › Low Stock.
        from apps.inventory.models import ReorderRequest

        reorders = (
            ReorderRequest.objects.filter(status__in=(ReorderRequest.Status.OPEN, ReorderRequest.Status.ORDERED))
            .select_related("item__material_type", "unit_type", "purchase_order_item__purchase_order")
            .order_by("-created_at")
        )
        if project_id:
            reorders = reorders.none()
        if request.query_params.get("unordered") in ("1", "true", "True"):
            reorders = reorders.filter(purchase_order_item__isnull=True)
        for rr in reorders:
            # Nobody planned a replenishment, so the last price paid is the
            # only figure to hold the quote against.
            lookup = (
                {"inventory_unit_type_id": rr.unit_type_id} if rr.unit_type_id
                else {"inventory_item_id": rr.item_id} if rr.item_id else None
            )
            last_price, last_po = last_procured_price(**lookup) if lookup else (None, None)
            opening = (
                rr.unit_type.unit_cost if rr.unit_type_id
                else rr.item.unit_cost if rr.item_id else None
            )
            if last_price is not None:
                ref = reference(last_price, f"Last PO · {last_po}", rr.quantity)
            elif opening:
                ref = reference(opening, "Opening cost — never purchased", rr.quantity)
            else:
                ref = reference(None, "No price on record", rr.quantity)
            rows.append({
                "kind": "reorder",
                "origin": ORIGIN_INVENTORY[0],
                "origin_detail": ORIGIN_INVENTORY[1],
                **ref,
                "reorder": str(rr.pk),
                "request_number": rr.request_number,
                "component": None,
                "device": None,
                "name": f"{rr.name} — stock replenishment",
                "asset": None,
                "asset_code": "Stock",
                "project": None,
                "project_name": None,
                # Replenishing the shelf answers to no project's date.
                "project_target_date": None,
                "required_quantity": rr.quantity,
                "unit": rr.unit,
                "outstanding_quantity": 0 if rr.purchase_order_item_id else rr.quantity,
                "available_quantity": rr.on_hand,
                "reorder_level": rr.reorder_level,
                "unit_price": rr.unit_type.unit_cost if rr.unit_type_id else (rr.item.unit_cost if rr.item_id else None),
                "last_unit_price": rr.unit_type.unit_cost if rr.unit_type_id else (rr.item.unit_cost if rr.item_id else None),
                "supply_vendor_name": None,
                "inventory_item": str(rr.item_id) if rr.item_id else None,
                "inventory_unit_type": str(rr.unit_type_id) if rr.unit_type_id else None,
                "purchase_order_item": str(rr.purchase_order_item_id) if rr.purchase_order_item_id else None,
                "po_number": rr.purchase_order_item.purchase_order.po_number if rr.purchase_order_item_id else None,
                "reason": rr.reason,
            })

        def _to_buy(c):
            # Each Procure decision is an awaiting request; a line flagged before
            # quantities were recorded falls back to everything outstanding.
            decided = c.procure_quantity
            return decided if decided else (c.outstanding_quantity if not c._open_requests() else 0)

        for c in components:
            if c.outstanding_quantity <= 0 or (_to_buy(c) <= 0 and not c.purchase_order_item_id):
                continue
            project_pk, project_name, project_due = project_of(c.device)
            planned_unit, priced_from = component_unit_price(c)
            rows.append({
                "kind": "component",
                "origin": ORIGIN_PROJECT[0] if project_pk else ORIGIN_ASSET[0],
                "origin_detail": (
                    ORIGIN_PROJECT[1] if project_pk else "Asset components"
                ),
                # The figure the budget was approved on, for the quantity
                # still being bought.
                **reference(planned_unit, planned_label(project_pk), _to_buy(c)),
                "priced_from": priced_from,
                "device": None,
                "component": str(c.pk),
                "name": c.name,
                "asset": str(c.device_id),
                "asset_code": c.device.asset_code,
                "project": project_pk,
                "project_name": project_name,
                "project_target_date": project_due,
                "required_quantity": c.quantity,
                "unit": c.unit or (
                    (c.inventory_unit_type.unit or "piece") if c.inventory_unit_type_id
                    else (c.inventory_item.material_type.unit or "piece")
                    if c.inventory_item_id and c.inventory_item.material_type_id else "piece"
                ),
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
        and optionally extra_items, currency, supplier_details, expected_delivery,
        terms, notes.
        Each requirement becomes a PO line for its outstanding quantity and is
        linked back, so receiving the goods closes the loop. A vendor-built
        asset is bought as one line. Prices default to the last known figure;
        the buyer changes them before the order is placed. ``extra_items`` are
        lines the requests never asked for — freight, a spare, a charge —
        written exactly as the new-order screen writes them.
        """
        from apps.assets.models import AssetComponent, Device
        from apps.suppliers.models import Supplier

        supplier_id = request.data.get("supplier")
        component_ids = request.data.get("components") or []
        device_ids = request.data.get("devices") or []
        reorder_ids = request.data.get("reorders") or []
        prices = request.data.get("prices") or {}
        extra_items = request.data.get("extra_items") or []
        if not isinstance(extra_items, list):
            return Response({"extra_items": ["Send a list of extra lines."]}, status=400)
        if not supplier_id:
            return Response({"supplier": ["Choose the supplier to buy from."]}, status=400)
        if not isinstance(component_ids, list) or not isinstance(device_ids, list) or not isinstance(reorder_ids, list):
            return Response({"components": ["Send lists of requirement, asset and reorder ids."]}, status=400)
        if not component_ids and not device_ids and not reorder_ids:
            return Response({"components": ["Pick at least one requirement."]}, status=400)
        from apps.inventory.models import ReorderRequest

        reorders = list(
            ReorderRequest.objects.filter(pk__in=reorder_ids)
            .select_related("item__material_type", "unit_type", "purchase_order_item__purchase_order")
        ) if reorder_ids else []
        if len(reorders) != len(set(map(str, reorder_ids))):
            return Response({"reorders": ["Unknown reorder request(s)."]}, status=400)
        ordered = [rr.name for rr in reorders if rr.status != ReorderRequest.Status.OPEN]
        if ordered:
            return Response({"reorders": [f"Not open any more: {', '.join(ordered)}"]}, status=400)
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

        # ── what each line was expected to cost ───────────────────────
        # Stamped onto the line so the figure that was agreed cannot drift
        # when the next order changes what "the last price" means.
        from apps.teams.costing import component_unit_price, last_procured_price

        Owner = PurchaseOrderItem.VarianceOwner
        reasons = request.data.get("variance_reasons") or {}

        def on_a_project(device) -> bool:
            from apps.teams.models import ProjectScopeItem
            return bool(
                device.project_id
                or ProjectScopeItem.objects.filter(device_id=device.pk).exists()
            )

        def reference_for_component(c):
            unit, label = component_unit_price(c)
            owner = Owner.PROJECT if on_a_project(c.device) else Owner.OPERATIONS
            return unit, label, owner

        def reference_for_reorder(rr):
            lookup = (
                {"inventory_unit_type_id": rr.unit_type_id} if rr.unit_type_id
                else {"inventory_item_id": rr.item_id} if rr.item_id else None
            )
            price, po_number = last_procured_price(**lookup) if lookup else (None, None)
            if price is not None:
                return price, f"Last PO · {po_number}", Owner.INVENTORY
            opening = (
                rr.unit_type.unit_cost if rr.unit_type_id
                else rr.item.unit_cost if rr.item_id else None
            )
            if opening:
                return opening, "Opening cost — never purchased", Owner.INVENTORY
            return None, "No price on record", Owner.INVENTORY

        def stamp(item, key, unit, label, owner):
            """Fix the reference to the line and ask for a blessing if needed."""
            if unit is None:
                return
            item.reference_unit_price = unit
            item.reference_label = label or ""
            item.variance_owner = owner
            item.variance_reason = (reasons.get(str(key)) or "").strip()
            item.save(update_fields=[
                "reference_unit_price", "reference_label", "variance_owner",
                "variance_reason", "unit_price", "updated_at",
            ])

        # The added lines answer to the same rules as a hand-written order's.
        extra = PurchaseOrderItemSerializer(data=extra_items, many=True)
        extra.is_valid(raise_exception=True)

        from .documents import DEFAULT_TERMS
        from .serializers import _buy_asset_on

        with transaction.atomic():
            purchase_order = PurchaseOrder.objects.create(
                supplier_id=supplier_id,
                currency=request.data.get("currency") or PurchaseOrder.Currency.PKR,
                supplier_details=(request.data.get("supplier_details") or "").strip(),
                payment_terms_id=request.data.get("payment_terms") or None,
                payment_terms_note=(request.data.get("payment_terms_note") or "").strip()[:200],
                expected_delivery=refuse_past(request.data.get("expected_delivery"), "expected_delivery"),
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
                stamp(
                    item, device.pk, device.purchase_price, "Listed price",
                    Owner.PROJECT if on_a_project(device) else Owner.OPERATIONS,
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
                stamp(item, component.pk, *reference_for_component(component))
                component.purchase_order_item = item
                component.save(update_fields=["purchase_order_item", "updated_at"])
            for rr in reorders:
                item = PurchaseOrderItem.objects.create(
                    purchase_order=purchase_order,
                    description=f"{rr.name} — stock replenishment (reorder level {rr.reorder_level} {rr.unit})",
                    quantity=rr.quantity,
                    unit_price=price_for(rr.pk, (
                        rr.unit_type.unit_cost if rr.unit_type_id
                        else (rr.item.unit_cost if rr.item_id else None)
                    )),
                    material_type=rr.item.material_type if rr.item_id and rr.item.material_type_id else None,
                    inventory_item=rr.item,
                    inventory_unit_type=rr.unit_type,
                )
                stamp(item, rr.pk, *reference_for_reorder(rr))
                rr.purchase_order_item = item
                rr.status = ReorderRequest.Status.ORDERED
                rr.save(update_fields=["purchase_order_item", "status", "updated_at"])
            for row in extra.validated_data:
                row.pop("id", None)
                device_id = row.pop("device", None)
                line = PurchaseOrderItem.objects.create(purchase_order=purchase_order, **row)
                _buy_asset_on(line, device_id)
            purchase_order.recalc_total()

        # The requests this order covers are no longer waiting on anybody.
        for component in components:
            notices.resolve(f"procure:{component.pk}")
        for rr in reorders:
            notices.resolve(f"reorder:{rr.pk}")
        for device in devices:
            notices.resolve(f"procure-asset:{device.pk}")

        # Anything priced over plan goes straight to the desk that has to
        # agree it, rather than waiting to be noticed.
        flagged = list(purchase_order.unagreed_lines())
        if flagged:
            from .variance import tell_the_owner
            tell_the_owner(flagged, raised_by=request.user)

        return Response(
            PurchaseOrderSerializer(purchase_order, context={"request": request}).data, status=drf_status.HTTP_201_CREATED
        )

    # Who may agree to pay over the odds, by whose figure was exceeded.
    # Which capability agrees a line priced over its reference, by whose
    # figure was passed. A project's manager agrees their own project's.
    VARIANCE_DECIDERS = {
        PurchaseOrderItem.VarianceOwner.PROJECT: "agree_project_variance",
        PurchaseOrderItem.VarianceOwner.INVENTORY: "agree_stock_variance",
        PurchaseOrderItem.VarianceOwner.OPERATIONS: ("agree_project_variance", "agree_stock_variance"),
    }

    @classmethod
    def may_agree(cls, user, item) -> bool:
        needed = cls.VARIANCE_DECIDERS.get(item.variance_owner, ())
        needed = (needed,) if isinstance(needed, str) else tuple(needed)
        project = getattr(item, "project_of_line", None)
        return any(can_for_project(user, project, c) for c in needed)

    @action(detail=False, methods=["get"], url_path="price-variances")
    def price_variances(self, request):
        """Lines waiting on this user's side to agree the price.

        Procurement cannot decide its own variance — that is the whole point
        — so the queue is served to whoever owns the figure that was passed.
        """
        owners = [
            owner for owner, caps in self.VARIANCE_DECIDERS.items()
            if any(can(request.user, c) for c in ((caps,) if isinstance(caps, str) else caps))
        ]
        # A project's manager sees their own project's lines whatever else
        # they hold; the per-line check below keeps it to those.
        if PurchaseOrderItem.VarianceOwner.PROJECT not in owners and request.user.managed_projects.exists():
            owners.append(PurchaseOrderItem.VarianceOwner.PROJECT)
        items = (
            PurchaseOrderItem.objects.filter(
                variance_status=PurchaseOrderItem.VarianceStatus.PENDING,
                variance_owner__in=owners,
            )
            # A cancelled order is not waiting on anybody. Its lines were
            # still being offered for agreement long after the order itself
            # was called off, so the queue never emptied.
            .exclude(purchase_order__status=PurchaseOrder.Status.CANCELLED)
            .select_related("purchase_order", "purchase_order__supplier")
            .order_by("-purchase_order__created_at")
        )
        def row(line, *, kind, order, order_number, supplier, currency, raised_by):
            return {
                "id": str(line.pk),
                # Which kind of order it sits on, so the decision goes to the
                # right place. Buying a part and buying an operation are the
                # same argument about the same kind of figure.
                "kind": kind,
                "order": str(order.pk),
                "order_number": order_number,
                # Kept for the screens written before work orders joined.
                "purchase_order": str(order.pk) if kind == "purchase" else None,
                "po_number": order_number,
                "supplier_name": supplier,
                "currency": currency,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "reference_unit_price": line.reference_unit_price,
                "reference_label": line.reference_label,
                # One decimal place: nobody argues a price over a millionth
                # of a percent, and the raw Decimal printed 15 digits of it.
                "variance_percent": (
                    None if line.variance_percent is None
                    else round(float(line.variance_percent), 1)
                ),
                "variance_owner": line.variance_owner,
                "variance_owner_display": line.get_variance_owner_display(),
                "variance_reason": line.variance_reason,
                "raised_by": raised_by,
            }

        rows = [row(
            i, kind="purchase", order=i.purchase_order,
            order_number=i.purchase_order.po_number,
            supplier=i.purchase_order.supplier.name if i.purchase_order.supplier_id else None,
            currency=i.purchase_order.currency,
            raised_by=(
                i.purchase_order.ordered_by.get_full_name()
                if i.purchase_order.ordered_by_id else None
            ),
        ) for i in items]

        # Work orders answer to the same rule against the project's plan, and
        # to the same people, so they wait in the same queue.
        from apps.workorders.models import WorkOrder, WorkOrderItem

        operations = (
            WorkOrderItem.objects.filter(
                variance_status=WorkOrderItem.VarianceStatus.PENDING,
                variance_owner__in=owners,
            )
            .exclude(work_order__status=WorkOrder.Status.CANCELLED)
            .select_related("work_order", "work_order__supplier", "work_order__created_by")
            .order_by("-work_order__created_at")
        )
        rows += [row(
            w, kind="work", order=w.work_order,
            order_number=w.work_order.wo_number,
            supplier=w.work_order.supplier.name if w.work_order.supplier_id else None,
            currency=w.work_order.currency,
            raised_by=(
                w.work_order.created_by.get_full_name()
                if w.work_order.created_by_id else None
            ),
        ) for w in operations]

        return Response({"count": len(rows), "results": rows})

    @action(detail=True, methods=["post"], url_path="price-variance")
    def price_variance(self, request, pk=None):
        """Agree, or refuse, the price on one line of this order.

        Body: ``item`` (line id), ``approve`` (bool), ``notes``.
        """
        purchase_order = self.get_object()
        item = purchase_order.items.filter(pk=request.data.get("item")).first()
        if item is None:
            return Response({"item": ["Pick the line to decide."]}, status=400)
        if item.variance_status != PurchaseOrderItem.VarianceStatus.PENDING:
            return Response({"detail": (
                f"That line is already {item.get_variance_status_display().lower()}."
            )}, status=400)

        if not self.may_agree(request.user, item):
            return Response({"detail": (
                f"This one is {item.get_variance_owner_display()}'s to agree, not yours."
            )}, status=drf_status.HTTP_403_FORBIDDEN)

        approve = bool(request.data.get("approve"))
        notes = (request.data.get("notes") or "").strip()
        if not approve and not notes:
            return Response(
                {"notes": ["Say why the price is refused — the buyer has to act on it."]},
                status=400,
            )

        item.variance_status = (
            PurchaseOrderItem.VarianceStatus.APPROVED if approve
            else PurchaseOrderItem.VarianceStatus.REJECTED
        )
        item.variance_notes = notes
        item.variance_decided_by = request.user
        item.variance_decided_at = timezone.now()
        item.variance_decided_price = item.unit_price
        item.save(update_fields=[
            "variance_status", "variance_notes", "variance_decided_by",
            "variance_decided_at", "variance_decided_price", "updated_at",
        ])
        from .variance import tell_the_buyer
        tell_the_buyer(item, request.user, approve)
        if not purchase_order.unagreed_lines().exists():
            notices.resolve(f"po-variance:{purchase_order.pk}")

        left = purchase_order.unagreed_lines().count()
        return Response({
            "detail": (
                f"Price agreed for '{item.description}'." if approve
                else f"Price refused for '{item.description}' — it goes back to Procurement."
            ),
            "unagreed_lines": left,
            "can_submit": left == 0,
        })

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        purchase_order = self.get_object()
        wanted = request.data.get("status")
        # Each move is its own right: raising and placing, signing off,
        # and cancelling an order that was already approved.
        if wanted == PurchaseOrder.Status.APPROVED:
            if not can(request.user, "approve_po"):
                return Response(
                    {"detail": "Purchase orders are approved by the Group Head."},
                    status=drf_status.HTTP_403_FORBIDDEN,
                )
        elif wanted == PurchaseOrder.Status.CANCELLED and purchase_order.status not in (
            PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.PENDING_APPROVAL,
        ):
            if not can(request.user, "cancel_po"):
                return Response(
                    {"detail": "Cancelling an approved order needs the right to cancel orders."},
                    status=drf_status.HTTP_403_FORBIDDEN,
                )
        elif not can(request.user, "raise_po"):
            return Response(
                {"detail": "Operations raise and move purchase orders."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        ser = PurchaseOrderTransitionSerializer(
            data=request.data, context={"purchase_order": purchase_order}
        )
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "").strip()

        # An order priced over what anybody planned for does not reach the
        # Group Head until the side whose figure was exceeded has said yes.
        if new_status == PurchaseOrder.Status.PENDING_APPROVAL:
            unagreed = list(purchase_order.unagreed_lines())
            if unagreed:
                waiting = ", ".join(
                    f"{i.description} ({i.get_variance_owner_display()})" for i in unagreed[:4]
                )
                return Response({"detail": (
                    f"{len(unagreed)} line(s) are priced above what was planned and have not "
                    f"been agreed: {waiting}. They go back for approval before this order "
                    f"can go up for signature."
                )}, status=drf_status.HTTP_400_BAD_REQUEST)

        # The date can arrive with the move: an order written without one
        # is given it here, at the moment it matters.
        given_delivery = refuse_past(ser.validated_data.get("expected_delivery"), "expected_delivery")
        if given_delivery and not purchase_order.expected_delivery:
            purchase_order.expected_delivery = given_delivery

        # A supplier cannot promise what nobody asked for: an order leaving
        # draft says when the goods are needed by. A draft is still being
        # written, so it is free to be incomplete.
        if (
            new_status not in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CANCELLED)
            and not purchase_order.expected_delivery
        ):
            return Response(
                {"expected_delivery": ["Say when the goods are needed by before sending this order on."]},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        # ...and how the supplier is paid: the order is the promise, and the
        # supplier reads the terms off it.
        if (
            new_status not in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CANCELLED)
            and not purchase_order.payment_terms_id
            and not purchase_order.payment_terms_note.strip()
        ):
            return Response(
                {"payment_terms": ["Set the payment terms before sending this order on."]},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        update_fields = ["status", "updated_at"]
        if given_delivery and "expected_delivery" not in update_fields:
            update_fields.append("expected_delivery")
        old_status_display = purchase_order.get_status_display()
        purchase_order.status = new_status
        if new_status == PurchaseOrder.Status.CANCELLED:
            # Stock reorders on this order go back to the queue to be bought again.
            from apps.inventory.models import ReorderRequest

            ReorderRequest.objects.filter(purchase_order_item__purchase_order=purchase_order, status="ordered").update(
                status=ReorderRequest.Status.OPEN, purchase_order_item=None,
            )

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
        self._tell_about(purchase_order, new_status, request.user, notes)

        return Response(PurchaseOrderSerializer(purchase_order, context={"request": request}).data)

    def _tell_about(self, po, new_status, actor, notes=""):
        """Who hears about a move: the approvers when it goes up, the buyer
        when it is decided, the store when goods are coming."""
        link = f"/procurement?po={po.pk}"
        ref = f"po:{po.pk}"
        money = f"{po.currency} {po.total_amount:,.0f}"
        S = PurchaseOrder.Status
        if new_status == S.PENDING_APPROVAL:
            notices.ask(
                "approve_po", exclude=[actor],
                title=f"{po.po_number} needs your approval",
                message=f"{po.supplier.name} · {money} · raised by {notices.who(po.ordered_by)}",
                link=link, ref=ref, data={"po": str(po.pk)},
            )
            return
        notices.resolve(ref)
        if new_status in (S.APPROVED, S.DRAFT, S.CANCELLED) and po.ordered_by_id:
            said = {
                S.APPROVED: f"{po.po_number} approved",
                S.DRAFT: f"{po.po_number} sent back to draft",
                S.CANCELLED: f"{po.po_number} cancelled",
            }[new_status]
            notices.tell(
                [po.ordered_by], exclude=[actor], kind="approval_decided",
                title=said,
                message=f"by {notices.who(actor)}" + (f" — {notes}" if notes else ""),
                link=link, data={"po": str(po.pk)},
            )
        if new_status == S.APPROVED:
            # The order is placed: the store will have goods to receive.
            notices.tell(
                notices.holders_of("inspect_goods", exclude=[actor]),
                title=f"{po.po_number} placed with {po.supplier.name}",
                message=f"{money} · delivery by {po.expected_delivery or 'date not set'}",
                link=link, data={"po": str(po.pk)},
            )


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrderItem.objects.select_related(
        "purchase_order", "asset_type", "device_model", "material_type"
    ).all()
    serializer_class = PurchaseOrderItemDetailSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_procurement"
    write_capability = "raise_po"
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
