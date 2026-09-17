import uuid

from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from common.permissions import (
    MANAGER_ROLES,
    AdminManagerWriteElseRead,
    WarehouseWriteElseRead,
)

from .labels import render_label, render_labels_pdf

from .models import (
    AssetCode,
    AssetComponent,
    ComponentTemplate,
    ComponentTemplateLine,
    AssetType,
    Brand,
    Device,
    DeviceImage,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
    ProductionRouteTemplate,
    ProductionRouteTemplateStep,
    ProductionStep,
)
from .serializers import (
    AssetCodeSerializer,
    AssetComponentSerializer,
    AssetTypeSerializer,
    BrandSerializer,
    DeviceAssignmentSerializer,
    DeviceDetailSerializer,
    DeviceImageSerializer,
    DeviceLifecycleEventSerializer,
    DeviceListSerializer,
    DeviceModelSerializer,
    DeviceTransitionSerializer,
    MaterialTypeSerializer,
    ProductionStepSerializer,
    ProductionStepTransitionSerializer,
    assignee_label,
)

# Who may move assets through the status machine: oversight roles plus the
# warehouse team (who receive stock, dispatch and process RMAs).
DEVICE_TRANSITION_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor", "warehouse")

# Hard cap on a bulk label print — one PDF page per device.
LABEL_BATCH_MAX = 200


def persist_asset_code(device, fmt, label_size=None):
    """Create or refresh the device's current AssetCode row for ``fmt``.

    Reuses the ``is_current`` row per (device, format) so repeated prints
    don't stack duplicate records — shared by the single-label and bulk
    label actions so both leave the same ledger behind.
    """
    code_obj = AssetCode.objects.filter(
        device=device, format=fmt, is_current=True
    ).first()
    if code_obj is None:
        code_obj = AssetCode(device=device, format=fmt)
    code_obj.label_size = label_size or code_obj.label_size or "60x30"

    content = render_label(device, fmt)
    code_obj.generated_file.save(content.name, content, save=True)
    return code_obj


class AssetTypeViewSet(viewsets.ModelViewSet):
    queryset = AssetType.objects.all()
    serializer_class = AssetTypeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]


class BrandViewSet(viewsets.ModelViewSet):
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class DeviceModelViewSet(viewsets.ModelViewSet):
    queryset = DeviceModel.objects.select_related("brand").all()
    serializer_class = DeviceModelSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["brand", "is_active"]
    search_fields = ["name", "model_number"]


class MaterialTypeViewSet(viewsets.ModelViewSet):
    # Inventory master data: the warehouse team open stock items and need to
    # name a new material at that moment, so they get the same write access
    # they already have on inventory items and categories.
    queryset = MaterialType.objects.all()
    serializer_class = MaterialTypeSerializer
    permission_classes = [IsAuthenticated, WarehouseWriteElseRead]
    filterset_fields = ["category"]
    search_fields = ["name", "category__name"]


