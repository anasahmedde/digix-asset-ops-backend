from apps.notifications.service import inform_line, who as _who
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.notifications import service as notices
from common.permissions import CapabilityGate

from .models import Quotation
from .pdf import build_quotation_pdf
from .serializers import QuotationSerializer, QuotationTransitionSerializer


def _spawn_project_if_needed(quotation: Quotation):
    """On acceptance, create the delivery Project sourced from this quotation
    and copy its items onto the project's BOM (mirrors the work-order spawn)."""
    from apps.teams.models import Project, ProjectBOMLine

    if Project.objects.filter(source_quotation=quotation).exists():
        return
    project = Project.objects.create(
        name=f"Project: {quotation.title}"[:300],
        description=quotation.description,
        client=quotation.client,
        site=quotation.site,
        source_quotation=quotation,
        phase=Project.Phase.PLANNING,
        status=Project.Status.PLANNING,
    )
    for item in quotation.items.all():
        ProjectBOMLine.objects.create(
            project=project,
            asset_type=item.asset_type,
            device_model=item.device_model,
            material_type=item.material_type,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            source_quotation_item=item,
        )


class QuotationViewSet(viewsets.ModelViewSet):
    # Reading a quotation is not the same as working one.
    queryset = (
        Quotation.objects.select_related("client", "site", "created_by")
        .prefetch_related(
            "items", "items__asset_type", "items__device_model", "items__material_type",
            "spawned_projects", "invoices__payments",
        )
        .all()
    )
    serializer_class = QuotationSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_quotations"
    write_capability = "manage_quotations"
    action_capabilities = {"print_pdf": "view_quotations", "suite_sign_in": "use_suite"}
    filterset_fields = ["status", "client", "site", "currency"]
    search_fields = ["quote_number", "title", "description"]
    ordering_fields = ["created_at", "valid_until", "total_amount"]

    def perform_create(self, serializer):
        quotation = serializer.save(created_by=self.request.user)
        inform_line(
            self.request.user,
            title=f"Quotation raised: {quotation.quote_number}",
            message=f"{quotation.title} - by {_who(self.request.user)}",
            link="/quotations", ref=f"fyi-quote:{quotation.pk}",
        )

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        quotation = self.get_object()
        ser = QuotationTransitionSerializer(data=request.data, context={"quotation": quotation})
        ser.is_valid(raise_exception=True)
        new_status = ser.validated_data["status"]
        if new_status == Quotation.Status.REJECTED and not (ser.validated_data.get("notes") or "").strip():
            return Response({"notes": ["Say why it was rejected."]}, status=400)

        update_fields = ["status", "updated_at"]
        quotation.status = new_status

        if new_status == Quotation.Status.ACCEPTED:
            quotation.accepted_at = timezone.now()
            update_fields += ["accepted_at"]

        # Acceptance and the project/BOM spawn must land together — a
        # failure mid-copy must not leave an accepted quote with half a BOM.
        with transaction.atomic():
            quotation.save(update_fields=update_fields)
            if new_status == Quotation.Status.ACCEPTED:
                from apps.finance.ledger import draft_full_invoice

                _spawn_project_if_needed(quotation)
                # Finance finds the invoice already drafted from the quotation.
                draft_full_invoice(quotation, request.user)

        if new_status == Quotation.Status.ACCEPTED:
            # get_object() prefetched spawned_projects before the spawn —
            # drop the stale cache so the response carries the new project.
            quotation._prefetched_objects_cache = {}
            # An accepted quote is a project somebody now has to run.
            project = quotation.spawned_projects.first() if hasattr(quotation, "spawned_projects") else None
            notices.tell(
                notices.holders_of("edit_projects", exclude=[request.user]),
                kind="work_assigned",
                title=f"Quotation {quotation.quote_number} accepted",
                message=f"{quotation.client.name if quotation.client_id else ''} · {quotation.title}"
                        + (" — project opened, assign its manager" if project else ""),
                link=(f"/projects?project={project.pk}" if project else "/projects"),
                data={"quotation": str(quotation.pk)},
            )

        return Response(QuotationSerializer(quotation).data)

    @action(detail=False, methods=["get"], url_path="suite")
    def suite_jobs(self, request):
        """DIGIX Suite jobs at Production Go-Ahead, and what was made from each."""
        from . import suite

        if not suite.enabled():
            return Response({"enabled": False, "jobs": []})
        from common.permissions import can

        try:
            jobs = suite.jobs()
        except suite.SuiteError as e:
            return Response({"enabled": True, "jobs": [], "error": str(e)})
        if not can(request.user, "view_prices"):
            # Blank, not zero: a figure this person may not see.
            for job in jobs:
                job.update(po_amount=None, net=None, gross=None, discount=None)
                for line in job["lines"]:
                    line["unit_price"] = None
        return Response({"enabled": True, "jobs": jobs})

    @action(detail=False, methods=["get"], url_path="suite/sign-in")
    def suite_sign_in(self, request):
        """A one-minute, single-use token Suite's /sso takes to sign this
        person in as the Suite account with their email."""
        from . import suite

        if not settings.SUITE_SSO_SECRET:
            return Response({"detail": "Signing in to Suite from here is not set up."}, status=400)
        if not (request.user.email or "").strip():
            return Response({"detail": "Your account has no email; Suite knows people by email. Ask an administrator to add it."}, status=400)
        return Response({"token": suite.sign_in_token(request.user)})

    @action(detail=False, methods=["post"], url_path="suite/create-project")
    def suite_create_project(self, request):
        """Make the quotation, project and assets for one Suite job; the
        client is chosen from this app's clients."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.clients.models import Client

        from . import suite

        if not suite.enabled():
            return Response({"detail": "The Suite link is not set up."}, status=400)
        try:
            client = Client.objects.filter(pk=request.data.get("client") or None).first()
        except (DjangoValidationError, ValueError):
            client = None
        if client is None:
            return Response({"client": ["Choose the client."]}, status=400)
        try:
            quotation, project, assets = suite.create_project(str(request.data.get("record_id") or ""), client, request.user)
        except suite.SuiteError as e:
            return Response({"detail": str(e)}, status=400)
        suite.push(quotation)  # the Suite job moves to Execution
        return Response({
            "quotation": str(quotation.pk), "quote_number": quotation.quote_number,
            "project": str(project.pk), "project_code": project.project_code,
            "assets": len(assets), "suite_sync_error": quotation.suite_sync_error,
        }, status=201)

    @action(detail=True, methods=["post"], url_path="suite-sync")
    def suite_sync(self, request, pk=None):
        """Try again to move the Suite job to where the project is."""
        from . import suite

        quotation = self.get_object()
        if not quotation.suite_record_id:
            return Response({"detail": "This quotation did not come from Suite."}, status=400)
        suite.push(quotation)
        quotation._prefetched_objects_cache = {}
        return Response(QuotationSerializer(quotation, context={"request": request}).data)

    @action(detail=True, methods=["get"], url_path="print")
    def print_pdf(self, request, pk=None):
        quotation = self.get_object()
        pdf_bytes = build_quotation_pdf(quotation)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{quotation.quote_number}.pdf"'
        return response
