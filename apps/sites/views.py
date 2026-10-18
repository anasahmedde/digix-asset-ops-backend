from apps.notifications.service import inform_line, who as _who
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from common.scoping import for_client
from rest_framework import status as drf_status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from apps.notifications import service as notices
from common.permissions import CapabilityGate, can, can_for_project

from common import clock

from . import activity


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
    if can(user, "edit_installation") and can(user, "act_across_teams"):
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
    permission_classes = [IsAuthenticated, CapabilityGate]
    write_capability = "manage_sites"
    filterset_fields = ["client", "city", "state_province", "country", "is_active"]
    search_fields = ["name", "address", "city", "state_province"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        # A client portal login sees its own client's sites only.
        return for_client(
            Site.objects.select_related("client")
            .prefetch_related("contacts")
            .annotate(device_count=Count("devices"))
            .order_by("-created_at"),
            self.request.user,
            "client_id",
        )

    def get_serializer_class(self):
        if self.action == "list":
            return SiteListSerializer
        return SiteDetailSerializer


    def perform_create(self, serializer):
        site = serializer.save()
        inform_line(
            self.request.user,
            title=f"Site added: {site.name}",
            message=f"{(site.client.name + ' - ') if site.client_id else ''}{site.city or ''} - by {_who(self.request.user)}".replace("  ", " ").strip(" -"),
            link=f"/sites?site={site.pk}", ref=f"fyi-site:{site.pk}",
        )


class SiteContactViewSet(viewsets.ModelViewSet):
    queryset = SiteContact.objects.select_related("site").all()
    serializer_class = SiteContactSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    write_capability = "manage_sites"
    filterset_fields = ["site", "is_primary"]
    search_fields = ["name", "email", "phone"]


class SiteZoneViewSet(viewsets.ModelViewSet):
    queryset = SiteZone.objects.select_related("site").all()
    serializer_class = SiteZoneSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    write_capability = "manage_sites"
    filterset_fields = ["site"]
    search_fields = ["name"]


def set_helpers(installation, ids, user) -> None:
    """Make these people the installation's helpers (the installer excepted)."""
    from apps.accounts.models import User

    if installation is None:
        return
    ids = [str(i) for i in (ids or []) if i and str(i) != str(installation.installed_by_id)]
    before = set(installation.helpers.values_list("pk", flat=True))
    installation.helpers.set(User.objects.filter(pk__in=ids, is_active=True))
    crew_changed(installation, before, user)


def set_helpers_for_asset(device, ids, user) -> None:
    """Helpers named where the asset is assigned land on its open installation."""
    installation = (
        DeviceInstallation.objects.filter(device=device, completed_at__isnull=True)
        .order_by("-installed_at").first()
    )
    set_helpers(installation, ids, user)


def crew_changed(installation, before: set, user) -> None:
    """Helpers added or taken off: on the log, and the newcomers told."""
    from apps.notifications import service as notices

    now = set(installation.helpers.values_list("pk", flat=True))
    if now == before:
        return
    names = ", ".join(activity.name(h) for h in installation.helpers.all()) or "none"
    activity.log(installation, "helpers", f"Helpers: {names}", user=user)
    added = list(installation.helpers.filter(pk__in=now - before))
    if added:
        notices.tell(
            added, exclude=[user], kind="work_assigned",
            title=f"You are helping on {installation.device.asset_code}",
            message=(
                f"Installer: {activity.name(installation.installed_by) or 'not named yet'} at {installation.site.name}. "
                "Check in and check out at the site from the field app."
            ),
            link=f"/installation-tracker?installation={installation.pk}", data={"installation": str(installation.pk)},
        )


def file_handover_document(record, user) -> None:
    """File the signed handover sheet with the job's and the asset's documents.

    It is the same file the handover record holds, so it appears under the
    installation's Related Documents, the asset's Documents and the library.
    """
    from apps.infrastructure.models import Document

    installation = record.installation
    try:
        size = record.signed_document.size
    except Exception:  # noqa: BLE001 - a missing file still gets its entry
        size = 0
    Document.objects.create(
        title=f"Signed handover - {record.device.asset_code}",
        doc_type=Document.DocType.HANDOVER,
        file=record.signed_document.name,
        file_size=size,
        description=f"Accepted by {record.accepted_by_name} on {record.handover_date:%d %b %Y}",
        device=record.device,
        site=installation.site,
        installation=installation,
        project=record.device.project_on,
        uploaded_by=user,
    )


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
        .prefetch_related("photos", "steps", "delays", "device__clients", "helpers")
        .all()
    )
    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_installations"
    # Opening a job and saying who does it is assignment; the checklist and
    # its templates are the installer's work; going live and handing over
    # are checked inside, against the installer and the capability.
    write_capability = "assign_installation"
    action_capabilities = {
        "activate": None, "handover": None, "verify_asset": None,
        "export": "view_installations", "handover_document": "view_installations",
        "activity": "view_installations",
        "reorder_steps": "edit_installation",
        "save_step_template": "manage_setup", "apply_step_template": "edit_installation",
    }
    filterset_fields = ["device", "site", "installed_by", "device__assigned_client", "device__project"]

    search_fields = [
        "device__asset_code", "device__display_name", "device__serial_number",
        "device__assigned_client__name", "device__clients__name",
        "installed_by__first_name", "installed_by__last_name", "installed_by__username",
        "site__name", "position_label",
    ]
    ordering_fields = ["installed_at", "due_date", "completed_at", "created_at"]

    def create(self, request, *args, **kwargs):
        """There is no opening a job by hand.

        An installation is the end of the asset's own road - registered,
        built from its components, planned and approved on its project, then
        assigned to a technician - and that assignment is what opens the job
        (services.open_installation_for). A job typed in here skipped all of
        it and put assets on the tracker that no project had agreed to.
        """
        return Response(
            {"detail": (
                "Installations are not added by hand. Assign the asset to a technician "
                "on its page (Assets › the asset › Installation & Activation); "
                "that opens its installation here."
            )},
            status=drf_status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def perform_update(self, serializer):
        """Edits say what changed: who installs it, when it is due, where."""
        watched = {
            "installed_by_id": "installer", "due_date": "due date", "position_label": "location at site",
            "notes": "notes", "vendor_id": "vendor", "external_vendor_name": "vendor", "installed_at": "assigned date",
        }
        before = {f: getattr(serializer.instance, f) for f in watched}
        helped_before = set(serializer.instance.helpers.values_list("pk", flat=True))
        installation = serializer.save()
        crew_changed(installation, helped_before, self.request.user)

        def moved(f):
            # The form sends the assigned time to the minute; a resave that
            # only drops the seconds is not an edit anybody made.
            old, new = before[f], getattr(installation, f)
            if hasattr(old, "second") and hasattr(new, "second"):
                return old.replace(second=0, microsecond=0) != new.replace(second=0, microsecond=0)
            return old != new

        changed = sorted({label for f, label in watched.items() if moved(f)})
        if installation.installed_by_id != before["installed_by_id"] and installation.installed_by_id:
            activity.log(installation, "reassigned", f"Reassigned to {activity.name(installation.installed_by)}",
                         user=self.request.user)
            changed = [c for c in changed if c != "installer"]
        if changed:
            activity.log(installation, "edited", "Edited: " + ", ".join(changed), user=self.request.user)

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        """Who did what on this installation, and when - newest first."""
        return Response(activity.rows(self.get_object()))

    def destroy(self, request, *args, **kwargs):
        """An installation record is never deleted - by anyone.

        It is the asset's history on site: who was sent, what was done,
        when it went live and who signed for it. Overriding the action (not
        a capability check) means the Administrator, who holds every
        capability, is refused too. An asset that came down is recorded as
        removed (removed_at); a wrong technician is reassigned on the asset.
        """
        return Response(
            {"detail": (
                "Installation records cannot be deleted. Reassign the technician on the "
                "asset's page, or record the removal if the asset has come down."
            )},
            status=drf_status.HTTP_405_METHOD_NOT_ALLOWED,
        )

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
        from .ordering import keep_handover_last

        with transaction.atomic():
            apply_step_order(installation.pk, ids)
            keep_handover_last(installation.pk)
        installation.refresh_from_db()
        activity.log(installation, "steps_reordered", "Steps reordered", user=request.user)
        return Response(self.get_serializer(installation).data)

    def get_queryset(self):
        qs = super().get_queryset()
        # ?crew=<id>: the jobs that person is on - as the installer or a helper.
        crew = self.request.query_params.get("crew")
        if crew:
            qs = qs.filter(Q(installed_by_id=crew) | Q(helpers__id=crew)).distinct()
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
            from .serializers import installation_progress

            progress = installation_progress(inst)
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
        activity.log(installation, "steps_added", f"Standard checklist applied ({len(lines)} steps)", user=request.user)
        return Response({
            "applied": len(lines),
            "installation": DeviceInstallationDetailSerializer(
                installation, context=self.get_serializer_context()
            ).data,
        })

    @action(detail=True, methods=["post"], url_path="verify-asset")
    def verify_asset(self, request, pk=None):
        """The phone scanned a label: is it the asset this installation is for?"""
        from apps.attendance.presence import require_on_site, verify_asset

        installation = self.get_object()
        user = request.user
        office = can(user, "act_across_teams") or can(user, "assign_installation")
        if not office and installation.installed_by_id != user.id:
            return Response({"detail": "Only the assigned installer verifies this asset."}, status=drf_status.HTTP_403_FORBIDDEN)
        if not office:
            require_on_site(user, installation.site)
        verify_asset(user, installation, installation.device, request.data.get("code"))
        activity.log(installation, "asset_scanned", f"Asset {installation.device.asset_code} scanned on site", user=user)
        return Response(DeviceInstallationDetailSerializer(installation, context=self.get_serializer_context()).data)

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
        mine = user.id in (installation.installed_by_id, installation.device.assigned_technician_id)
        if not (mine and can(user, "activate_asset")) and not (
            can(user, "activate_asset") and can(user, "act_across_teams")
        ) and not (
            installation.installed_by_id and user.manages(installation.installed_by) and can(user, "activate_asset")
        ):
            return Response(
                {"detail": "Only the assigned installer or operations management can activate this asset."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        if mine and not (can(user, "act_across_teams") or can(user, "assign_installation")):
            from apps.attendance.presence import require_on_site, require_verified_asset

            require_on_site(user, installation.site)
            require_verified_asset(installation, installation.device)

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
            # something the technician on site settles: it is recorded on
            # the project, and dated from this moment.

        installation.refresh_from_db()
        installation._prefetched_objects_cache = {}
        self._tell_the_project(installation, user, f"{device.asset_code} is live")
        activity.log(installation, "activated", f"{device.asset_code} marked Active, with a photo of it running", user=user)
        return Response(
            DeviceInstallationDetailSerializer(installation, context=self.get_serializer_context()).data
        )

    def _tell_the_project(self, installation, actor, title):
        """The project running this asset hears it moved."""
        from apps.assets.serializers import _project_of

        device = installation.device
        project = _project_of(device)
        people = []
        if project is not None and project.manager_id:
            people.append(project.manager)
        people += notices.holders_of("record_client_warranty") if project is None else []
        notices.tell(
            people, exclude=[actor],
            title=title,
            message=f"{device.display_name or ''} at {installation.site.name}" + (f" · {project.name}" if project else ""),
            link=(f"/projects?project={project.pk}&tab=execution" if project else f"/assets?device={device.pk}"),
            data={"installation": str(installation.pk)},
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def handover(self, request, pk=None):
        """Formal handover (WF-12): record acceptance, assign client + site to
        the asset, complete the handover step and move the asset to Active."""
        installation = self.get_object()
        user = request.user
        if not can(user, "close_installation") and installation.installed_by_id != user.id:
            return Response(
                {"detail": "Only the assigned installer or operations management can hand over."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if getattr(installation, "handover", None):
            return Response(
                {"detail": "This installation has already been handed over."},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if not can(user, "close_installation"):
            from apps.attendance.presence import require_on_site

            require_on_site(user, installation.site)
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
            if record.signed_document:
                file_handover_document(record, user)
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
            # The handover is the end of the job. With every work step skipped
            # and no handover step on the checklist, nothing above stamped it,
            # so the field app kept asking the installer to check in.
            if installation.completed_at is None:
                installation.completed_at = clock.now()
                installation.save(update_fields=["completed_at", "updated_at"])
            _anchor_client_warranties(installation)

            device.refresh_from_db()
            if device.status != "active":
                device._transition_user = user
                device._transition_reason = f"Handover accepted by {record.accepted_by_name}"
                device.status = "active"
                device.save(update_fields=["status", "updated_at"])

        self._tell_the_project(installation, user, f"{installation.device.asset_code} handed over to the client")
        activity.log(installation, "handed_over", (
            f"Handover recorded - accepted by {installation.handover.accepted_by_name}"
            if getattr(installation, "handover", None) is not None else "Handover recorded"
        ), user=user)
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

    def _from_the_site(self, installation):
        """The installer works the checklist from the site; who else may edit
        it (the office, their supervisor) is decided further down."""
        from apps.attendance.presence import require_on_site, require_verified_asset

        user = self.request.user
        if installation.installed_by_id == user.id and not can(user, "act_across_teams"):
            require_on_site(user, installation.site)
            require_verified_asset(installation, installation.device)

    def _may_change_checklist(self, installation):
        """The office lays a checklist out; the installer may add to it or
        take an unworked step off it on their own job, from the site."""
        user = self.request.user
        if can(user, "assign_installation") or can(user, "act_across_teams"):
            return
        if may_advance_installation(user, installation) and can(user, "edit_installation"):
            return
        raise PermissionDenied("Only the office or this job's installer changes its checklist.")

    def create(self, request, *args, **kwargs):
        # A new step goes on the end of the list, wherever the screen thought
        # the end was: the phone counted without Handover and asked for a
        # number Handover already held, and was refused.
        from django.db.models import Max

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        last = (
            InstallationStep.objects.filter(installation_id=data.get("installation"))
            .aggregate(m=Max("step_number"))["m"]
            or 0
        )
        data["step_number"] = last + 1
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        serializer.instance.refresh_from_db()
        return Response(self.get_serializer(serializer.instance).data, status=drf_status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        from .ordering import keep_handover_last

        installation = serializer.validated_data["installation"]
        refuse_if_closed_out(installation)
        self._may_change_checklist(installation)
        self._from_the_site(installation)
        with transaction.atomic():
            step = serializer.save()
            # Added after Handover, it would wait on Handover, which waits on
            # it: the new step goes in before Handover instead.
            keep_handover_last(installation.pk)
        activity.log(installation, "step_added", f"Step added: {activity.step_name(step)}", user=self.request.user, step=step)

    def perform_update(self, serializer):
        from django.utils import timezone
        from rest_framework.exceptions import ValidationError as _VE

        step = serializer.instance
        refuse_if_closed_out(step.installation)
        self._from_the_site(step.installation)

        new_status = serializer.validated_data.get("status", step.status)
        if new_status == InstallationStep.StepStatus.SKIPPED and step.status != new_status:
            raise _VE({"status": (
                "Steps are not skipped. Start it and complete it - or, before any "
                "step has started, remove it from the checklist."
            )})
        if step.step_type == InstallationStep.StepType.HANDOVER and new_status != step.status:
            raise _VE({"status": (
                "Handover completes itself when the signed handover is recorded, "
                "at the end of the job - it is not started or ticked off by hand."
            )})
        moving_on = new_status in (
            InstallationStep.StepStatus.IN_PROGRESS,
            InstallationStep.StepStatus.COMPLETED,
        )
        # A checklist is a sequence. Step 7 could be marked done with steps 2
        # and 3 untouched, and then the asset went live over the gap - so
        # "Completed" stopped meaning the work before it had been done.
        # An installation with nobody on it can still be worked through to
        # the end by whoever has the rights, and the asset then goes live
        # with no record of who put it up. Somebody has to be named before
        # the first step moves — our own installer, or the vendor whose
        # crew is doing it on a turnkey job.
        job = step.installation
        if moving_on and new_status != step.status and not (
            job.installed_by_id or job.vendor_id or (job.external_vendor_name or "").strip()
        ):
            raise _VE({"status": (
                "Nobody is assigned to this installation. Name the installer, "
                "or the vendor doing it, before the work is recorded against it."
            )})

        if moving_on and new_status != step.status:
            behind = [
                s.custom_label or s.get_step_type_display()
                # Handover comes after everything, wherever it is numbered.
                for s in step.installation.steps.filter(step_number__lt=step.step_number)
                .exclude(step_type=InstallationStep.StepType.HANDOVER)
                if s.status not in (
                    InstallationStep.StepStatus.COMPLETED,
                    InstallationStep.StepStatus.SKIPPED,
                )
            ]
            if behind:
                raise _VE({"status": (
                    f"Finish the steps before this one first: {', '.join(behind)}. "
                    "They run in the order shown."
                )})

        # Work that was never started cannot be finished. The step was being
        # ticked straight from Not Started to Completed, so "Completed" said
        # the job was done without anybody having begun it — and the dates
        # the card shows had nothing behind them.
        if (
            new_status == InstallationStep.StepStatus.COMPLETED
            and step.status != new_status
            and step.status != InstallationStep.StepStatus.IN_PROGRESS
        ):
            if step.status == InstallationStep.StepStatus.ON_HOLD:
                raise _VE({"status": (
                    "This step is on hold. Start it again before marking it complete."
                )})
            raise _VE({"status": (
                "Start this step before completing it — a step that was never "
                "started has no record of the work behind it."
            )})

        extra = {}
        if new_status == InstallationStep.StepStatus.IN_PROGRESS and step.started_by_id is None:
            extra["started_by"] = self.request.user
        if new_status == InstallationStep.StepStatus.COMPLETED and step.status != new_status:
            extra["completed_by"] = self.request.user
            if "completed_at" not in serializer.validated_data and not step.completed_at:
                extra["completed_at"] = clock.now()
        was = step.status
        step = serializer.save(**extra)
        if step.status != was:
            moved = {
                "in_progress": "resumed" if was == "on_hold" else "started",
                "on_hold": "put on hold", "completed": "completed", "skipped": "skipped",
                "not_started": "reset to Not Started",
            }.get(step.status, step.get_status_display().lower())
            activity.log(step.installation, f"step_{step.status}", f"{activity.step_name(step)} {moved}",
                         user=self.request.user, step=step)

    def perform_destroy(self, instance):
        """Remove the step, then close the gap it leaves in the numbering."""
        from rest_framework.exceptions import ValidationError as _VE

        from .ordering import renumber_steps

        refuse_if_closed_out(instance.installation)
        self._may_change_checklist(instance.installation)
        # A step that has been worked is part of the record of the job.
        if instance.status != InstallationStep.StepStatus.NOT_STARTED:
            raise _VE({"detail": (
                f"{instance.custom_label or instance.get_step_type_display()} has been worked on, "
                "so it stays on the checklist: start it and complete it."
            )})
        self._from_the_site(instance.installation)
        installation_id = instance.installation_id
        removed = activity.step_name(instance)
        installation = instance.installation
        with transaction.atomic():
            instance.delete()
            renumber_steps(installation_id)
        activity.log(installation, "step_removed", f"Step removed: {removed}", user=self.request.user)

    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_installations"
    # Steps are added and removed by whoever assigns the job; advancing one
    # is the installer's, checked against the installation itself.
    write_capability = "assign_installation"
    # Adding a step and taking an unworked one off are open to the job's own
    # installer too, checked against the installation in perform_create and
    # perform_destroy: the phone offers both, and the gate refused them.
    action_capabilities = {"update": None, "partial_update": None, "create": None, "destroy": None}

    def get_permissions(self):
        if self.action in ("update", "partial_update"):
            return [IsAuthenticated(), IsSuperAdminOrAssignedInstaller()]
        return super().get_permissions()


class InstallationDelayViewSet(viewsets.ModelViewSet):
    queryset = InstallationDelay.objects.select_related(
        "installation", "step", "reported_by"
    ).all()
    serializer_class = InstallationDelaySerializer
    filterset_fields = ["installation", "step", "cause"]
    ordering_fields = ["created_at"]

    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_installations"
    write_capability = "assign_installation"
    # Delays are logged by the installer (checked against the job inside);
    # whoever assigns installations edits and resolves them.
    action_capabilities = {"create": None}

    def perform_update(self, serializer):
        was = serializer.instance.resolved_at
        delay = serializer.save()
        if delay.resolved_at and not was:
            activity.log(delay.installation, "delay_resolved", f"Delay resolved: {delay.get_cause_display()}",
                         user=self.request.user, step=delay.step)

    def perform_create(self, serializer):
        installation = serializer.validated_data["installation"]
        user = self.request.user
        if not may_advance_installation(user, installation):
            raise PermissionDenied("This installation is not yours to flag a delay on.")
        delay = serializer.save(reported_by=user)
        activity.log(installation, "delay", (
            f"Delay flagged: {delay.get_cause_display()}"
            + (f" on {activity.step_name(delay.step)}" if delay.step_id else "")
            + (f" - {delay.description}" if getattr(delay, "description", "") else "")
        ), user=user, step=delay.step)
        notices.tell(
            notices.holders_of("assign_installation", exclude=[user]),
            title=f"Delay flagged: {installation.device.asset_code}",
            message=f"{delay.get_cause_display()} at {installation.site.name}" + (f" — {delay.description}" if getattr(delay, "description", "") else ""),
            link=f"/installation-tracker?installation={installation.pk}", data={"installation": str(installation.pk)},
        )


class InstallationPhotoViewSet(viewsets.ModelViewSet):
    queryset = InstallationPhoto.objects.select_related("installation").all()
    serializer_class = InstallationPhotoSerializer
    filterset_fields = ["installation", "photo_type"]

    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_installations"
    write_capability = "edit_installation"
    # Field techs and vendors attach photos to their own jobs (checked inside).
    action_capabilities = {"create": None}

    def perform_create(self, serializer):
        user = self.request.user
        if getattr(user, "role", "") == "vendor":
            # Vendor-portal users may only photograph their own installations.
            installation = serializer.validated_data["installation"]
            if not user.supplier_id or installation.vendor_id != user.supplier_id:
                raise PermissionDenied("Vendors can only add photos to their own installations.")
        photo = serializer.save(taken_by=user)
        activity.log(photo.installation, "photo", f"{photo.get_photo_type_display()} photo added", user=user)
