from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from common.scoping import for_client
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from common.permissions import ADMIN_ROLES, AdminManagerWriteElseRead, CommercialWriteElseRead


def may_advance_installation(user, installation) -> bool:
    """Who may move an installation along.

    The person doing the work, and whoever they answer to. A supervisor
    could open the tracker and read every step but not touch one, because
    the rule said "platform admin" where the organogram says "the installer's
    own line" — so a job waiting on a correction waited for a Super Admin.

    Deliberately not "any manager": marking a step done is a claim about
    work that happened on site, so it stays with the people who were there
    or who are answerable for them. Another technician cannot touch a job
    that is not theirs.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    role = getattr(user, "role", None)
    if role in ADMIN_ROLES:
        return True
    if installation.installed_by_id == user.id:
        return True
    installer = installation.installed_by
    if installer is not None and user.manages(installer):
        return True
    return bool(
        role == "vendor"
        and getattr(user, "supplier_id", None)
        and installation.vendor_id == user.supplier_id
    )


class IsSuperAdminOrAssignedInstaller(BasePermission):
    """Step/delay actions: the assigned installer (mobile), their reporting
    line, the installation's vendor (portal login, XC-04) or Operations."""

    message = "This installation is not yours to advance."

    def has_object_permission(self, request, view, obj):
        installation = obj.installation if hasattr(obj, "installation") else obj
        user = request.user
        if may_advance_installation(user, installation):
            return True
        return False

from .models import (
    InstallationRouteTemplate,
    InstallationRouteTemplateStep,
    DeviceInstallation,
    HandoverRecord,
    InstallationDelay,
    InstallationPhoto,
    InstallationStep,
    Site,
    SiteContact,
    SiteZone,
)
from .serializers import (
    DeviceInstallationDetailSerializer,
    DeviceInstallationListSerializer,
    HandoverCreateSerializer,
    InstallationDelaySerializer,
    InstallationPhotoSerializer,
    InstallationStepSerializer,
    SiteContactSerializer,
    SiteDetailSerializer,
    SiteListSerializer,
    SiteZoneSerializer,
)


class SiteViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["client", "city", "state_province", "country", "is_active"]
    search_fields = ["name", "address", "city", "state_province"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        # A client portal login sees its own client's sites only.
        return for_client(
            Site.objects.select_related("client")
            .prefetch_related("contacts")
            .annotate(device_count=Count("devices")),
            self.request.user,
            "client_id",
        )

    def get_serializer_class(self):
        if self.action == "list":
            return SiteListSerializer
        return SiteDetailSerializer


class SiteContactViewSet(viewsets.ModelViewSet):
    queryset = SiteContact.objects.select_related("site").all()
    serializer_class = SiteContactSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["site", "is_primary"]
    search_fields = ["name", "email", "phone"]


class SiteZoneViewSet(viewsets.ModelViewSet):
    queryset = SiteZone.objects.select_related("site").all()
    serializer_class = SiteZoneSerializer
    permission_classes = [IsAuthenticated, CommercialWriteElseRead]
    filterset_fields = ["site"]
    search_fields = ["name"]


def _attach_to_asset_gallery(device, image, user, caption: str) -> None:
    """Put a photo taken on site into the asset's own gallery.

    A picture of the installed asset belongs on the asset, not only inside the
    installation record — the registry is where anyone looks for it. The first
    one an asset ever gets becomes its primary image.
    """
    from apps.assets.models import DeviceImage

    # The same file object is read twice (installation photo, then here), so
    # rewind it or the second save writes an empty file.
    if hasattr(image, "seek"):
        image.seek(0)
    DeviceImage.objects.create(
        device=device,
        image=image,
        caption=caption,
        is_primary=not device.images.exists(),
    )


class DeviceInstallationViewSet(viewsets.ModelViewSet):
    queryset = (
        DeviceInstallation.objects
        .select_related(
            "device", "device__device_model", "device__device_model__brand",
            "device__asset_type", "device__assigned_client", "device__project",
            "installed_by", "vendor", "site", "zone",
        )
        .prefetch_related("photos", "steps", "delays", "device__clients")
        .all()
    )
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["device", "site", "installed_by", "device__assigned_client", "device__project"]

    search_fields = [
        "device__asset_code", "device__display_name", "device__serial_number",
        "device__assigned_client__name", "device__clients__name",
        "installed_by__first_name", "installed_by__last_name", "installed_by__username",
        "site__name", "position_label",
    ]
    ordering_fields = ["installed_at", "due_date", "completed_at", "created_at"]

    def perform_destroy(self, instance):
        """A duplicate can go; a handed-over job cannot.

        The handover is the client's record of acceptance as much as ours,
        so the job it belongs to stays. Anything short of that — above all
        the second entry opened by mistake for an asset already on the
        tracker — can be removed, steps and photos with it.
        """
        from rest_framework.exceptions import ValidationError as _VE

        if getattr(instance, "handover", None) is not None:
            raise _VE(
                "This installation has been handed over to the client, so it "
                "stays on the record. Record its removal instead if the asset "
                "has come down."
            )
        instance.delete()

    @action(detail=True, methods=["get"], url_path="handover-document")
    def handover_document(self, request, pk=None):
        """The handover certificate as a PDF.

        Printed before the visit it carries blank lines for the client to sign;
        once a handover has been recorded it prints what was agreed instead.
        """
        from django.http import HttpResponse

        from .documents import render_handover_pdf

        installation = self.get_object()
        pdf = render_handover_pdf(installation)
        response = HttpResponse(pdf, content_type="application/pdf")
        name = f"handover-{installation.device.asset_code}"
        response["Content-Disposition"] = f'attachment; filename="{name}.pdf"'
        return response

    @action(detail=True, methods=["post"], url_path="reorder-steps")
    def reorder_steps(self, request, pk=None):
        """Put this installation's checklist in the order given.

        Body: ``{"steps": [id, id, ...]}`` — the running order. A step left out
        keeps its place at the end, so a list from a screen that has not
        refreshed cannot quietly drop one.
        """
        from .ordering import apply_step_order

        installation = self.get_object()
        refuse_if_closed_out(installation)
        ids = request.data.get("steps")
        if not isinstance(ids, list) or not ids:
            return Response(
                {"steps": ["Give the step ids in the order they should run."]},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            apply_step_order(installation.pk, ids)
        installation.refresh_from_db()
        return Response(self.get_serializer(installation).data)

    def get_queryset(self):
        qs = super().get_queryset()
        # Vendor scope (XC-04): portal users only see installations their
        # supplier is doing; a vendor login without a supplier sees nothing.
        user = self.request.user
        if getattr(user, "role", "") == "vendor" and not user.is_superuser:
            if not user.supplier_id:
                return qs.none()
            qs = qs.filter(vendor_id=user.supplier_id)
        # ?escalated=true|false — installations with a non-empty escalation ledger.
        escalated = self.request.query_params.get("escalated")
        if escalated is not None:
            value = escalated.strip().lower()
            if value in ("true", "1"):
                qs = qs.exclude(escalation_state={})
            elif value in ("false", "0"):
                qs = qs.filter(escalation_state={})
        # ?bucket=<progress bucket> — tracker drill-downs derived from the
        # completion stamp and the step checklist. Handled here so list AND
        # export share them; unknown values are ignored.
        bucket = self.request.query_params.get("bucket")
        if bucket == "completed":
            qs = qs.filter(completed_at__isnull=False)
        elif bucket == "overdue":
            qs = qs.filter(completed_at__isnull=True, due_date__lt=timezone.localdate())
        elif bucket == "on_hold":
            qs = qs.filter(steps__status=InstallationStep.StepStatus.ON_HOLD).distinct()
        elif bucket == "in_progress":
            qs = qs.filter(
                completed_at__isnull=True,
                steps__status__in=[
                    InstallationStep.StepStatus.IN_PROGRESS,
                    InstallationStep.StepStatus.COMPLETED,
                ],
            ).distinct()
        elif bucket == "not_started":
            qs = qs.filter(completed_at__isnull=True).exclude(
                steps__status__in=[
                    InstallationStep.StepStatus.IN_PROGRESS,
                    InstallationStep.StepStatus.ON_HOLD,
                    InstallationStep.StepStatus.COMPLETED,
                    InstallationStep.StepStatus.SKIPPED,
                ]
            )
        elif bucket == "delayed":
            qs = qs.filter(delays__cause=InstallationDelay.Cause.CLIENT).distinct()
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return DeviceInstallationListSerializer
        return DeviceInstallationDetailSerializer

    def get_permissions(self):
        # The handover action carries its own gate (assigned installer or
        # HANDOVER_ROLES) — the viewset's manager-write permission would
        # otherwise reject the installer/supervisor before it ever runs.
        if self.action in ("handover", "activate"):
            return [IsAuthenticated()]
        return super().get_permissions()

    HANDOVER_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of the installation tracker — filter-aware (XC-01)."""
        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Asset Code", "Asset Name", "Site", "Clients", "Installer",
            "Vendor", "Due Date", "Completed At", "Progress %", "Escalated",
        ]
        rows = []
        for inst in qs:
            client_names = []
            if inst.device.assigned_client:
                client_names.append(inst.device.assigned_client.name)
            for client in inst.device.clients.all():
                if client.name not in client_names:
                    client_names.append(client.name)
            steps = list(inst.steps.all())
            completed = sum(1 for s in steps if s.status == InstallationStep.StepStatus.COMPLETED)
            progress = round((completed / len(steps)) * 100) if steps else 0
            rows.append([
                inst.device.asset_code,
                inst.device.display_name,
                inst.site.name if inst.site_id else "",
                ", ".join(client_names),
                (inst.installed_by.get_full_name() or inst.installed_by.username) if inst.installed_by_id else "",
                inst.vendor.name if inst.vendor_id else "",
                inst.due_date,
                inst.completed_at,
                progress,
                bool(inst.escalation_state),
            ])
        log_export(request.user, "installation", len(rows), export_params(request))
        return xlsx_response("installations", "Installations", columns, rows)

    @action(detail=True, methods=["post"], url_path="save-step-template")
    def save_step_template(self, request, pk=None):
        """Keep this job's checklist as the standard for the asset type."""
        installation = self.get_object()
        asset_type = installation.device.asset_type
        if asset_type is None:
            return Response(
                {"detail": "Give the asset a type first — checklists are held per asset type."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        steps = list(installation.steps.all())
        if not steps:
            return Response(
                {"detail": "There are no steps on this installation to save."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            template, _ = InstallationRouteTemplate.objects.get_or_create(
                asset_type=asset_type, defaults={"created_by": request.user},
            )
            # Replace wholesale: the job in hand is the current definition.
            template.steps.all().delete()
            for index, step in enumerate(steps):
                InstallationRouteTemplateStep.objects.create(
                    template=template,
                    step_number=index + 1,
                    step_type=step.step_type,
                    custom_label=step.custom_label,
                    assigned_team=step.assigned_team,
                    description=step.description,
                )
        return Response({
            "asset_type": asset_type.name,
            "saved_steps": len(steps),
            "detail": f"Saved as the standard installation checklist for {asset_type.name}.",
        })

    @action(detail=True, methods=["post"], url_path="apply-step-template")
    def apply_step_template(self, request, pk=None):
        """Lay this job out from the standard checklist for its asset type."""
        installation = self.get_object()
        asset_type = installation.device.asset_type
        if asset_type is None:
            return Response(
                {"detail": "Give the asset a type first — checklists are held per asset type."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        template = InstallationRouteTemplate.objects.filter(asset_type=asset_type).first()
        if template is None or not template.steps.exists():
            return Response(
                {"detail": (
                    f"No standard checklist saved for {asset_type.name} yet — lay the steps "
                    f"out here and save them as the standard."
                )},
                status=drf_status.HTTP_404_NOT_FOUND,
            )
        if installation.steps.exclude(status=InstallationStep.StepStatus.NOT_STARTED).exists():
            return Response(
                {"detail": "Work has already started on this checklist; it cannot be replaced."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            installation.steps.all().delete()
            lines = list(template.steps.all())
            InstallationStep.objects.bulk_create([
                InstallationStep(
                    installation=installation,
                    step_type=line.step_type,
                    custom_label=line.custom_label,
                    assigned_team=line.assigned_team,
                    description=line.description,
                    step_number=index + 1,
                )
                for index, line in enumerate(lines)
            ])
        installation.refresh_from_db()
        installation._prefetched_objects_cache = {}
        return Response({
            "applied": len(lines),
            "installation": DeviceInstallationDetailSerializer(
                installation, context=self.get_serializer_context()
            ).data,
        })

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def activate(self, request, pk=None):
        """Technician marks the installed asset live, with a photo of it.

        The registry status is not something anyone types in: the person who
        physically installed the asset says it is running, from the tracker,
        and the photo they upload is the evidence. It lands in the asset's own
        gallery as well as the installation record.
        """
        installation = self.get_object()
        user = request.user
        if (
            getattr(user, "role", None) not in self.HANDOVER_ROLES
            and installation.installed_by_id != user.id
            and installation.device.assigned_technician_id != user.id
        ):
            return Response(
                {"detail": "Only the assigned installer or operations management can activate this asset."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        device = installation.device
        if device.status == "active":
            return Response({"detail": "This asset is already active."}, status=drf_status.HTTP_400_BAD_REQUEST)
        # Live means the checklist is finished, not merely that the asset
        # reads Installed - an asset moved to Installed by hand could be
        # activated with half its steps untouched.
        unfinished = [
            step.custom_label or step.get_step_type_display()
            for step in installation.steps.exclude(step_type=InstallationStep.StepType.HANDOVER)
            if step.status not in (InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED)
        ]
        if unfinished:
            return Response(
                {"detail": f"Finish the installation steps first: {', '.join(unfinished)}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        # The checklist is done, so the asset is installed whatever word the
        # registry holds - an asset still reading "In Production" with every
        # step complete was stuck: Active waited on Installed, and nothing
        # was left to make it Installed. Anything past Installed that is not
        # Active (out of service, written off, the client's now) is a
        # different story and stays one.
        from .signals import PRE_INSTALL_STATUSES

        if device.status not in PRE_INSTALL_STATUSES and device.status != "installed":
            return Response(
                {"detail": (
                    f"The asset is '{device.get_status_display()}', which is not a state it "
                    "goes live from. Sort that out in the asset registry first."
                )},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        photos = request.FILES.getlist("photos")
        if not photos:
            return Response(
                {"photos": "Upload a photo of the installed asset — it is the record that it is running."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            for image in photos:
                InstallationPhoto.objects.create(
                    installation=installation,
                    photo_type=InstallationPhoto.PhotoType.POST_INSTALL,
                    image=image,
                    caption=request.data.get("notes", "") or "",
                    taken_by=user,
                )
                _attach_to_asset_gallery(device, image, user, "Installed — Active")

            if device.status != "installed":
                # Step through Installed so the registry's history reads the
                # way the asset's life actually went.
                device._transition_user = user
                device._transition_reason = "Installation checklist complete"
                device.status = "installed"
                device.save(update_fields=["status", "updated_at"])
            device._transition_user = user
            device._transition_reason = (
                request.data.get("notes") or "Marked active from the installation tracker"
            )
            device.status = "active"
            device.save(update_fields=["status", "updated_at"])

            # What we promise the client is a commercial commitment, not
            # something the technician on site settles. Client warranties are
            # raised by the supervisor under Warranties.

        installation.refresh_from_db()
        installation._prefetched_objects_cache = {}
        return Response(
            DeviceInstallationDetailSerializer(installation, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def handover(self, request, pk=None):
        """Formal handover (WF-12): record acceptance, assign client + site to
        the asset, complete the handover step and move the asset to Active."""
        installation = self.get_object()
        user = request.user
        if (
            getattr(user, "role", None) not in self.HANDOVER_ROLES
            and installation.installed_by_id != user.id
        ):
            return Response(
                {"detail": "Only the assigned installer or operations management can hand over."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if getattr(installation, "handover", None):
            return Response(
                {"detail": "This installation has already been handed over."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        pending = [
            step.custom_label or step.get_step_type_display()
            for step in installation.steps.exclude(step_type=InstallationStep.StepType.HANDOVER)
            if step.status not in (InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED)
        ]
        if pending:
            return Response(
                {"detail": f"Complete the remaining steps before handover: {', '.join(pending)}."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        ser = HandoverCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        device = installation.device
        # Who the asset belongs to was settled when the work was set up, and
        # handing it to anybody else would contradict the project it was sold
        # under. Where that answer exists it stands; a caller may only name a
        # client when nothing else has.
        settled = device.client_for
        asked = ser.validated_data.get("client")
        if settled is not None and asked is not None and asked != settled:
            return Response(
                {"client": (
                    f"This asset belongs to {settled.name}, which was set when the work was "
                    f"raised. Change it there rather than at handover."
                )},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        client = settled or asked
        if client is None:
            return Response(
                {"client": "The asset has no client yet — pick the client receiving it."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # Row-lock and re-check under the lock: two concurrent submits
            # must yield one record and one clean 400, not an IntegrityError.
            installation = (
                DeviceInstallation.objects.select_for_update()
                .select_related("device", "site")
                .get(pk=installation.pk)
            )
            if getattr(installation, "handover", None):
                return Response(
                    {"detail": "This installation has already been handed over."},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            device = installation.device
            record = HandoverRecord.objects.create(
                installation=installation,
                device=device,
                client=client,
                site=installation.site,
                handover_date=ser.validated_data.get("handover_date") or timezone.localdate(),
                accepted_by_name=ser.validated_data["accepted_by_name"],
                acceptance_notes=ser.validated_data.get("acceptance_notes", ""),
                signed_document=ser.validated_data.get("signed_document"),
                performed_by=user,
            )
            device.assigned_client = client
            device.current_site = installation.site
            device.installation_date = record.handover_date
            device.save(update_fields=["assigned_client", "current_site", "installation_date", "updated_at"])

            for image in request.FILES.getlist("photos"):
                InstallationPhoto.objects.create(
                    installation=installation,
                    photo_type=InstallationPhoto.PhotoType.HANDOVER,
                    image=image,
                    taken_by=user,
                )
                _attach_to_asset_gallery(device, image, user, "Handover")

            # Completing the handover step stamps completed_at, re-anchors the
            # client warranty to the record's date and journals the flip.
            step = installation.steps.filter(step_type=InstallationStep.StepType.HANDOVER).first()
            if step and step.status not in (
                InstallationStep.StepStatus.COMPLETED, InstallationStep.StepStatus.SKIPPED
            ):
                step.status = InstallationStep.StepStatus.COMPLETED
                step.save()

            # The step-save signal only anchors while completed_at is unset —
            # when the checklist was already closed out (mobile flow) the
            # formal record's date must still win, so re-anchor explicitly.
            from .signals import _anchor_client_warranties

            installation.refresh_from_db()
            _anchor_client_warranties(installation)

            device.refresh_from_db()
            if device.status != "active":
                device._transition_user = user
                device._transition_reason = f"Handover accepted by {record.accepted_by_name}"
                device.status = "active"
                device.save(update_fields=["status", "updated_at"])

        installation.refresh_from_db()
        installation._prefetched_objects_cache = {}
        return Response(
            DeviceInstallationDetailSerializer(installation, context=self.get_serializer_context()).data,
            status=drf_status.HTTP_201_CREATED,
        )


def refuse_if_closed_out(installation):
    """An installation whose asset is live or handed over is history.

    Its checklist is the record of how the asset came to be in service, and a
    record is read, not edited: no step is added, moved, reset or re-done once
    the asset has gone live or the client has signed for it.
    """
    from rest_framework.exceptions import ValidationError

    device = installation.device
    live = device.status in ("active", "under_maintenance", "client_property", "decommissioned")
    if live or getattr(installation, "handover", None) is not None:
        why = "handed over" if getattr(installation, "handover", None) is not None else "live"
        raise ValidationError({
            "detail": f"{device.asset_code} is {why} — its installation steps are a record now and cannot be changed."
        })


class InstallationStepViewSet(viewsets.ModelViewSet):
    queryset = InstallationStep.objects.select_related("installation", "installation__device").all()
    serializer_class = InstallationStepSerializer
    filterset_fields = ["installation", "step_type", "status"]
    ordering_fields = ["step_number"]

    def perform_create(self, serializer):
        refuse_if_closed_out(serializer.validated_data["installation"])
        serializer.save()

    def perform_update(self, serializer):
        from django.utils import timezone
        from rest_framework.exceptions import ValidationError as _VE

        step = serializer.instance
        refuse_if_closed_out(step.installation)

        new_status = serializer.validated_data.get("status", step.status)
        moving_on = new_status in (
            InstallationStep.StepStatus.IN_PROGRESS,
            InstallationStep.StepStatus.COMPLETED,
        )
        # A checklist is a sequence. Step 7 could be marked done with steps 2
        # and 3 untouched, and then the asset went live over the gap - so
        # "Completed" stopped meaning the work before it had been done.
        if moving_on and new_status != step.status:
            behind = [
                s.custom_label or s.get_step_type_display()
                for s in step.installation.steps.filter(step_number__lt=step.step_number)
                if s.status not in (
                    InstallationStep.StepStatus.COMPLETED,
                    InstallationStep.StepStatus.SKIPPED,
                )
            ]
            if behind:
                raise _VE({"status": (
                    f"Finish the steps before this one first: {', '.join(behind)}. "
                    "A step can be skipped if it does not apply."
                )})

        extra = {}
        if new_status == InstallationStep.StepStatus.COMPLETED and step.status != new_status:
            extra["completed_by"] = self.request.user
            if "completed_at" not in serializer.validated_data and not step.completed_at:
                extra["completed_at"] = timezone.now()
        serializer.save(**extra)

    def perform_destroy(self, instance):
        """Remove the step, then close the gap it leaves in the numbering."""
        from .ordering import renumber_steps

        refuse_if_closed_out(instance.installation)
        installation_id = instance.installation_id
        with transaction.atomic():
            instance.delete()
            renumber_steps(installation_id)

    def get_permissions(self):
        # Only the assigned installer (mobile) or a super admin (desktop) may
        # advance a step; only managers add/remove steps.
        if self.action in ("update", "partial_update"):
            return [IsAuthenticated(), IsSuperAdminOrAssignedInstaller()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]


class InstallationDelayViewSet(viewsets.ModelViewSet):
    queryset = InstallationDelay.objects.select_related(
        "installation", "step", "reported_by"
    ).all()
    serializer_class = InstallationDelaySerializer
    filterset_fields = ["installation", "step", "cause"]
    ordering_fields = ["created_at"]

    def get_permissions(self):
        # Delays are logged by the assigned installer or a super admin;
        # managers can edit/resolve/remove them.
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]

    def perform_create(self, serializer):
        installation = serializer.validated_data["installation"]
        user = self.request.user
        if not may_advance_installation(user, installation):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("This installation is not yours to flag a delay on.")
        serializer.save(reported_by=user)


class InstallationPhotoViewSet(viewsets.ModelViewSet):
    queryset = InstallationPhoto.objects.select_related("installation").all()
    serializer_class = InstallationPhotoSerializer
    filterset_fields = ["installation", "photo_type"]

    def get_permissions(self):
        # Field techs may attach installation photos; managers can also edit/remove.
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAuthenticated(), AdminManagerWriteElseRead()]

    def perform_create(self, serializer):
        user = self.request.user
        if getattr(user, "role", "") == "vendor":
            # Vendor-portal users may only photograph their own installations.
            installation = serializer.validated_data["installation"]
            if not user.supplier_id or installation.vendor_id != user.supplier_id:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied("Vendors can only add photos to their own installations.")
        serializer.save(taken_by=user)
