from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.db.models import Count, Q, Sum
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import AdminManagerWriteElseRead, WarehouseWriteElseRead

from .costing import project_devices
from .models import (
    ProjectBudget,
    ProjectCostLine,
    BOMAllocation,
    Project,
    ProjectBOMLine,
    ProjectBottleneck,
    ProjectMember,
    ProjectMilestone,
    ProjectScopeItem,
)
from .serializers import (
    ProjectCostLineSerializer,
    ProjectBOMLineSerializer,
    ProjectBottleneckSerializer,
    ProjectDetailSerializer,
    ProjectListSerializer,
    ProjectMemberSerializer,
    ProjectMilestoneSerializer,
    ProjectScopeItemSerializer,
)


def _installations_for(devices):
    """What the Installation Tracker knows about these assets, by device id.

    Execution should not have to send people hunting: the job on the tracker,
    who it went to, the stage it has reached and when it is due all read here.
    """
    from apps.sites.models import DeviceInstallation, InstallationStep

    jobs = (
        DeviceInstallation.objects
        .filter(device__in=devices)
        .select_related("site", "installed_by", "vendor")
        .prefetch_related("steps")
        .order_by("device_id", "-installed_at")
    )
    out = {}
    for job in jobs:
        # The live job wins; a finished one stands until a new one opens.
        if job.device_id in out and out[job.device_id]["completed_at"] is None:
            continue
        steps = sorted(job.steps.all(), key=lambda s: s.step_number)
        done = [s for s in steps if s.status == InstallationStep.StepStatus.COMPLETED]
        current = next(
            (s for s in steps if s.status not in (
                InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED,
            )),
            None,
        )
        crew = job.installed_by
        out[job.device_id] = {
            "id": str(job.pk),
            "site_name": job.site.name if job.site_id else None,
            "installed_by_name": (crew.get_full_name() or crew.username) if crew else None,
            "vendor_name": job.vendor.name if job.vendor_id else (job.external_vendor_name or None),
            "due_date": job.due_date,
            "installed_at": job.installed_at,
            "completed_at": job.completed_at,
            "steps_done": len(done),
            "steps_total": len(steps),
            "progress": round(len(done) / len(steps) * 100) if steps else 0,
            "stage": (
                "Complete" if job.completed_at is not None
                else (current.custom_label or current.get_step_type_display()) if current is not None
                else "Not started"
            ),
            "stage_status": current.get_status_display() if current is not None else None,
        }
    return out


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["status", "phase", "contract_type", "client", "site", "manager"]
    search_fields = ["name", "location", "description", "client__name", "site__name", "sites__name"]
    ordering_fields = ["created_at", "start_date", "target_date", "progress"]

    @staticmethod
    def _activity(project):
        """What has already happened on a project, in the words a user reads.

        Stock issued, purchase orders raised or work orders placed are records
        other people rely on; a project carrying any of them is not deleted.
        """
        from apps.procurement.models import PurchaseOrderItem
        from apps.workorders.models import WorkOrder

        found = []
        issued = (
            project.inventory_issuances.exists()
            or project.issuance_requests.filter(quantity_issued__gt=0).exists()
        )
        if issued:
            found.append("stock issued to it")
        # Lines raised for its requirements, for its vendor-built assets, or
        # for the components of assets on its scope.
        orders = (
            PurchaseOrderItem.objects.filter(
                Q(bom_line__project=project)
                | Q(procured_devices__project_scope_items__project=project)
                | Q(asset_components__device__project_scope_items__project=project)
            )
            .exclude(purchase_order__status="cancelled")
            .distinct()
            .count()
        )
        if orders:
            found.append(f"{orders} purchase order line(s) raised for it")
        wos = project.work_orders.exclude(status=WorkOrder.Status.CANCELLED).count()
        if wos:
            found.append(f"{wos} work order(s)")
        return found

    def destroy(self, request, *args, **kwargs):
        """A project with no activity can go; one with activity is kept."""
        project = self.get_object()
        activity = self._activity(project)
        if activity:
            return Response(
                {
                    "detail": (
                        f"'{project.name}' has {', '.join(activity)}. Projects with activity are kept "
                        "for the record — move it to On Hold or Order Lost instead."
                    )
                },
                status=400,
            )
        return super().destroy(request, *args, **kwargs)

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

    def _execution_blocked(self, project):
        """Execution decisions follow budget approval: a plan that exists but is
        not approved blocks them; a project with no plan at all is left alone."""
        plan = ProjectBudget.objects.filter(project=project).first()
        if plan is not None and plan.status != ProjectBudget.Status.APPROVED:
            return Response(
                {"detail": f"The budget for {project.name} is {plan.get_status_display().lower()} — "
                           "execution starts once it is approved."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        return None

    @action(detail=True, methods=["get"])
    def requirements(self, request, pk=None):
        """Every asset in this project and what it is built from.

        A project can hold many assets; each asset's components are its bill of
        materials, gathered here automatically. Per line we report what is
        required, what the warehouse actually holds, and how the requirement is
        being covered — which is the decision the user makes on this screen.
        Availability is informational: procuring is allowed even when stock
        would cover it.
        """
        from apps.assets.models import AssetComponent, Device
        from apps.assets.serializers import AssetComponentSerializer, ProductionStepSerializer

        project = self.get_object()
        # An asset reaches a project two ways: its own `project` field, or a
        # Scope row. Both count, or assets added through the Scope screen would
        # silently go missing from the build plan.
        devices = (
            project_devices(project)
            .select_related("procurement_item__purchase_order", "asset_type")
            .prefetch_related(
                "components__inventory_item__material_type",
                "components__inventory_unit_type",
                "components__purchase_order_item__purchase_order",
                "production_steps__work_orders", "production_steps__workshop",
            )
        )

        assets = []
        totals = {"required": 0, "issued": 0, "outstanding": 0,
                  "awaiting_decision": 0, "to_procure": 0}
        installs = _installations_for(devices)
        from apps.assets.serializers import assignee_label

        for device in devices:
            if device.source != Device.Source.INHOUSE:
                # Bought complete: the one decision is to send it to Procurement.
                on_order = device.procurement_item_id is not None
                arrived = device.status != Device.Status.PROCURED
                totals["required"] += 1
                if not arrived:
                    totals["outstanding"] += 1
                    if on_order or device.procurement_requested_at:
                        totals["to_procure"] += 1
                    else:
                        totals["awaiting_decision"] += 1
                assets.append({
                    "id": str(device.pk),
                    "asset_code": device.asset_code,
                    "display_name": device.display_name or (device.asset_type.name if device.asset_type_id else ""),
                    "status": device.status,
                    "status_display": device.get_status_display(),
                    "vendor_asset": True,
                    "source": device.source,
                    "source_display": device.get_source_display(),
                    "purchase_price": device.purchase_price,
                    "supply_vendor_name": device.supply_vendor_name,
                    "procurement_requested_at": device.procurement_requested_at,
                    "po_number": device.procurement_item.purchase_order.po_number if on_order else None,
                    "po_status": device.procurement_item.purchase_order.status if on_order else None,
                    "components": [],
                    "steps": [],
                    "route_complete": False,
                    "installation": installs.get(device.pk),
                    "assigned_to_display": assignee_label(
                        device.assigned_technician, device.assigned_vendor_name,
                        device.assigned_vendor_contact,
                    ),
                })
                continue
            components = list(device.components.all())
            rows = AssetComponentSerializer(components, many=True).data
            for component, row in zip(components, rows):
                totals["required"] += component.quantity
                totals["issued"] += component.issued_quantity
                totals["outstanding"] += component.outstanding_quantity
                if component.fulfilment == AssetComponent.Fulfilment.PENDING:
                    totals["awaiting_decision"] += 1
                elif component.fulfilment == AssetComponent.Fulfilment.PROCUREMENT:
                    totals["to_procure"] += 1
                # Can this line be covered from stock right now?
                row["can_use_stock"] = (
                    component.outstanding_quantity > 0
                    and component.available_quantity >= component.outstanding_quantity
                )
            steps = sorted(device.production_steps.all(), key=lambda x: x.step_number)
            assets.append({
                "id": str(device.pk),
                "asset_code": device.asset_code,
                "display_name": device.display_name or str(device.device_model),
                "status": device.status,
                "status_display": device.get_status_display(),
                "vendor_asset": False,
                "source": device.source,
                "source_display": device.get_source_display(),
                "components": rows,
                # The route's own decisions: each operation in-house or on a work order.
                "steps": ProductionStepSerializer(steps, many=True).data,
                "route_complete": bool(steps) and all(x.status in ("completed", "skipped") for x in steps),
                "installation": installs.get(device.pk),
                "assigned_to_display": assignee_label(
                    device.assigned_technician, device.assigned_vendor_name,
                    device.assigned_vendor_contact,
                ),
            })

        return Response({"project": str(project.pk), "assets": assets, "totals": totals})

    @action(detail=True, methods=["post"], url_path="procure-asset")
    def procure_asset(self, request, pk=None):
        """Execution decision for a vendor-supplied asset: buy it complete.

        Sends the asset to Procurement's to-buy list, where the purchase order
        is raised and follows the usual approval and receipt. `undo` pulls it
        back while no order has been raised.
        """
        from apps.assets.models import Device

        project = self.get_object()
        blocked = self._execution_blocked(project)
        if blocked is not None:
            return blocked
        device = project_devices(project).filter(pk=request.data.get("device")).first()
        if device is None:
            return Response({"device": ["Choose an asset on this project."]}, status=400)
        if device.source == Device.Source.INHOUSE:
            return Response({"device": ["An in-house build is not bought complete — decide its components instead."]}, status=400)
        if device.procurement_item_id:
            return Response({"device": [f"{device.asset_code} is already on {device.procurement_item.purchase_order.po_number}."]}, status=400)
        if device.status != Device.Status.PROCURED:
            return Response({"device": [f"{device.asset_code} has already been received."]}, status=400)
        if request.data.get("undo"):
            device.procurement_requested_at = None
            detail = f"{device.asset_code} taken back from Procurement."
        else:
            device.procurement_requested_at = timezone.now()
            detail = f"{device.asset_code} sent to Procurement — raise the purchase order from To Procure."
        device.save(update_fields=["procurement_requested_at", "updated_at"])
        return Response({"detail": detail, "procurement_requested_at": device.procurement_requested_at})

    # Who signs a budget off. Kept apart from who writes it: an estimate should
    # not be approved by the person who produced it (super admins excepted).
    BUDGET_APPROVER_ROLES = ("super_admin", "group_head", "finance")

    @action(detail=True, methods=["get", "patch"], url_path="plan")
    def plan(self, request, pk=None):
        """The project's cost plan; PATCH sets the contingency percentage."""
        from .costing import build_plan, get_or_create_plan

        project = self.get_object()
        if request.method == "PATCH":
            plan = get_or_create_plan(project)
            if not plan.is_editable:
                return Response(
                    {"detail": f"The budget is {plan.get_status_display().lower()} — revise it to change it."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            try:
                pct = Decimal(str(request.data.get("contingency_percent", plan.contingency_percent)))
            except (InvalidOperation, TypeError):
                return Response({"contingency_percent": ["Must be a number."]}, status=400)
            if pct < 0 or pct > 100:
                return Response({"contingency_percent": ["Must be between 0 and 100."]}, status=400)
            plan.contingency_percent = pct
            plan.save(update_fields=["contingency_percent", "updated_at"])
        return Response(build_plan(project))

    @action(detail=True, methods=["get"], url_path="actuals/document")
    def actuals_document(self, request, pk=None):
        """The execution actuals as a PDF — the complete table, for the file."""
        from django.http import HttpResponse

        from .costing import build_actuals
        from .documents import render_actuals_pdf

        project = self.get_object()
        pdf = render_actuals_pdf(project, build_actuals(project))
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="actual-cost-{project.name}.pdf"'
        return response

    @action(detail=True, methods=["get"], url_path="plan/document")
    def plan_document(self, request, pk=None):
        """The cost plan as a PDF, for approval or the file."""
        from django.http import HttpResponse

        from .costing import build_plan
        from .documents import render_cost_plan_pdf

        project = self.get_object()
        pdf = render_cost_plan_pdf(project, build_plan(project))
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="cost-plan-{project.name}.pdf"'
        return response

    @action(detail=True, methods=["get"], url_path="boq")
    def boq(self, request, pk=None):
        """Bill of quantities: every component the whole project needs."""
        from .costing import build_boq

        return Response(build_boq(self.get_object()))

    @action(detail=True, methods=["get"], url_path="boq/document")
    def boq_document(self, request, pk=None):
        from django.http import HttpResponse

        from .costing import build_boq
        from .documents import render_boq_pdf

        project = self.get_object()
        pdf = render_boq_pdf(project, build_boq(project))
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="boq-{project.name}.pdf"'
        return response

    @action(detail=True, methods=["post"], url_path="raise-work-order")
    def raise_work_order(self, request, pk=None):
        """A work order for a vendor-built asset, the way a PO covers a part.

        Body: device, supplier, optional amount, expected_delivery, notes.
        The order type follows the asset's route: supplied-only, or supplied
        and installed. It lands in Work Orders as a draft.
        """
        from decimal import Decimal, InvalidOperation

        from apps.assets.models import Device, ProductionStep
        from apps.suppliers.models import Supplier
        from apps.workorders.models import WorkOrder, WorkOrderItem

        project = self.get_object()
        blocked = self._execution_blocked(project)
        if blocked is not None:
            return blocked
        device = Device.objects.filter(pk=request.data.get("device")).first()
        if device is None:
            return Response({"device": ["Choose the asset the vendor is building."]}, status=400)
        supplier = Supplier.objects.filter(pk=request.data.get("supplier")).first()
        if supplier is None:
            return Response({"supplier": ["Choose the vendor."]}, status=400)
        # One operation of an in-house route, given to an outside workshop.
        step = None
        if request.data.get("production_step"):
            step = ProductionStep.objects.filter(pk=request.data.get("production_step"), device=device).first()
            if step is None:
                return Response({"production_step": ["That operation is not on this asset's route."]}, status=400)
            if step.live_work_orders().exists():
                return Response({"production_step": [f"'{step.name}' already has a work order."]}, status=400)
            if step.status in ("completed", "skipped"):
                return Response({"production_step": [f"'{step.name}' is already finished."]}, status=400)
        else:
            if device.source == Device.Source.INHOUSE:
                return Response({"device": ["An in-house build is not given to a vendor whole — raise a work order per operation."]}, status=400)
            if device.work_orders.exclude(status="cancelled").exists():
                return Response({"device": [f"{device.asset_code} already has a work order."]}, status=400)
        fallback = (step.planned_cost if step is not None else device.purchase_price) or 0
        try:
            amount = Decimal(str(request.data.get("amount") or fallback))
        except (InvalidOperation, ValueError):
            amount = Decimal("0")

        if step is not None:
            order_type = WorkOrder.OrderType.SERVICES
        else:
            order_type = (
                WorkOrder.OrderType.SUPPLY_INSTALL
                if device.source == Device.Source.VENDOR_TURNKEY else WorkOrder.OrderType.SUPPLY
            )
        title = (
            f"{step.name} — {device.asset_code} — {project.name}" if step is not None
            else f"{device.display_name or device.asset_code} — {project.name}"
        )
        with transaction.atomic():
            order = WorkOrder.objects.create(
                title=title,
                description=(request.data.get("notes") or "").strip(),
                order_type=order_type,
                production_step=step,
                supplier=supplier,
                client=project.client,
                site=device.current_site or project.site,
                project=project,
                device=device,
                expected_delivery=request.data.get("expected_delivery") or None,
                created_by=request.user,
            )
            WorkOrderItem.objects.create(
                work_order=order,
                asset_type=device.asset_type,
                device_model=device.device_model,
                description=(
                    f"{step.name} on {device.display_name or device.asset_code}" if step is not None
                    else f"{device.display_name or device.asset_code} ({device.get_source_display()})"
                ),
                production_step=step,
                quantity=1,
                unit_price=amount,
            )
            order.recalc_total()
            if step is not None:
                # The route now says this operation happens at that workshop.
                step.location = ProductionStep.Location.EXTERNAL
                step.workshop = supplier
                step.workshop_name = ""
                step.save(update_fields=["location", "workshop", "workshop_name", "updated_at"])
        from apps.workorders.serializers import WorkOrderSerializer

        return Response(WorkOrderSerializer(order).data, status=201)

    @action(detail=True, methods=["get"], url_path="actuals")
    def actuals(self, request, pk=None):
        """What the project is actually costing, against what was approved."""
        from .costing import build_actuals

        return Response(build_actuals(self.get_object()))

    @action(detail=True, methods=["post"], url_path="submit-budget")
    def submit_budget(self, request, pk=None):
        """Send the estimate up for approval."""
        from .costing import build_plan, get_or_create_plan

        project = self.get_object()
        plan = get_or_create_plan(project)
        if not plan.is_editable:
            return Response(
                {"detail": f"The budget is already {plan.get_status_display().lower()}."}, status=400
            )
        summary = build_plan(project)
        if summary["total"] <= 0:
            return Response(
                {"detail": "There is nothing to approve yet — the estimate comes to zero."}, status=400
            )
        plan.status = ProjectBudget.Status.SUBMITTED
        plan.submitted_by = request.user
        plan.submitted_at = timezone.now()
        plan.decision_notes = ""
        plan.save(update_fields=["status", "submitted_by", "submitted_at", "decision_notes", "updated_at"])
        return Response(build_plan(project))

    @action(detail=True, methods=["post"], url_path="approve-budget")
    def approve_budget(self, request, pk=None):
        return self._decide_budget(request, approve=True)

    @action(detail=True, methods=["post"], url_path="reject-budget")
    def reject_budget(self, request, pk=None):
        return self._decide_budget(request, approve=False)

    def _decide_budget(self, request, *, approve):
        from .costing import build_plan, get_or_create_plan

        project = self.get_object()
        plan = get_or_create_plan(project)
        role = getattr(request.user, "role", "")
        if role not in self.BUDGET_APPROVER_ROLES:
            return Response(
                {"detail": "Budgets are approved by the group head, finance or a super admin."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if plan.status != ProjectBudget.Status.SUBMITTED:
            return Response({"detail": "Only a budget awaiting approval can be decided."}, status=400)
        if plan.submitted_by_id == request.user.id and role != "super_admin":
            return Response(
                {"detail": "You submitted this budget — someone else has to approve it."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        notes = (request.data.get("notes") or "").strip()
        if not approve and not notes:
            return Response({"notes": ["Say why the budget is being sent back."]}, status=400)

        summary = build_plan(project)
        plan.status = ProjectBudget.Status.APPROVED if approve else ProjectBudget.Status.REJECTED
        plan.decided_by = request.user
        plan.decided_at = timezone.now()
        plan.decision_notes = notes
        update = ["status", "decided_by", "decided_at", "decision_notes", "updated_at"]
        if approve:
            # Freeze the figure that was signed off; the project carries it.
            plan.approved_total = summary["total"]
            update.append("approved_total")
            project.budget = summary["total"]
            moved = ["budget", "updated_at"]
            # Planning is over once the figure is agreed: the order moves on to
            # getting the parts in, and stops reading as still being planned.
            if project.phase == Project.Phase.PLANNING:
                project.phase = Project.Phase.PROCUREMENT
                moved.append("phase")
            if project.status == Project.Status.PLANNING:
                project.status = Project.Status.ON_TRACK
                moved.append("status")
            project.save(update_fields=moved)
        plan.save(update_fields=update)
        return Response(build_plan(project))

    @action(detail=True, methods=["post"], url_path="revise-budget")
    def revise_budget(self, request, pk=None):
        """Reopen an approved or pending budget for changes.

        Execution locks again until the revised figure is approved.
        """
        from .costing import build_plan, get_or_create_plan

        project = self.get_object()
        plan = get_or_create_plan(project)
        if plan.is_editable:
            return Response({"detail": "The budget is already open for changes."}, status=400)
        plan.status = ProjectBudget.Status.DRAFT
        plan.save(update_fields=["status", "updated_at"])
        return Response(build_plan(project))

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


class ProjectCostLineViewSet(viewsets.ModelViewSet):
    """Overheads on a project cost plan (travel, labour, transport, ...)."""

    queryset = ProjectCostLine.objects.select_related("project").all()
    serializer_class = ProjectCostLineSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["project"]

    def perform_create(self, serializer):
        from .costing import get_or_create_plan

        line = serializer.save()
        get_or_create_plan(line.project)

    def perform_destroy(self, instance):
        plan = getattr(instance.project, "cost_plan", None)
        planned = (instance.quantity or 0) * (instance.unit_cost or 0)
        if plan is not None and not plan.is_editable and planned:
            from rest_framework.exceptions import ValidationError as _VE

            raise _VE(f"The budget is {plan.get_status_display().lower()} — revise it before changing costs.")
        instance.delete()