class DeviceViewSet(viewsets.ModelViewSet):
    queryset = Device.objects.select_related(
        "asset_type", "device_model", "device_model__brand", "current_site",
        "assigned_client", "supplier", "assigned_technician", "installed_by", "project",
    ).prefetch_related(
        "images", "warranties", "clients", "project_scope_items__project",
    ).all()
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = [
        "status", "source", "asset_type", "device_model", "current_site",
        "assigned_client", "assigned_technician", "project",
    ]
    search_fields = [
        "asset_code", "serial_number", "mobile_id", "mac_address", "display_name",
        "device_model__name", "current_site__name", "assigned_client__name",
    ]
    ordering_fields = ["created_at", "asset_code", "status", "installation_date"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Vendor scope (XC-04): portal users only see devices that appear in
        # tickets assigned to their supplier or installations their supplier
        # is doing — read-only (vendors are in no write-role group).
        user = self.request.user
        if getattr(user, "role", "") == "vendor" and not user.is_superuser:
            if not user.supplier_id:
                return qs.none()
            qs = qs.filter(
                Q(tickets__assigned_vendor_id=user.supplier_id)
                | Q(linked_tickets__assigned_vendor_id=user.supplier_id)
                | Q(installations__vendor_id=user.supplier_id)
            ).distinct()
        # ?flag=operational|warranty_expired — dashboard drill-downs; lives
        # here (not filterset_fields) so list AND export share it. Unknown
        # values are ignored.
        flag = self.request.query_params.get("flag")
        if flag == "operational":
            qs = qs.filter(status__in=[Device.Status.ACTIVE, Device.Status.INSTALLED])
        elif flag == "warranty_expired":
            qs = (
                qs.filter(warranties__isnull=False)
                .exclude(warranties__status="active")
                .distinct()
            )
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceListSerializer
        return DeviceDetailSerializer

    def get_permissions(self):
        # These actions carry their own role gate (supervisor and warehouse may
        # transition/reassign but are read-only elsewhere).
        if self.action in ("transition", "reassign"):
            return [IsAuthenticated()]
        return super().get_permissions()

    # ── Status transition (guarded state machine) ─────────────────────

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """Move the device through the status machine, journalling the flip.

        The reason is mandatory — signals write it into the lifecycle event
        and the audit trail alongside who performed the change.
        """
        device = self.get_object()
        role = getattr(request.user, "role", "")
        # The field technician who owns this asset closes out their own
        # installation (installed → active); everything else stays with the
        # oversight roles.
        owns_installation = request.user.id in (
            device.assigned_technician_id, device.installed_by_id,
        )
        if role not in DEVICE_TRANSITION_ROLES and not (role == "technician" and owns_installation):
            return Response(
                {"detail": "You do not have permission to change asset status."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = DeviceTransitionSerializer(data=request.data, context={"device": device})
        ser.is_valid(raise_exception=True)

        new_status = ser.validated_data["status"]

        if role not in DEVICE_TRANSITION_ROLES:
            # Installed and Active are the technician's to record, but they are
            # recorded in the Installation Tracker with the site work and photo.
            return Response(
                {"detail": (
                    "Installation progress is recorded in the Installation Tracker, not in "
                    "the registry. Ask operations for any other status change."
                )},
                status=status.HTTP_403_FORBIDDEN,
            )
        update_fields = ["status", "updated_at"]
        reason = ser.validated_data["reason"]

        # Moving to `assigned` records who it went to — an internal technician
        # or an external vendor — and names them in the journalled reason.
        if new_status == Device.Status.ASSIGNED:
            technician = ser.validated_data.get("assigned_technician")
            vendor = (ser.validated_data.get("assigned_vendor_name") or "").strip()
            contact = (ser.validated_data.get("assigned_vendor_contact") or "").strip()

            # Only a turnkey job has an installing vendor, and there our
            # technician oversees them; every other route is one or the
            # other, so naming a technician clears the vendor.
            vendor_route = device.source == Device.Source.VENDOR_TURNKEY
            device.assigned_technician = technician
            device.assigned_vendor_name = vendor if (vendor_route or not technician) else ""
            device.assigned_vendor_contact = contact if (vendor_route or not technician) else ""
            update_fields += [
                "assigned_technician", "assigned_vendor_name", "assigned_vendor_contact",
            ]

            site = ser.validated_data.get("current_site") or device.current_site
            if site != device.current_site:
                device.current_site = site
                update_fields.append("current_site")

            reason = f"{reason} · Assigned to {assignee_label(technician, vendor, contact)}"

        if new_status == Device.Status.UNDER_MAINTENANCE:
            # Read by the maintenance app's signal when it raises the job.
            device._maintenance_details = {
                "next_due": ser.validated_data.get("maintenance_due"),
                "priority": ser.validated_data.get("maintenance_priority") or "high",
                "assigned_to": ser.validated_data.get("maintenance_assigned_to"),
                "instructions": ser.validated_data.get("maintenance_instructions", ""),
            }

        device.status = new_status
        device._transition_user = request.user
        device._transition_reason = reason
        device.save(update_fields=update_fields)

        device.refresh_from_db()
        return Response(DeviceDetailSerializer(device, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reassign(self, request, pk=None):
        """Hand an assigned asset to a different technician or vendor.

        Status is untouched — this is for changing the assignee mid-flight,
        which the status machine can't express (``assigned`` is not a valid
        transition target from ``assigned``). Journalled as a Reassignment.
        """
        device = self.get_object()
        if getattr(request.user, "role", "") not in DEVICE_TRANSITION_ROLES:
            return Response(
                {"detail": "You do not have permission to reassign assets."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if device.status != Device.Status.ASSIGNED and not (
            device.assigned_technician_id or device.assigned_vendor_name
        ):
            return Response(
                {"detail": (
                    f"Only an assigned asset can be reassigned (this one is "
                    f"'{device.get_status_display()}' with no assignee)."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = DeviceAssignmentSerializer(data=request.data, context={"device": device})
        ser.is_valid(raise_exception=True)

        previous = DeviceDetailSerializer(device, context={"request": request}).data["assigned_to_display"]
        technician = ser.validated_data.get("assigned_technician")
        vendor = (ser.validated_data.get("assigned_vendor_name") or "").strip()
        contact = (ser.validated_data.get("assigned_vendor_contact") or "").strip()

        vendor_route = device.source == Device.Source.VENDOR_TURNKEY
        device.assigned_technician = technician
        device.assigned_vendor_name = vendor if (vendor_route or not technician) else ""
        device.assigned_vendor_contact = contact if (vendor_route or not technician) else ""
        device.save(update_fields=[
            "assigned_technician", "assigned_vendor_name", "assigned_vendor_contact", "updated_at",
        ])

        new_label = assignee_label(technician, vendor, contact)
        # No status change here, so the status signal won't fire — journal it.
        DeviceLifecycleEvent.objects.create(
            device=device,
            event_type=DeviceLifecycleEvent.EventType.REASSIGNMENT,
            from_value=previous or "",
            to_value=new_label,
            description=f"{ser.validated_data['reason']} · Reassigned to {new_label}",
            performed_by=request.user,
        )

        device.refresh_from_db()
        return Response(DeviceDetailSerializer(device, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="apply-route-template")
    def apply_route_template(self, request, pk=None):
        """Lay out this asset's build from the standard route for its type."""
        device = self.get_object()
        if device.source != Device.Source.INHOUSE:
            return Response(
                {"detail": "Only in-house builds have a production route."}, status=400
            )
        if not device.asset_type_id:
            return Response(
                {"detail": "Give the asset a type first — routes are held per asset type."},
                status=400,
            )
        template = ProductionRouteTemplate.objects.filter(asset_type=device.asset_type).first()
        if template is None:
            return Response(
                {"detail": (
                    f"No standard route saved for {device.asset_type.name} yet — "
                    f"define the steps here and save them as the route."
                )},
                status=404,
            )
        if device.production_steps.exists():
            return Response(
                {"detail": "This asset already has a route; clear it before applying another."},
                status=400,
            )

        with transaction.atomic():
            created = [
                ProductionStep.objects.create(
                    device=device,
                    step_number=step.step_number,
                    name=step.name,
                    location=step.location,
                    workshop=step.workshop,
                    workshop_name=step.workshop_name,
                    expected_days=step.expected_days,
                )
                for step in template.steps.all()
            ]
        return Response(
            {"applied": len(created), "steps": ProductionStepSerializer(created, many=True).data}
        )

    @action(detail=True, methods=["post"], url_path="save-route-template")
    def save_route_template(self, request, pk=None):
        """Keep this asset's route as the standard for its type.

        Producing a type for the first time means working the sequence out;
        saving it means the next one starts from it.
        """
        device = self.get_object()
        if not device.asset_type_id:
            return Response(
                {"detail": "Give the asset a type first — routes are held per asset type."},
                status=400,
            )
        steps = list(device.production_steps.all())
        if not steps:
            return Response({"detail": "There is no route on this asset to save."}, status=400)

        with transaction.atomic():
            template, _ = ProductionRouteTemplate.objects.get_or_create(
                asset_type=device.asset_type, defaults={"created_by": request.user},
            )
            # Replace wholesale: the asset in hand is the current definition.
            template.steps.all().delete()
            for step in steps:
                ProductionRouteTemplateStep.objects.create(
                    template=template,
                    step_number=step.step_number,
                    name=step.name,
                    location=step.location,
                    workshop=step.workshop,
                    workshop_name=step.workshop_name,
                    expected_days=step.expected_days,
                )
        return Response({
            "asset_type": device.asset_type.name,
            "saved_steps": len(steps),
            "detail": f"Saved as the standard route for {device.asset_type.name}.",
        })

    @action(detail=True, methods=["post"], url_path="apply-component-template")
    def apply_component_template(self, request, pk=None):
        """Fill this asset's parts list from the standard one for its type."""
        device = self.get_object()
        if device.source != Device.Source.INHOUSE:
            return Response(
                {"detail": "Only in-house builds are assembled from our inventory."}, status=400
            )
        if not device.asset_type_id:
            return Response(
                {"detail": "Give the asset a type first — parts lists are held per asset type."},
                status=400,
            )
        template = ComponentTemplate.objects.filter(asset_type=device.asset_type).first()
        if template is None:
            return Response(
                {"detail": (
                    f"No standard components saved for {device.asset_type.name} yet — "
                    f"add them here and save them as the standard."
                )},
                status=404,
            )
        if device.components.exists():
            return Response(
                {"detail": "This asset already has components; clear them before applying a set."},
                status=400,
            )

        created = []
        with transaction.atomic():
            for line in template.lines.select_related("inventory_item", "inventory_unit_type"):
                # Go through the serializer so the name, category and supplier
                # are derived from inventory exactly as a hand-added line is.
                ser = AssetComponentSerializer(data={
                    "device": str(device.pk),
                    "inventory_item": str(line.inventory_item_id) if line.inventory_item_id else None,
                    "inventory_unit_type": (
                        str(line.inventory_unit_type_id) if line.inventory_unit_type_id else None
                    ),
                    "quantity": line.quantity,
                })
                ser.is_valid(raise_exception=True)
                created.append(ser.save())
        return Response({
            "applied": len(created),
            "components": AssetComponentSerializer(created, many=True).data,
        })

    @action(detail=True, methods=["post"], url_path="save-component-template")
    def save_component_template(self, request, pk=None):
        """Keep this asset's parts list as the standard for its type."""
        device = self.get_object()
        if not device.asset_type_id:
            return Response(
                {"detail": "Give the asset a type first — parts lists are held per asset type."},
                status=400,
            )
        components = list(device.components.all())
        if not components:
            return Response({"detail": "There are no components on this asset to save."}, status=400)

        with transaction.atomic():
            template, _ = ComponentTemplate.objects.get_or_create(
                asset_type=device.asset_type, defaults={"created_by": request.user},
            )
            # Replace wholesale: the asset in hand is the current definition.
            template.lines.all().delete()
            for component in components:
                ComponentTemplateLine.objects.create(
                    template=template,
                    inventory_item=component.inventory_item,
                    inventory_unit_type=component.inventory_unit_type,
                    quantity=component.quantity,
                )
        return Response({
            "asset_type": device.asset_type.name,
            "saved_lines": len(components),
            "detail": f"Saved as the standard components for {device.asset_type.name}.",
        })

    @action(detail=True, methods=["get"])
    def lifecycle(self, request, pk=None):
        device = self.get_object()
        events = device.lifecycle_events.order_by("-created_at")
        page = self.paginate_queryset(events)
        if page is not None:
            serializer = DeviceLifecycleEventSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = DeviceLifecycleEventSerializer(events, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def status_summary(self, request):
        """Device count per status for dashboard widgets."""
        summary = Device.objects.values("status").annotate(count=Count("id")).order_by("status")
        return Response(summary)

    @action(detail=False, methods=["get"])
    def dashboard_stats(self, request):
        """Comprehensive device stats for the main dashboard."""
        total = Device.objects.count()
        by_status = dict(
            Device.objects.values_list("status").annotate(c=Count("id")).values_list("status", "c")
        )
        active_count = by_status.get("active", 0) + by_status.get("installed", 0)

        by_city = list(
            Device.objects.filter(current_site__isnull=False)
            .values(city=F("current_site__city"))
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
        by_region = list(
            Device.objects.filter(current_site__isnull=False)
            .values(region=F("current_site__state_province"))
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        by_model_type = list(
            Device.objects.values(screen_type=F("device_model__screen_type"))
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        return Response({
            "total": total,
            "working": active_count,
            "installed": by_status.get("installed", 0) + by_status.get("active", 0),
            "out_of_order": by_status.get("decommissioned", 0) + by_status.get("lost_stolen", 0),
            "under_maintenance": by_status.get("under_maintenance", 0),
            "in_stock": by_status.get("in_stock", 0),
            "by_status": by_status,
            "by_city": by_city,
            "by_region": by_region,
            "by_model_type": by_model_type,
        })

    @action(detail=False, methods=["get"])
    def map_data(self, request):
        """Device locations with status for map rendering."""
        devices = (
            Device.objects.filter(
                current_site__isnull=False,
                current_site__latitude__isnull=False,
                current_site__longitude__isnull=False,
            )
            .select_related("current_site")
            .annotate(
                open_tickets=Count(
                    "tickets",
                    filter=~Q(tickets__status__in=["closed", "approved", "rejected"]),
                )
            )
            .values(
                "id", "asset_code", "status", "open_tickets",
                "current_site__id",
                "current_site__name", "current_site__city",
                "current_site__state_province", "current_site__country",
                "current_site__latitude", "current_site__longitude",
            )
        )
        return Response(list(devices))

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of the asset registry (XC-01).

        Respects the caller's filters/search/ordering; capped at
        EXPORT_MAX_ROWS; journalled in the audit trail.
        """
        from .serializers import _warranty_status

        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Asset Code", "Serial Number", "Name", "Model", "Asset Type",
            "Status", "Source", "Batch Number", "Client", "Site", "Project",
            "Purchase Date", "Purchase Price", "Warranty Status",
        ]
        rows = []
        for d in qs:
            rows.append([
                d.asset_code,
                d.serial_number,
                d.display_name,
                str(d.device_model) if d.device_model_id else "",
                d.asset_type.name if d.asset_type_id else "",
                d.get_status_display(),
                d.get_source_display(),
                d.batch_number,
                d.assigned_client.name if d.assigned_client_id else "",
                d.current_site.name if d.current_site_id else "",
                d.project.name if d.project_id else "",
                d.purchase_date,
                d.purchase_price,
                _warranty_status(d),
            ])
        log_export(request.user, "device", len(rows), export_params(request))
        return xlsx_response("assets", "Assets", columns, rows)

    @action(detail=True, methods=["post"], url_path="ready-for-installation")
    def ready_for_installation(self, request, pk=None):
        """The build is finished: a finished route takes the asset to In Stock so
        it can be assigned to a site. Already stocked assets pass straight through."""
        device = self.get_object()
        if device.status == Device.Status.IN_STOCK:
            return Response(DeviceDetailSerializer(device, context={"request": request}).data)
        if device.status != Device.Status.IN_PRODUCTION:
            return Response(
                {"detail": f"{device.asset_code} is {device.get_status_display()} — only an asset in production is finished into stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # A vendor-supplied asset has no route of its own to finish.
        if device.source == Device.Source.INHOUSE and not device.route_complete:
            return Response(
                {"detail": "The production route is not finished yet — complete or skip every operation first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        device._transition_user = request.user
        device._transition_reason = (
            "Production route complete — ready to assign for installation"
            if device.source == Device.Source.INHOUSE else "Vendor-supplied asset in hand — ready to assign for installation"
        )
        device.status = Device.Status.IN_STOCK
        device.save(update_fields=["status", "updated_at"])
        return Response(DeviceDetailSerializer(device, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="label")
    def label(self, request, pk=None):
        """Generate (or refresh) the printable QR/barcode label for this device.

        The label encodes ``asset_code``, which the mobile scanner resolves via
        the device search endpoint. Re-generating reuses the current AssetCode
        row so repeated prints don't stack duplicate records.
        """
        device = self.get_object()
        fmt = request.data.get("format", AssetCode.LabelFormat.QR)
        if fmt not in AssetCode.LabelFormat.values:
            return Response(
                {"format": [f"Must be one of: {', '.join(AssetCode.LabelFormat.values)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code_obj = persist_asset_code(device, fmt, request.data.get("label_size"))
        return Response(AssetCodeSerializer(code_obj, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="labels")
    def labels(self, request):
        """Bulk label print (WF-05): one PDF, one label per page.

        Body: ``{"ids": [uuid, ...], "format": "qr"|"code128"}``. Uses the
        same rendering, AssetCode ledger and role gate as the single-label
        action (POST under AdminManagerWriteElseRead → managers only).
        Capped at ``LABEL_BATCH_MAX`` devices per request; any id that
        doesn't resolve fails the whole batch with a 400 naming it.
        """
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids:
            return Response(
                {"ids": ["Provide a non-empty list of device ids."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ids) > LABEL_BATCH_MAX:
            return Response(
                {"ids": [f"At most {LABEL_BATCH_MAX} devices per batch (got {len(ids)})."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            ids = [uuid.UUID(str(i)) for i in ids]
        except (ValueError, AttributeError, TypeError):
            return Response(
                {"ids": ["All ids must be valid UUIDs."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Repeated ids collapse to one page/label — dedupe before resolving
        # so the missing-id check compares against distinct ids only.
        ids = list(dict.fromkeys(ids))

        fmt = request.data.get("format", AssetCode.LabelFormat.QR)
        if fmt not in AssetCode.LabelFormat.values:
            return Response(
                {"format": [f"Must be one of: {', '.join(AssetCode.LabelFormat.values)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        devices = list(
            self.get_queryset().filter(pk__in=ids).order_by("asset_code")
        )
        # A partial batch must not silently print fewer pages — name every
        # id that didn't resolve so the caller can fix the selection.
        found = {d.pk for d in devices}
        missing = [str(i) for i in ids if i not in found]
        if missing:
            return Response(
                {"ids": [f"Unknown device ids: {', '.join(missing)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Persist the ledger exactly like the single-label action so bulk
        # prints leave the same AssetCode trail behind.
        label_size = request.data.get("label_size")
        for device in devices:
            persist_asset_code(device, fmt, label_size)

        pdf = render_labels_pdf(devices, fmt)
        filename = f"labels-{fmt}-{timezone.localdate().isoformat()}.pdf"
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


def _budget_block_response(component):
    """A 400 when the asset's project has a budget that is not approved yet.

    Planning precedes execution: until the estimate is signed off, nothing is
    drawn from stock or bought for the project.
    """
    from apps.teams.costing import blocking_budget

    project = blocking_budget(component.device)
    if project is None:
        return None
    plan = project.cost_plan
    return Response(
        {"detail": (
            f"The budget for {project.name} is {plan.get_status_display().lower()} — "
            f"execution starts once it is approved."
        )},
        status=400,
    )


def _refuse_if_vendor_asset(device, what: str):
    """A vendor-supplied asset arrives complete: it is bought, not built, so
    it carries no components or production route of its own."""
    if device is not None and device.source != Device.Source.INHOUSE:
        raise ValidationError({
            "device": f"{device.asset_code} is {device.get_source_display()} — it arrives complete "
                      f"from the vendor and has no {what} of its own."
        })


class AssetComponentViewSet(viewsets.ModelViewSet):
    queryset = (
        AssetComponent.objects.select_related(
            "device", "supplier", "inventory_item__material_type", "inventory_unit", "inventory_unit_type"
        )
        .prefetch_related("warranties")
        .all()
    )
    serializer_class = AssetComponentSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "component_type", "inventory_item", "inventory_unit", "inventory_unit_type"]
    search_fields = ["name", "serial_number"]

    # Components are requirements, not withdrawals: adding or removing one
    # never moves stock. The warehouse is touched only by the fulfilment
    # actions below, which the project screen drives.

    def get_permissions(self):
        # Asking for more is not the same as being granted it. The people who
        # meet the shortfall on site are the ones who know about it, so anyone
        # may raise the request; only a manager decides it.
        if self.action == "increase_quantity":
            return [IsAuthenticated()]
        return super().get_permissions()

    # 10: once the project is executing, the build definition is frozen. The
    # budget approved this parts list and this route; only status moves now.
    def _refuse_if_locked(self, device):
        if device is not None and device.is_locked:
            raise PermissionDenied(
                f"{device.asset_code} is in execution — its components and production "
                "route are fixed. Only the asset's status can change now."
            )

    def perform_create(self, serializer):
        self._refuse_if_locked(serializer.validated_data.get("device"))
        _refuse_if_vendor_asset(serializer.validated_data.get("device"), "components" if isinstance(self, AssetComponentViewSet) else "production route")
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._refuse_if_locked(serializer.instance.device)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._refuse_if_locked(instance.device)
        super().perform_destroy(instance)


    @action(detail=True, methods=["post"], url_path="fulfil-from-stock")
    def fulfil_from_stock(self, request, pk=None):
        """Ask the store to cover this requirement (or part of it) from stock.

        Stock leaves the warehouse from one desk only, so deciding to use
        inventory raises a request rather than moving the goods here. The
        storekeeper issues it — in full or in part — and that is what advances
        the requirement.
        """
        from apps.inventory.models import IssuanceRequest

        component = self.get_object()
        blocked = _budget_block_response(component)
        if blocked is not None:
            return blocked
        try:
            quantity = int(request.data.get("quantity", component.outstanding_quantity))
        except (TypeError, ValueError):
            return Response({"quantity": ["Must be a whole number."]}, status=400)
        if quantity < 1:
            return Response({"quantity": ["Ask for at least one."]}, status=400)
        if quantity > component.outstanding_quantity:
            return Response({"quantity": [
                f"Only {component.outstanding_quantity} of this requirement is still outstanding."
            ]}, status=400)

        # Anything already queued counts against what is left to ask for.
        already_open = sum(
            row.outstanding_quantity
            for row in component.issuance_requests.exclude(
                status=IssuanceRequest.Status.CANCELLED
            )
        )
        if already_open + quantity > component.outstanding_quantity:
            return Response({"quantity": [
                f"{already_open} already waiting with the store on this line."
            ]}, status=400)

        with transaction.atomic():
            component.fulfilment = AssetComponent.Fulfilment.FROM_STOCK
            component.save(update_fields=["fulfilment", "updated_at"])
            issuance_request = IssuanceRequest.objects.create(
                item=component.inventory_item,
                unit_type=component.inventory_unit_type,
                quantity_requested=quantity,
                source=IssuanceRequest.Source.PROJECT,
                purpose=f"{component.device.asset_code} · {component.name}",
                project=component.device.project,
                asset_component=component,
                requested_by=request.user,
            )

        component.refresh_from_db()
        return Response({
            "component": AssetComponentSerializer(component).data,
            "requested": quantity,
            "request_number": issuance_request.request_number,
        })

    @action(detail=True, methods=["post"], url_path="mark-for-procurement")
    def mark_for_procurement(self, request, pk=None):
        """Flag this requirement to be bought.

        Deliberately allowed even when the warehouse has enough — whether to
        consume stock or buy new is the user's call, not the system's.
        """
        component = self.get_object()
        blocked = _budget_block_response(component)
        if blocked is not None:
            return blocked
        if component.outstanding_quantity == 0:
            return Response(
                {"detail": "This requirement is already fully covered."}, status=400
            )
        component.fulfilment = AssetComponent.Fulfilment.PROCUREMENT
        component.save(update_fields=["fulfilment", "updated_at"])
        # The goods still come through the store: queue the issue now, marked
        # as waiting on procurement, so it is issued from stock once received.
        from apps.inventory.models import IssuanceRequest

        already_open = component.issuance_requests.exclude(
            status=IssuanceRequest.Status.CANCELLED
        ).exclude(status=IssuanceRequest.Status.FULFILLED).exists()
        if not already_open and component.outstanding_quantity > 0:
            IssuanceRequest.objects.create(
                item=component.inventory_item,
                unit_type=component.inventory_unit_type,
                quantity_requested=component.outstanding_quantity,
                source=IssuanceRequest.Source.PROJECT,
                purpose=f"{component.device.asset_code} · {component.name} — procurement in progress",
                project=component.device.project,
                asset_component=component,
                requested_by=request.user,
            )
        # Journalled on the asset so the decision is visible there, but the
        # build has not started, so the status is left alone.
        DeviceLifecycleEvent.objects.create(
            device=component.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=(
                f"{component.name} × {component.outstanding_quantity} flagged for procurement"
            ),
            performed_by=request.user,
            metadata={"component": str(component.pk)},
        )
        return Response(AssetComponentSerializer(component).data)

    # Why a requirement grew once the job was under way. On site the plan
    # meets reality: parts get damaged in handling, the count was wrong, or
    # something arrives faulty — and the project still has to be finished.
    INCREASE_REASONS = {
        "damaged": "Damaged / manhandled",
        "miscalculated": "Miscalculated",
        "faulty": "Faulty on arrival",
        "other": "Other",
    }

    @action(detail=True, methods=["post"], url_path="increase-quantity")
    def increase_quantity(self, request, pk=None):
        """Ask for more than was planned, recording why.

        The extra spends money that was already signed off, so the request
        waits for a manager instead of taking effect where it is typed.
        """
        component = self.get_object()
        if component.pending_increase:
            return Response(
                {"detail": "An increase is already waiting for approval on this line."},
                status=400,
            )
        try:
            additional = int(request.data.get("additional", 0))
        except (TypeError, ValueError):
            return Response({"additional": ["Must be a whole number."]}, status=400)
        if additional < 1:
            return Response({"additional": ["Add at least one."]}, status=400)
        reason = str(request.data.get("reason", ""))
        if reason not in self.INCREASE_REASONS:
            return Response(
                {"reason": [f"Choose one of: {', '.join(self.INCREASE_REASONS)}."]}, status=400
            )
        notes = (request.data.get("notes") or "").strip()
        if reason == "other" and not notes:
            return Response({"notes": ["Say what happened."]}, status=400)

        component.pending_increase = additional
        component.increase_reason = reason
        component.increase_notes = notes
        component.increase_requested_by = request.user
        component.increase_requested_at = timezone.now()
        component.save(update_fields=[
            "pending_increase", "increase_reason", "increase_notes",
            "increase_requested_by", "increase_requested_at", "updated_at",
        ])

        label = self.INCREASE_REASONS[reason]
        DeviceLifecycleEvent.objects.create(
            device=component.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=(
                f"{component.name}: increase of {additional} requested "
                f"({label})" + (f" — {notes}" if notes else "")
            ),
            performed_by=request.user,
            metadata={
                "component": str(component.pk), "reason": reason, "additional": additional,
            },
        )
        component.refresh_from_db()
        return Response(AssetComponentSerializer(component).data)

    def _increase_decision(self, request, component):
        """Guard shared by the two decisions on a requested increase."""
        from common.permissions import MANAGER_ROLES

        if getattr(request.user, "role", "") not in MANAGER_ROLES:
            return Response(
                {"detail": "Only a manager can decide a quantity increase."}, status=403
            )
        if not component.pending_increase:
            return Response(
                {"detail": "Nothing is waiting for approval on this line."}, status=400
            )
        return None

    CLEARED_INCREASE_FIELDS = [
        "pending_increase", "increase_reason", "increase_notes",
        "increase_requested_by", "increase_requested_at", "updated_at",
    ]

    def _clear_increase(self, component):
        component.pending_increase = None
        component.increase_reason = ""
        component.increase_notes = ""
        component.increase_requested_by = None
        component.increase_requested_at = None

    @action(detail=True, methods=["post"], url_path="approve-increase")
    def approve_increase(self, request, pk=None):
        """Grant a requested increase, raising what the asset needs."""
        component = self.get_object()
        denied = self._increase_decision(request, component)
        if denied is not None:
            return denied

        additional = component.pending_increase
        label = self.INCREASE_REASONS.get(component.increase_reason, component.increase_reason)
        notes, asked_by = component.increase_notes, component.increase_requested_by
        before = component.quantity
        component.quantity = before + additional
        fields = ["quantity", *self.CLEARED_INCREASE_FIELDS]
        # A line that was fully covered is short again, so it needs a fresh
        # stock-or-procure decision for the extra.
        if component.fulfilment == AssetComponent.Fulfilment.FULFILLED:
            component.fulfilment = AssetComponent.Fulfilment.PENDING
            fields.append("fulfilment")
        self._clear_increase(component)
        component.save(update_fields=fields)

        DeviceLifecycleEvent.objects.create(
            device=component.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=(
                f"{component.name}: required quantity raised {before} → {component.quantity} "
                f"({label}, approved)" + (f" — {notes}" if notes else "")
            ),
            performed_by=request.user,
            metadata={
                "component": str(component.pk), "reason": label,
                "from": before, "to": component.quantity,
                "requested_by": str(asked_by.pk) if asked_by else None,
            },
        )
        component.refresh_from_db()
        return Response(AssetComponentSerializer(component).data)

    @action(detail=True, methods=["post"], url_path="reject-increase")
    def reject_increase(self, request, pk=None):
        """Turn a requested increase down; the requirement does not move."""
        component = self.get_object()
        denied = self._increase_decision(request, component)
        if denied is not None:
            return denied

        additional = component.pending_increase
        decision_notes = (request.data.get("notes") or "").strip()
        self._clear_increase(component)
        component.save(update_fields=self.CLEARED_INCREASE_FIELDS)

        DeviceLifecycleEvent.objects.create(
            device=component.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=(
                f"{component.name}: increase of {additional} turned down"
                + (f" — {decision_notes}" if decision_notes else "")
            ),
            performed_by=request.user,
            metadata={"component": str(component.pk), "additional": additional},
        )
        component.refresh_from_db()
        return Response(AssetComponentSerializer(component).data)

    @action(detail=True, methods=["post"], url_path="reset-fulfilment")
    def reset_fulfilment(self, request, pk=None):
        """Undo the decision, returning anything already issued to stock."""
        from apps.inventory.models import IssuanceRequest
        from apps.inventory.services import return_stock_for_component

        component = self.get_object()
        with transaction.atomic():
            # Anything still queued with the store is no longer wanted.
            component.issuance_requests.exclude(
                status=IssuanceRequest.Status.CANCELLED
            ).update(status=IssuanceRequest.Status.CANCELLED)
            return_stock_for_component(component, request.user)
            component.refresh_from_db()
            component.fulfilment = AssetComponent.Fulfilment.PENDING
            # Release the PO link too: the decision is being undone, so the
            # requirement must be offerable for procurement again.
            component.purchase_order_item = None
            component.save(update_fields=["fulfilment", "purchase_order_item", "updated_at"])
        component.refresh_from_db()
        return Response(AssetComponentSerializer(component).data)


class ProductionStepViewSet(viewsets.ModelViewSet):
    """The production route for an in-house build.

    The initiator lays the steps out up front; the shop floor walks them
    through a guarded status flow so the asset always says where it physically
    is — including while it sits at an outside workshop.
    """

    queryset = ProductionStep.objects.select_related(
        "device", "workshop", "assigned_to"
    ).all()
    serializer_class = ProductionStepSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]

    # 10: once the project is executing, the build definition is frozen. The
    # budget approved this parts list and this route; only status moves now.
    def _refuse_if_locked(self, device):
        if device is not None and device.is_locked:
            raise PermissionDenied(
                f"{device.asset_code} is in execution — its components and production "
                "route are fixed. Only the asset's status can change now."
            )

    def perform_create(self, serializer):
        self._refuse_if_locked(serializer.validated_data.get("device"))
        _refuse_if_vendor_asset(serializer.validated_data.get("device"), "components" if isinstance(self, AssetComponentViewSet) else "production route")
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._refuse_if_locked(serializer.instance.device)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._refuse_if_locked(instance.device)
        super().perform_destroy(instance)

    filterset_fields = ["device", "status", "location"]
    search_fields = ["name", "workshop_name", "device__asset_code"]
    ordering_fields = ["step_number", "created_at"]

    def get_permissions(self):
        # The technician running the build advances its steps.
        if self.action == "transition":
            return [IsAuthenticated()]
        return super().get_permissions()

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        """Shift a step one place up or down the route.

        Sequence is the whole point of a routing, and the order it was typed in
        is rarely the order it should run in. Numbers are swapped with the
        neighbour rather than renumbered, so nothing else in the route shifts.
        """
        step = self.get_object()
        self._refuse_if_locked(step.device)
        direction = str(request.data.get("direction", "")).lower()
        if direction not in ("up", "down"):
            return Response({"direction": "Send 'up' or 'down'."}, status=400)

        siblings = ProductionStep.objects.filter(device_id=step.device_id)
        if direction == "up":
            neighbour = siblings.filter(step_number__lt=step.step_number).order_by("-step_number").first()
        else:
            neighbour = siblings.filter(step_number__gt=step.step_number).order_by("step_number").first()
        if neighbour is None:
            return Response(
                {"detail": f"'{step.name}' is already {'first' if direction == 'up' else 'last'}."},
                status=400,
            )

        with transaction.atomic():
            # (device, step_number) is unique, so park one number out of the
            # way before the swap instead of colliding mid-update.
            parking = (
                siblings.order_by("-step_number").values_list("step_number", flat=True).first() or 0
            ) + 1
            mine, theirs = step.step_number, neighbour.step_number
            ProductionStep.objects.filter(pk=step.pk).update(step_number=parking)
            ProductionStep.objects.filter(pk=neighbour.pk).update(step_number=mine)
            ProductionStep.objects.filter(pk=step.pk).update(step_number=theirs)
            # Renumber so the sequence reads 1..n with no gaps left by deletes;
            # park everything high first so no two rows collide on the way.
            ordered = list(siblings.order_by("step_number", "created_at").values_list("pk", flat=True))
            offset = len(ordered) + parking
            for n, pk in enumerate(ordered, start=1):
                ProductionStep.objects.filter(pk=pk).update(step_number=offset + n)
            for n, pk in enumerate(ordered, start=1):
                ProductionStep.objects.filter(pk=pk).update(step_number=n)

        steps = ProductionStep.objects.filter(device_id=step.device_id).select_related(
            "workshop", "assigned_to"
        )
        return Response(ProductionStepSerializer(steps, many=True).data)

    @action(detail=True, methods=["post"], url_path="decide")
    def decide(self, request, pk=None):
        """Execution decision for one operation: done in-house. (Giving it to a
        workshop is a work order, raised from the project.)"""
        step = self.get_object()
        if step.work_orders.exclude(status="cancelled").exists():
            return Response(
                {"detail": f"'{step.name}' is on a work order — cancel that first to bring it in-house."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        step.location = ProductionStep.Location.IN_HOUSE
        step.workshop = None
        step.workshop_name = ""
        step.save(update_fields=["location", "workshop", "workshop_name", "updated_at"])
        return Response(ProductionStepSerializer(step).data)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Advance one step, stamping the time that matters for that move."""
        step = self.get_object()
        role = getattr(request.user, "role", "")
        owns = request.user.id in (step.assigned_to_id, step.device.assigned_technician_id)
        if role not in DEVICE_TRANSITION_ROLES and not (role == "technician" and owns):
            return Response(
                {"detail": "You do not have permission to advance this production step."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = ProductionStepTransitionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]

        if new_status == step.status:
            return Response({"detail": "The step is already in that status."}, status=400)
        if not step.can_transition_to(new_status):
            allowed = ", ".join(ProductionStep.VALID_TRANSITIONS.get(step.status, ())) or "none"
            return Response(
                {"detail": f"Cannot move from '{step.status}' to '{new_status}'. Allowed: {allowed}."},
                status=400,
            )

        now = timezone.now()
        stamps = {
            ProductionStep.Status.IN_PROGRESS: "started_at",
            ProductionStep.Status.SENT_OUT: "sent_at",
            ProductionStep.Status.RETURNED: "returned_at",
            ProductionStep.Status.COMPLETED: "completed_at",
        }
        fields = ["status", "updated_at"]
        step.status = new_status
        stamp = stamps.get(new_status)
        if stamp and getattr(step, stamp) is None:
            setattr(step, stamp, now)
            fields.append(stamp)
        if ser.validated_data.get("notes"):
            step.notes = ser.validated_data["notes"]
            fields.append("notes")
        step.save(update_fields=fields)

        # The asset's own history shows the build moving, including trips out.
        where = step.workshop_display
        DeviceLifecycleEvent.objects.create(
            device=step.device,
            event_type=DeviceLifecycleEvent.EventType.NOTE,
            description=(
                f"Production step {step.step_number} '{step.name}' → "
                f"{step.get_status_display()}" + (f" ({where})" if where else "")
            ),
            performed_by=request.user,
            metadata={"step": str(step.pk), "location": step.location},
        )
        return Response(ProductionStepSerializer(step).data)


class DeviceImageViewSet(viewsets.ModelViewSet):
    queryset = DeviceImage.objects.select_related("device").all()
    serializer_class = DeviceImageSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "is_primary"]

    def get_permissions(self):
        # Field technicians photograph the assets they installed; the object
        # level check in perform_create keeps that to their own assets.
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_create(self, serializer):
        device = serializer.validated_data.get("device")
        user = self.request.user
        role = getattr(user, "role", "")
        owns_installation = device is not None and user.id in (
            device.assigned_technician_id, device.installed_by_id,
        )
        if role not in MANAGER_ROLES and not owns_installation:
            raise PermissionDenied("You can only add photos to assets assigned to you.")
        serializer.save()


class DeviceLifecycleEventViewSet(viewsets.ModelViewSet):
    queryset = DeviceLifecycleEvent.objects.select_related("device", "performed_by").all()
    serializer_class = DeviceLifecycleEventSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "event_type"]
    ordering_fields = ["created_at"]


class AssetCodeViewSet(viewsets.ModelViewSet):
    queryset = AssetCode.objects.select_related("device").all()
    serializer_class = AssetCodeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "format", "is_current"]
