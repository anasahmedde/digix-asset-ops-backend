from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import MANAGER_ROLES, TechnicianCanCreate

from .models import Ticket, TicketAttachment, TicketComment
from .serializers import (
    TicketAttachmentSerializer,
    TicketCommentSerializer,
    TicketListSerializer,
    TicketReviewSerializer,
    TicketSerializer,
    TicketSubmitCompletionSerializer,
    TicketTransitionSerializer,
)


class TicketViewSet(viewsets.ModelViewSet):
    queryset = Ticket.objects.select_related(
        "device", "site", "assigned_to", "reported_by",
        "completed_by", "reviewed_by",
    ).prefetch_related("attachments", "comments").all()
    permission_classes = [IsAuthenticated, TechnicianCanCreate]
    filterset_fields = [
        "status", "priority", "category", "assigned_to", "site", "device",
    ]
    search_fields = ["title", "description"]
    ordering_fields = ["created_at", "due_date", "priority", "status"]

    def get_serializer_class(self):
        if self.action == "list":
            return TicketListSerializer
        return TicketSerializer

    def perform_create(self, serializer):
        serializer.save(reported_by=self.request.user)

    # ── Status transition ─────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        ticket = self.get_object()
        user = request.user
        role = getattr(user, "role", "")
        is_assignee = ticket.assigned_to_id == user.id
        is_manager = role in MANAGER_ROLES

        ser = TicketTransitionSerializer(data=request.data, context={"ticket": ticket})
        ser.is_valid(raise_exception=True)

        old_status = ticket.status
        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "")

        assignee_transitions = {
            Ticket.Status.IN_PROGRESS,
            Ticket.Status.ON_HOLD,
            Ticket.Status.BLOCKED,
        }
        if new_status in assignee_transitions and not is_assignee and not is_manager:
            return Response(
                {"detail": "Only the assigned person can perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )

        reviewer_transitions = {
            Ticket.Status.APPROVED,
            Ticket.Status.REJECTED,
            Ticket.Status.CLOSED,
        }
        if new_status in reviewer_transitions and not is_manager:
            is_reporter = ticket.reported_by_id == user.id
            if not is_reporter:
                return Response(
                    {"detail": "Only the reporter or a manager can perform this action."},
                    status=status.HTTP_403_FORBIDDEN,
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

        ticket.status = new_status
        ticket.save(update_fields=[
            "status", "blocked_reason", "hold_reason", "updated_at",
        ])

        old_display = dict(Ticket.Status.choices).get(old_status, old_status)
        new_display = dict(Ticket.Status.choices).get(new_status, new_status)
        TicketComment.objects.create(
            ticket=ticket,
            author=request.user,
            content=notes or f"Status changed from {old_display} to {new_display}",
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

        if ticket.assigned_to_id != request.user.id:
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
        ticket.completed_by = request.user
        ticket.completed_at = now
        ticket.save(update_fields=[
            "status", "completion_notes", "completed_by", "completed_at", "updated_at",
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

    @action(detail=True, methods=["get", "post"], url_path="comments")
    def ticket_comments(self, request, pk=None):
        ticket = self.get_object()

        if request.method == "GET":
            comments = ticket.comments.select_related("author").all()
            return Response(TicketCommentSerializer(comments, many=True).data)

        ser = TicketCommentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
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
