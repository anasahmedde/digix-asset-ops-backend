from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead

from .models import WorkOrder, WorkOrderItem
from .pdf import build_work_order_pdf
from .serializers import (
    WorkOrderListSerializer,
    WorkOrderSerializer,
    WorkOrderTransitionSerializer,
)


def _spawn_project_if_needed(work_order: WorkOrder):
    """On approval, create the delivery Project sourced from this Work Order."""
    from apps.teams.models import Project

    if work_order.order_type in (WorkOrder.OrderType.SUPPLY, WorkOrder.OrderType.PRODUCTION, WorkOrder.OrderType.SERVICES):
        return  # a purchase, or services rendered: nothing to install
    if Project.objects.filter(source_work_order=work_order).exists():
        return
    Project.objects.create(
        name=f"Install: {work_order.title}"[:300],
        description=work_order.description,
        client=work_order.client,
        site=work_order.site,
        source_work_order=work_order,
        status=Project.Status.PLANNING,
        start_date=work_order.order_date,
        target_date=work_order.expected_delivery,
    )


def _status_from_lines(work_order) -> str:
    """Where an order stands once its lines have been looked at.

    Every job accepted and the order is finished; anything still waiting keeps
    it on the receiving desk; otherwise the vendor has work in hand again.
    """
    from .models import WorkOrder

    lines = list(work_order.items.all())
    if lines and all(i.accepted for i in lines):
        return WorkOrder.Status.COMPLETED
    if any(i.awaiting_inspection for i in lines):
        return (
            WorkOrder.Status.PARTIALLY_DELIVERED if any(i.with_vendor for i in lines)
            else WorkOrder.Status.DELIVERED
        )
    return WorkOrder.Status.IN_PROGRESS


class WorkOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        WorkOrder.objects.select_related(
            "supplier", "client", "site", "payment_terms", "terms_template",
            "created_by", "approved_by",
        )
        .prefetch_related(
            "items", "items__asset_type", "items__device_model",
            "items__production_step__device",
        )
        .all()
    )
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "order_type", "supplier", "client", "site"]
    search_fields = ["wo_number", "title", "description"]
    ordering_fields = ["created_at", "expected_delivery", "total_amount"]

    def get_serializer_class(self):
        if self.action == "list":
            return WorkOrderListSerializer
        return WorkOrderSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        work_order = self.get_object()
        ser = WorkOrderTransitionSerializer(data=request.data, context={"work_order": work_order})
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        now = timezone.now()

        # The same sign-off as a purchase order: the Group Head approves;
        # Operations move the order everywhere else.
        role = getattr(request.user, "role", "")
        if role == "group_head" and new_status not in (WorkOrder.Status.APPROVED, WorkOrder.Status.DRAFT):
            return Response(
                {"detail": "The Group Head signs work orders off; Operations move them otherwise."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if new_status == WorkOrder.Status.APPROVED and role not in ("super_admin", "group_head"):
            return Response({"detail": "Work orders are approved by the Group Head."}, status=status.HTTP_403_FORBIDDEN)
        if new_status == WorkOrder.Status.COMPLETED:
            return Response(
                {"detail": "Delivered work is completed by inspecting it — Work Orders › Work Receiving."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Which lines came in. A whole delivery is everything still out; a part
        # delivery is the lines the vendor names, and there has to be something
        # left for it to be a part of.
        lines = None
        if new_status in (WorkOrder.Status.DELIVERED, WorkOrder.Status.PARTIALLY_DELIVERED):
            outstanding = [i for i in work_order.items.all() if i.with_vendor]
            if not outstanding:
                return Response(
                    {"detail": f"Nothing on {work_order.wo_number} is still with the vendor."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if new_status == WorkOrder.Status.PARTIALLY_DELIVERED:
                if len(work_order.items.all()) < 2:
                    return Response(
                        {"detail": (
                            "There is one job on this order — it is either delivered or it is not."
                        )},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                wanted = {str(i) for i in (request.data.get("items") or [])}
                if not wanted:
                    return Response(
                        {"items": ["Say which jobs the vendor has finished."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                lines = [i for i in outstanding if str(i.pk) in wanted]
                if len(lines) != len(wanted):
                    return Response(
                        {"items": ["Pick jobs from this order that are still with the vendor."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if len(lines) == len(outstanding):
                    # Everything came in after all: that is a whole delivery.
                    new_status = WorkOrder.Status.DELIVERED
            else:
                lines = outstanding

        update_fields = ["status", "updated_at"]
        work_order.status = new_status

        if new_status == WorkOrder.Status.APPROVED:
            work_order.approved_by = request.user
            work_order.approved_at = now
            update_fields += ["approved_by", "approved_at"]
            # The order date is the day it was signed off.
            if work_order.order_date is None:
                work_order.order_date = timezone.localdate()
                update_fields.append("order_date")
        elif new_status == WorkOrder.Status.ISSUED:
            work_order.issued_at = now
            update_fields += ["issued_at"]
        elif new_status in (WorkOrder.Status.DELIVERED, WorkOrder.Status.PARTIALLY_DELIVERED):
            # The order is dated from the first thing that came in.
            if work_order.delivered_at is None or new_status == WorkOrder.Status.DELIVERED:
                work_order.delivered_at = now
                update_fields += ["delivered_at"]

        work_order.save(update_fields=update_fields)

        if lines:
            for line in lines:
                line.delivered_at = now
                line.inspection_result = ""
                line.save(update_fields=["delivered_at", "inspection_result", "updated_at"])

        if new_status == WorkOrder.Status.APPROVED:
            _spawn_project_if_needed(work_order)

        return Response(WorkOrderSerializer(work_order).data)

    # ── Requests from Execution, and the orders raised from them ──────────

    @action(detail=False, methods=["get"], url_path="requests")
    def requests(self, request):
        """Operations Execution asked to give to a vendor, waiting for an order.

        The project decides *that* an operation goes out; here the vendor is
        chosen and the order raised — one draft for as many operations as go
        to the same vendor.
        """
        from apps.assets.models import ProductionStep
        from apps.teams.models import ProjectScopeItem

        steps = (
            ProductionStep.objects.filter(
                location=ProductionStep.Location.EXTERNAL, work_order_requested_at__isnull=False,
            )
            .exclude(status__in=(ProductionStep.Status.COMPLETED, ProductionStep.Status.SKIPPED))
            .select_related("device", "device__project", "device__device_model")
            .order_by("work_order_requested_at")
        )
        scope = {
            r["device_id"]: (str(r["project_id"]), r["project__name"], r["project__target_date"])
            for r in ProjectScopeItem.objects.values(
                "device_id", "project_id", "project__name", "project__target_date"
            )
        }
        rows = []
        for step in steps:
            if step.live_work_orders().exists():
                continue
            device = step.device
            if device.project_id:
                project, project_name = str(device.project_id), device.project.name
                project_due = device.project.target_date
            else:
                project, project_name, project_due = scope.get(device.id, (None, None, None))
            rows.append({
                "step": str(step.pk),
                "step_number": step.step_number,
                "operation": step.name,
                "device": str(device.pk),
                "asset_code": device.asset_code,
                "asset_name": device.display_name or str(device.device_model),
                "project": project,
                "project_name": project_name,
                # When the project needs it — what the order is dated from.
                "project_target_date": project_due,
                "planned_cost": step.planned_cost,
                "requested_at": step.work_order_requested_at,
                "notes": step.notes,
            })
        return Response({"results": rows})

    @action(detail=False, methods=["post"], url_path="raise")
    def raise_wo(self, request):
        """One draft work order, to one vendor, for the chosen requests.

        Body: steps (ids), supplier, optional amounts {step: amount},
        expected_delivery, terms, notes. Each operation becomes a line priced
        at the amount given or the step's planned cost. The order lands in the
        list as a draft for the same approval a purchase order gets.
        """
        from decimal import Decimal, InvalidOperation

        from apps.assets.models import ProductionStep
        from apps.suppliers.models import Supplier
        from apps.teams.models import Project, ProjectScopeItem

        step_ids = [str(x) for x in (request.data.get("steps") or [])]
        supplier = Supplier.objects.filter(pk=request.data.get("supplier")).first()
        if supplier is None:
            return Response({"supplier": ["Choose the vendor."]}, status=400)
        steps = list(
            ProductionStep.objects.filter(pk__in=step_ids)
            .select_related("device", "device__project", "device__asset_type", "device__device_model")
        )
        if not steps or len(steps) != len(set(step_ids)):
            return Response({"steps": ["Pick at least one request."]}, status=400)
        for step in steps:
            if step.live_work_orders().exists():
                return Response({"steps": [f"'{step.name}' on {step.device.asset_code} is already on a work order."]}, status=400)
            if step.status in (ProductionStep.Status.COMPLETED, ProductionStep.Status.SKIPPED):
                return Response({"steps": [f"'{step.name}' on {step.device.asset_code} is already finished."]}, status=400)

        amounts = request.data.get("amounts") or {}

        def amount_for(step):
            raw = amounts.get(str(step.pk))
            if raw in (None, ""):
                return step.planned_cost or Decimal("0")
            try:
                return Decimal(str(raw))
            except InvalidOperation:
                return step.planned_cost or Decimal("0")

        scope = {
            r["device_id"]: r["project_id"]
            for r in ProjectScopeItem.objects.filter(device_id__in=[s.device_id for s in steps]).values("device_id", "project_id")
        }
        project_ids = {s.device.project_id or scope.get(s.device_id) for s in steps} - {None}
        project = Project.objects.filter(pk=next(iter(project_ids))).first() if len(project_ids) == 1 else None
        devices = {s.device_id for s in steps}
        device = steps[0].device if len(devices) == 1 else None
        codes = sorted({s.device.asset_code for s in steps})
        title = (
            f"{steps[0].name} — {steps[0].device.asset_code}" if len(steps) == 1
            else f"{len(steps)} operations — {', '.join(codes)}"
        )
        if project is not None:
            title = f"{title} — {project.name}"

        with transaction.atomic():
            order = WorkOrder.objects.create(
                title=title[:300],
                description=(request.data.get("notes") or "").strip(),
                order_type=WorkOrder.OrderType.SERVICES,
                supplier=supplier,
                client=project.client if project is not None else None,
                site=(
                    device.current_site if device is not None and device.current_site_id
                    else (project.site if project is not None else None)
                ),
                project=project,
                device=device,
                production_step=steps[0] if len(steps) == 1 else None,
                expected_delivery=request.data.get("expected_delivery") or None,
                terms_conditions=(request.data.get("terms") or "").strip(),
                created_by=request.user,
            )
            for step in steps:
                WorkOrderItem.objects.create(
                    work_order=order,
                    asset_type=step.device.asset_type,
                    device_model=step.device.device_model,
                    description=f"{step.name} on {step.device.display_name or step.device.asset_code}",
                    quantity=1,
                    unit_price=amount_for(step),
                    production_step=step,
                )
            order.recalc_total()
        return Response(WorkOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="requests/send-back")
    def send_back(self, request):
        """Hand a request back to the project: the operation is undecided
        again, and the reason is written on the asset for Execution to read."""
        from apps.assets.models import DeviceLifecycleEvent, ProductionStep

        step = ProductionStep.objects.filter(pk=request.data.get("step")).select_related("device").first()
        if step is None:
            return Response({"step": ["Pick the request to send back."]}, status=400)
        if step.live_work_orders().exists():
            return Response({"step": [f"'{step.name}' is already on a work order — cancel that instead."]}, status=400)
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"reason": ["Say why it is going back."]}, status=400)
        with transaction.atomic():
            step.location = ProductionStep.Location.UNDECIDED
            step.workshop = None
            step.workshop_name = ""
            step.work_order_requested_at = None
            step.save(update_fields=["location", "workshop", "workshop_name", "work_order_requested_at", "updated_at"])
            DeviceLifecycleEvent.objects.create(
                device=step.device,
                event_type=DeviceLifecycleEvent.EventType.NOTE,
                description=f"Work order request for '{step.name}' sent back by Work Orders: {reason}",
                performed_by=request.user,
                metadata={"step": str(step.pk), "sent_back": True, "reason": reason},
            )
        return Response({"detail": f"'{step.name}' on {step.device.asset_code} is back with the project to decide.", "step": str(step.pk)})

    @action(detail=True, methods=["post"], url_path="inspect")
    def inspect(self, request, pk=None):
        """Work receiving: the delivered work is inspected, job by job.

        Body: result ("accepted" | "rework"), notes, and optionally ``items``
        naming which of the delivered jobs this verdict covers — everything
        waiting, when it says nothing. Accepted finishes those jobs and their
        operations; rework sends them back to the vendor with the reason. The
        order completes only once every job on it has been accepted.
        """
        work_order = self.get_object()
        if work_order.status not in (WorkOrder.Status.DELIVERED, WorkOrder.Status.PARTIALLY_DELIVERED):
            return Response(
                {"detail": f"'{work_order.wo_number}' is {work_order.get_status_display().lower()} — only delivered work is inspected."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = request.data.get("result")
        if result not in (WorkOrder.InspectionResult.ACCEPTED, WorkOrder.InspectionResult.REWORK):
            return Response({"result": ["Choose 'accepted' or 'rework'."]}, status=400)
        notes = (request.data.get("notes") or "").strip()
        if result == WorkOrder.InspectionResult.REWORK and not notes:
            return Response({"notes": ["Say what has to be redone."]}, status=400)

        waiting = [i for i in work_order.items.all() if i.awaiting_inspection]
        wanted = {str(i) for i in (request.data.get("items") or [])}
        if wanted:
            lines = [i for i in waiting if str(i.pk) in wanted]
            if len(lines) != len(wanted):
                return Response({"items": ["Pick jobs on this order that are waiting to be inspected."]}, status=400)
        else:
            lines = waiting

        now = timezone.now()
        who = request.user.get_full_name() or request.user.username
        stamp = f"[{timezone.localtime(now):%Y-%m-%d %H:%M}] {who}: "
        body = notes or ("Accepted" if result == WorkOrder.InspectionResult.ACCEPTED else "")

        with transaction.atomic():
            for line in lines:
                line.inspected_by = request.user
                line.inspected_at = now
                line.inspection_result = result
                line.inspection_notes = (
                    (line.inspection_notes + "\n" if line.inspection_notes else "") + stamp + body
                ).strip()
                if result == WorkOrder.InspectionResult.REWORK:
                    # Back on the vendor's bench until he sends it in again.
                    line.delivered_at = None
                line.save(update_fields=[
                    "inspected_by", "inspected_at", "inspection_result", "inspection_notes",
                    "delivered_at", "updated_at",
                ])

            work_order.inspected_by = request.user
            work_order.inspected_at = now
            work_order.inspection_result = result
            work_order.inspection_notes = (
                (work_order.inspection_notes + "\n" if work_order.inspection_notes else "") + stamp + body
            ).strip()
            work_order.status = _status_from_lines(work_order)
            work_order.save(update_fields=[
                "inspected_by", "inspected_at", "inspection_result", "inspection_notes", "status", "updated_at",
            ])
        work_order.refresh_from_db()
        return Response(WorkOrderSerializer(work_order).data)

    @action(detail=False, methods=["get"], url_path="receiving")
    def receiving(self, request):
        """Delivered work waiting to be inspected."""
        qs = self.filter_queryset(self.get_queryset()).filter(
            status__in=(WorkOrder.Status.DELIVERED, WorkOrder.Status.PARTIALLY_DELIVERED)
        ).order_by("delivered_at", "updated_at")
        return Response({"results": WorkOrderSerializer(qs, many=True).data})

    @action(detail=True, methods=["get"], url_path="print")
    def print_pdf(self, request, pk=None):
        work_order = self.get_object()
        pdf_bytes = build_work_order_pdf(work_order)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{work_order.wo_number}.pdf"'
        return response
