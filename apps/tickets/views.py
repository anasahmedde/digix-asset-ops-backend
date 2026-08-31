from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.exports import EXPORT_MAX_ROWS, export_params, log_export, xlsx_response
from common.permissions import MANAGER_ROLES, AdminManagerWriteElseRead, TechnicianCanCreate

from .models import Ticket, TicketAttachment, TicketComment, TicketIssueType
from .serializers import (
    TicketAssignSerializer,
    TicketAttachmentSerializer,
    TicketCommentSerializer,
    TicketIssueTypeSerializer,
    TicketListSerializer,
    TicketReviewSerializer,
    TicketSerializer,
    TicketSubmitCompletionSerializer,
    TicketTransitionSerializer,
)


def _is_assigned_vendor(user, ticket):
    """Vendor-portal user whose supplier is this ticket's assigned vendor (XC-04)."""
    return bool(
        getattr(user, "role", "") == "vendor"
        and getattr(user, "supplier_id", None)
        and ticket.assigned_vendor_id == user.supplier_id
    )


class TicketIssueTypeViewSet(viewsets.ModelViewSet):
    """Fault catalogue (Module Burnt, HDMI Cable Issue, …) managed from Setup."""

    queryset = TicketIssueType.objects.all()
    serializer_class = TicketIssueTypeSerializer
    permission_classes = [IsAuthenticated, AdminManagerWriteElseRead]
    filterset_fields = ["is_active"]
    search_fields = ["name"]


class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.select_related(
        "device", "site", "issue_type", "assigned_to", "assigned_vendor",
        "reported_by", "completed_by", "reviewed_by",
    ).prefetch_related("attachments", "comments", "devices").all()
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = [
        "status", "priority", "category", "issue_type", "assigned_to",
        "assigned_vendor", "site", "escalated", "is_billable",
    ]
    search_fields = [
        "ticket_number", "title", "description",
        "site__name", "assigned_to__first_name", "assigned_to__last_name",
    ]
    ordering_fields = ["created_at", "due_date", "response_due_at", "priority", "status"]

    def get_queryset(self):
        """Role-scoped visibility.

        Field staff (technicians) only see tickets assigned to them or that
        they raised. Vendor-portal users only see tickets assigned to their
        supplier. Oversight roles (admin/ops/supervisor) and marketing
        (who relay client decisions and close tickets) see everything.
        """
        qs = super().get_queryset()
        # ?device= matches the primary asset OR any linked asset (MW-03 —
        # handled here rather than filterset_fields so both paths hit).
        device_id = self.request.query_params.get("device")
        if device_id:
            import uuid as _uuid

            from rest_framework.exceptions import ValidationError as DRFValidationError

            try:
                _uuid.UUID(device_id)
            except (ValueError, AttributeError, TypeError):
                raise DRFValidationError({"device": "Enter a valid UUID."})
            qs = qs.filter(Q(device_id=device_id) | Q(devices__id=device_id)).distinct()
        # ?flag=unassigned|sla_breached|past_due|in_review — dashboard
        # drill-downs; handled here so list AND export share them. Unknown
        # values are ignored.
        flag = self.request.query_params.get("flag")
        if flag == "unassigned":
            qs = qs.filter(
                assigned_to__isnull=True, assigned_vendor__isnull=True
            ).exclude(status=Ticket.Status.CLOSED)
        elif flag == "sla_breached":
            qs = qs.filter(
                Q(escalated=True)
                | Q(response_due_at__lt=timezone.now(), status=Ticket.Status.OPEN)
            )
        elif flag == "past_due":
            qs = qs.filter(due_date__lt=timezone.localdate()).exclude(
                status__in=[Ticket.Status.CLOSED, Ticket.Status.APPROVED]
            )
        elif flag == "in_review":
            qs = qs.filter(status=Ticket.Status.PENDING_REVIEW)
        user = self.request.user
        role = getattr(user, "role", "")
        if role == "technician" and not user.is_superuser:
            return qs.filter(Q(assigned_to=user) | Q(reported_by=user))
        if role == "vendor" and not user.is_superuser:
            # Vendor scope (XC-04): only tickets assigned to their supplier;
            # a vendor login without a supplier link sees nothing.
            if not user.supplier_id:
                return qs.none()
            return qs.filter(assigned_vendor_id=user.supplier_id)
        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return TicketListSerializer
        return TicketSerializer

    def perform_create(self, serializer):
        serializer.save(reported_by=self.request.user)

    # ── Excel export (XC-01) ──────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """Excel export of tickets — role-scoped and filter-aware."""
        qs = self.filter_queryset(self.get_queryset())[:EXPORT_MAX_ROWS]
        columns = [
            "Ticket #", "Title", "Category", "Priority", "Status",
            "Asset Code", "Site", "Assigned To", "Vendor",
            "Billable", "Charge To", "Repair Cost",
            "Due Date", "Created At", "Closed At",
        ]
        rows = []
        for t in qs:
            rows.append([
                t.ticket_number,
                t.title,
                t.get_category_display(),
                t.get_priority_display(),
                t.get_status_display(),
                t.device.asset_code if t.device_id else "",
                t.site.name if t.site_id else "",
                (t.assigned_to.get_full_name() or t.assigned_to.username) if t.assigned_to_id else "",
                t.assigned_vendor.name if t.assigned_vendor_id else "",
                t.is_billable,
                t.get_charge_to_display() if t.charge_to else "",
                t.repair_cost,
                t.due_date,
                t.created_at,
                t.closed_at,
            ])
        log_export(request.user, "ticket", len(rows), export_params(request))
        return xlsx_response("tickets", "Tickets", columns, rows)

    # ── Assignment (Operations only) ──────────────────────────────────

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """Assign the ticket to an employee and/or a vendor (in-warranty assets)."""
        if getattr(request.user, "role", "") not in MANAGER_ROLES:
            return Response(
                {"detail": "Only Operations can assign tickets."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ticket = self.get_object()
        ser = TicketAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        parts = []
        if "assigned_to" in ser.validated_data:
            from apps.accounts.models import User

            user_id = ser.validated_data["assigned_to"]
            assignee = User.objects.filter(pk=user_id).first() if user_id else None
            ticket.assigned_to = assignee
            parts.append(f"assignee → {assignee.get_full_name() or assignee.username}" if assignee else "assignee cleared")
        if "assigned_vendor" in ser.validated_data:
            from apps.suppliers.models import Supplier

            vendor_id = ser.validated_data["assigned_vendor"]
            vendor = Supplier.objects.filter(pk=vendor_id).first() if vendor_id else None
            ticket.assigned_vendor = vendor
            parts.append(f"vendor → {vendor.name}" if vendor else "vendor cleared")
        if (ticket.assigned_to or ticket.assigned_vendor) and not ticket.assigned_at:
            ticket.assigned_at = timezone.now()
        ticket.save(update_fields=["assigned_to", "assigned_vendor", "assigned_at", "updated_at"])

        notes = ser.validated_data.get("notes", "")
        TicketComment.objects.create(
            ticket=ticket,
            author=request.user,
            content=(notes or f"Assignment updated: {', '.join(parts)}"),
            comment_type=TicketComment.CommentType.STATUS_CHANGE,
        )
        ticket.refresh_from_db()
        return Response(TicketSerializer(ticket).data)

    # ── Status transition ─────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        ticket = self.get_object()
        user = request.user
        role = getattr(user, "role", "")
        # A vendor-portal user counts as the assignee on tickets assigned to
        # their supplier (XC-04) — same workflow powers, same restrictions.
        is_assignee = ticket.assigned_to_id == user.id or _is_assigned_vendor(user, ticket)
        is_manager = role in MANAGER_ROLES

        ser = TicketTransitionSerializer(data=request.data, context={"ticket": ticket})
        ser.is_valid(raise_exception=True)

        old_status = ticket.status
        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "")

        is_marketing = role in ("marketing", "marketing_head")
        is_reporter = ticket.reported_by_id == user.id

        # Decisions OUT of an approval stage belong to the approver, not the assignee.
        if old_status == Ticket.Status.PENDING_OPS_APPROVAL and not is_manager:
            return Response(
                {"detail": "Only Operations can decide on this approval request."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if old_status == Ticket.Status.PENDING_CLIENT_APPROVAL and not (is_manager or is_marketing):
            return Response(
                {"detail": "Only Marketing or Operations can relay the client's decision."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Reopening a closed ticket: Operations or the original reporter only,
        # within the reopen window, and a reason is required.
        is_reopen = old_status == Ticket.Status.CLOSED
        if is_reopen:
            if not (is_manager or is_reporter):
                return Response(
                    {"detail": "Only Operations or the reporter can reopen a closed ticket."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            # Legacy rows closed before closed_at existed fall back to the
            # last update so the window never fails open.
            closed_reference = ticket.closed_at or ticket.updated_at
            deadline = (
                closed_reference + timedelta(days=Ticket.REOPEN_WINDOW_DAYS)
                if closed_reference else None
            )
            if deadline and timezone.now() > deadline:
                return Response(
                    {"detail": (
                        f"Closed tickets can only be reopened within "
                        f"{Ticket.REOPEN_WINDOW_DAYS} days of closure."
                    )},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not notes.strip():
                return Response(
                    {"notes": "Reopen reason is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        assignee_transitions = {
            Ticket.Status.IN_PROGRESS,
            Ticket.Status.ON_HOLD,
            Ticket.Status.BLOCKED,
            Ticket.Status.ALIGNMENT_PENDING,
            Ticket.Status.PENDING_OPS_APPROVAL,
        }
        approval_stages = (
            Ticket.Status.PENDING_OPS_APPROVAL,
            Ticket.Status.PENDING_CLIENT_APPROVAL,
        )
        if (
            new_status in assignee_transitions
            and old_status not in approval_stages
            and not is_reopen  # reopen has its own gate above
            and not is_assignee
            and not is_manager
        ):
            return Response(
                {"detail": "Only the assigned person can perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if new_status in (Ticket.Status.APPROVED, Ticket.Status.REJECTED) and not is_manager and not is_reporter:
            return Response(
                {"detail": "Only the reporter or a manager can perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Closing is the final client sign-off: Marketing, a manager, or the reporter.
        if new_status == Ticket.Status.CLOSED and not (is_manager or is_marketing or is_reporter):
            return Response(
                {"detail": "Only Marketing, Operations or the reporter can close a ticket."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if new_status == Ticket.Status.PENDING_OPS_APPROVAL and not notes.strip():
            return Response(
                {"notes": "Describe what needs approval (issue found, expected cost/parts)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if new_status == Ticket.Status.BLOCKED:
            if not notes.strip():
                return Response(
                    {"notes": "Blocker reason is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ticket.blocked_reason = notes

        if new_status == Ticket.Status.ON_HOLD:
            if not notes.strip():
                return Response(
                    {"notes": "Hold reason is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ticket.hold_reason = notes

        if new_status == Ticket.Status.IN_PROGRESS:
            ticket.blocked_reason = ""
            ticket.hold_reason = ""

        # Stamp closure time on close; clear it again when the ticket reopens.
        if new_status == Ticket.Status.CLOSED:
            ticket.closed_at = timezone.now()
            # Closing a warranty claim releases the warranty from its
            # "claim pending" state (WF-14).
            if ticket.category == Ticket.Category.WARRANTY_CLAIM and ticket.warranty_id:
                from apps.warranties.models import Warranty

                warranty = ticket.warranty
                if warranty.status == Warranty.Status.CLAIMED:
                    warranty.status = (
                        Warranty.Status.EXPIRED
                        if warranty.end_date and warranty.end_date < timezone.localdate()
                        else Warranty.Status.ACTIVE
                    )
                    warranty.save(update_fields=["status", "updated_at"])
        elif is_reopen:
            ticket.closed_at = None
            # Reopening a warranty claim puts the warranty back into its
            # "claim pending" state — mirror of the close branch above.
            if ticket.category == Ticket.Category.WARRANTY_CLAIM and ticket.warranty_id:
                from apps.warranties.models import Warranty

                warranty = ticket.warranty
                if warranty.status in (Warranty.Status.ACTIVE, Warranty.Status.EXPIRED):
                    warranty.status = Warranty.Status.CLAIMED
                    warranty.save(update_fields=["status", "updated_at"])

        ticket.status = new_status
        ticket.save(update_fields=[
            "status", "blocked_reason", "hold_reason", "closed_at", "updated_at",
        ])

        old_display = dict(Ticket.Status.choices).get(old_status, old_status)
        new_display = dict(Ticket.Status.choices).get(new_status, new_status)
        if is_reopen:
            content = f"Ticket reopened: {notes}"
        else:
            content = notes or f"Status changed from {old_display} to {new_display}"
        TicketComment.objects.create(
            ticket=ticket,
            author=request.user,
            content=content,
            comment_type=TicketComment.CommentType.STATUS_CHANGE,
            old_status=old_status,
            new_status=new_status,
        )

        return Response(TicketSerializer(ticket).data)

    # ── Submit completion (assignee submits work for review) ──────────

    @action(
        detail=True, methods=["post"], url_path="submit-completion",
        parser_classes=[MultiPartParser, FormParser],
    )
    def submit_completion(self, request, pk=None):
        ticket = self.get_object()

        if ticket.assigned_to_id != request.user.id and not _is_assigned_vendor(request.user, ticket):
            return Response(
                {"detail": "Only the assigned person can submit completion."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not ticket.can_transition_to(Ticket.Status.PENDING_REVIEW):
            return Response(
                {"detail": f"Cannot submit for review from '{ticket.get_status_display()}' status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = TicketSubmitCompletionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        now = timezone.now()
        old_status = ticket.status
        completion_notes = ser.validated_data["completion_notes"]
        images = ser.validated_data.get("images", [])
        captions = ser.validated_data.get("captions", [])

        ticket.status = Ticket.Status.PENDING_REVIEW
        ticket.completion_notes = completion_notes
        ticket.parts_used = ser.validated_data.get("parts_used", "")
        ticket.completed_by = request.user
        ticket.completed_at = now
        ticket.save(update_fields=[
            "status", "completion_notes", "parts_used", "completed_by", "completed_at", "updated_at",
        ])

        for i, image in enumerate(images):
            caption = captions[i] if i < len(captions) else ""
            TicketAttachment.objects.create(
                ticket=ticket,
                uploaded_by=request.user,
                file=image,
                caption=caption,
                attachment_type=TicketAttachment.AttachmentType.COMPLETION,
            )

        TicketComment.objects.create(
            ticket=ticket,
            author=request.user,
            content=completion_notes,
            comment_type=TicketComment.CommentType.COMPLETION,
            old_status=old_status,
            new_status=Ticket.Status.PENDING_REVIEW,
        )

        ticket.refresh_from_db()
        return Response(TicketSerializer(ticket).data)

    # ── Review (reporter / supervisor approves or rejects) ────────────

    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        ticket = self.get_object()
        user = request.user
        role = getattr(user, "role", "")
        is_reporter = ticket.reported_by_id == user.id
        is_manager = role in MANAGER_ROLES

        if not is_reporter and not is_manager:
            return Response(
                {"detail": "Only the reporter or a manager can review tickets."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if ticket.status != Ticket.Status.PENDING_REVIEW:
            return Response(
                {"detail": "Ticket is not pending review."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = TicketReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        action_type = ser.validated_data["action"]
        comments = ser.validated_data.get("comments", "")
        now = timezone.now()
        old_status = ticket.status

        if action_type == "approve":
            ticket.status = Ticket.Status.APPROVED
            ticket.reviewed_by = request.user
            ticket.reviewed_at = now
            ticket.review_comments = comments
            ticket.resolved_at = now
            ticket.save(update_fields=[
                "status", "reviewed_by", "reviewed_at",
                "review_comments", "resolved_at", "updated_at",
            ])

            TicketComment.objects.create(
                ticket=ticket,
                author=request.user,
                content=comments or "Ticket approved.",
                comment_type=TicketComment.CommentType.APPROVAL,
                old_status=old_status,
                new_status=Ticket.Status.APPROVED,
            )
        else:
            ticket.status = Ticket.Status.REJECTED
            ticket.reviewed_by = request.user
            ticket.reviewed_at = now
            ticket.review_comments = comments
            ticket.completion_notes = ""
            ticket.completed_by = None
            ticket.completed_at = None
            ticket.save(update_fields=[
                "status", "reviewed_by", "reviewed_at", "review_comments",
                "completion_notes", "completed_by", "completed_at", "updated_at",
            ])

            TicketComment.objects.create(
                ticket=ticket,
                author=request.user,
                content=comments,
                comment_type=TicketComment.CommentType.REJECTION,
                old_status=old_status,
                new_status=Ticket.Status.REJECTED,
            )

        ticket.refresh_from_db()
        return Response(TicketSerializer(ticket).data)

    # ── Comments ──────────────────────────────────────────────────────

    @action(
        detail=True, methods=["get", "post"], url_path="comments",
        parser_classes=[JSONParser, MultiPartParser, FormParser],
    )
    def ticket_comments(self, request, pk=None):
        ticket = self.get_object()

        if request.method == "GET":
            comments = ticket.comments.select_related("author").all()
            return Response(TicketCommentSerializer(comments, many=True).data)

        ser = TicketCommentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not ser.validated_data.get("content", "").strip() and not ser.validated_data.get("image"):
            return Response(
                {"content": "Add a message or a photo."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ser.save(ticket=ticket, author=request.user, comment_type=TicketComment.CommentType.COMMENT)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    # ── Attachments ───────────────────────────────────────────────────

    @action(
        detail=True, methods=["get", "post"], url_path="attachments",
        parser_classes=[MultiPartParser, FormParser],
    )
    def ticket_attachments(self, request, pk=None):
        ticket = self.get_object()

        if request.method == "GET":
            attachments = ticket.attachments.select_related("uploaded_by").all()
            return Response(TicketAttachmentSerializer(attachments, many=True).data)

        ser = TicketAttachmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(ticket=ticket, uploaded_by=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)


class TicketAttachmentViewSet(
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = TicketAttachment.objects.select_related("uploaded_by").all()
    serializer_class = TicketAttachmentSerializer
    permission_classes = [IsAuthenticated]
