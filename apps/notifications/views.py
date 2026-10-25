from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.tickets.models import Ticket
from common.permissions import IsSuperAdmin

from rest_framework.exceptions import PermissionDenied

from .models import CalendarEvent, Notification, PushToken, StickyNote, WebhookEndpoint
from .serializers import (
    CalendarEventSerializer,
    NotificationSerializer,
    PushTokenSerializer,
    StickyNoteSerializer,
    WebhookEndpointSerializer,
)
from .tasks import send_webhook_delivery


class PushTokenViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Register / de-register a device's Expo push token for the current user."""

    serializer_class = PushTokenSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "token"

    def get_queryset(self):
        return PushToken.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        obj, _ = PushToken.objects.update_or_create(
            token=token,
            defaults={
                "user": request.user,
                "platform": serializer.validated_data.get("platform", ""),
            },
        )
        return Response(PushTokenSerializer(obj).data, status=status.HTTP_201_CREATED)


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["notification_type", "is_read", "is_actionable"]
    search_fields = ["title", "message"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user)
        qs = qs.exclude(is_actionable=True, resolved_at__isnull=False)
        return qs

    @action(detail=False, methods=["get"], url_path="waiting-on")
    def waiting_on(self, request):
        """Whose desk something is on: everyone still holding an unanswered
        request filed under ``?ref=``. Empty once it is answered, or when
        nobody was asked."""
        ref = (request.query_params.get("ref") or "").strip()
        capability = (request.query_params.get("capability") or "").strip()
        if not ref and not capability:
            return Response([])

        from apps.accounts.models import User

        def named(u):
            # The Administrator can decide anything, so naming them on every
            # request says nothing: the desk is the people whose job it is.
            return u.role != User.Role.SUPER_ADMIN and not u.is_superuser
        if not ref:
            # Nothing filed: the desk is everyone who holds the right to decide.
            from .service import holders_of

            return Response([
                {"id": str(p.pk), "name": p.get_full_name() or p.username,
                 "role": (p.role or "").replace("_", " ").title(), "since": None}
                for p in holders_of(capability) if named(p)
            ])
        rows = (
            Notification.objects.filter(ref=ref, is_actionable=True, resolved_at__isnull=True)
            .select_related("recipient").order_by("created_at")
        )
        seen, out = set(), []
        for n in rows:
            if n.recipient_id in seen or not named(n.recipient):
                continue
            seen.add(n.recipient_id)
            out.append({
                "id": str(n.recipient_id),
                "name": n.recipient.get_full_name() or n.recipient.username,
                "role": (n.recipient.role or "").replace("_", " ").title(),
                "since": n.created_at,
            })
        return Response(out)

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({"count": count})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])
        return Response(NotificationSerializer(notification).data)

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        updated = (
            self.get_queryset()
            .filter(is_read=False)
            .update(is_read=True, read_at=timezone.now())
        )
        return Response({"updated": updated})

    @action(detail=False, methods=["get"])
    def assigned_tickets_summary(self, request):
        """Active tickets assigned to the current user — shown as login banner."""
        tickets = Ticket.objects.filter(
            assigned_to=request.user,
        ).exclude(
            status__in=("approved", "closed"),
        ).values(
            "id", "title", "priority", "status", "category", "due_date", "created_at",
        ).order_by("-created_at")

        overdue = 0
        today = timezone.now().date()
        for t in tickets:
            if t["due_date"] and t["due_date"] < today:
                overdue += 1

        return Response({
            "total": len(tickets),
            "overdue": overdue,
            "tickets": list(tickets),
        })


class WebhookEndpointViewSet(viewsets.ModelViewSet):
    queryset = WebhookEndpoint.objects.all()
    serializer_class = WebhookEndpointSerializer
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    filterset_fields = ["is_active"]
    search_fields = ["name", "url"]

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        webhook = self.get_object()
        payload = {
            "event": "webhook.test",
            "title": "Test webhook delivery",
            "message": "If you receive this, your webhook endpoint is configured correctly.",
        }
        send_webhook_delivery.delay(str(webhook.id), "webhook.test", payload)
        return Response({"status": "Test webhook queued for delivery."})


class StickyNoteViewSet(viewsets.ModelViewSet):
    """The dashboard's two boards.

    A person sees the team board and their own private notes, never anybody
    else's private notes. Only the author edits or removes a note.
    """

    serializer_class = StickyNoteSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["scope"]

    def get_queryset(self):
        user = self.request.user
        return (
            StickyNote.objects.filter(
                Q(scope=StickyNote.Scope.TEAM) | Q(author=user)
            )
            .select_related("author")
            .prefetch_related("mentions", "replies__author")
        )

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        """Answer a team note. Its author and whoever it tags are told."""
        from .models import StickyNoteReply
        from .service import tell

        note = self.get_object()
        if note.scope != StickyNote.Scope.TEAM:
            raise PermissionDenied("Only a team note takes replies.")
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"body": ["Write the reply."]}, status=status.HTTP_400_BAD_REQUEST)
        StickyNoteReply.objects.create(note=note, author=request.user, body=body[:2000])
        who = request.user.get_full_name() or request.user.username
        tell(
            [note.author, *note.mentions.all()], exclude=[request.user], kind=Notification.Type.SYSTEM,
            title=f"{who} replied on a team note", message=f"{body[:200]} — on: {note.body[:80]}",
            link="/", ref=f"note:{note.pk}", data={"note": str(note.pk)},
        )
        note = self.get_queryset().get(pk=note.pk)
        return Response(self.get_serializer(note).data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        note = serializer.save(author=self.request.user)
        self._tag_the_named(note)
        self._tell_the_tagged(note, set())

    def perform_update(self, serializer):
        # Ticking a note off is anybody's on that board; changing its words is the author's.
        if set(serializer.validated_data) - {"done"}:
            self._author_only(serializer.instance)
        before = set(serializer.instance.mentions.values_list("pk", flat=True))
        was_done = serializer.instance.done_at is not None
        note = serializer.save()
        # Somebody else ticking it off is the answer the author was waiting for.
        if note.scope == StickyNote.Scope.TEAM and not was_done and note.done_at and note.author_id != self.request.user.id:
            from .service import tell

            who = self.request.user.get_full_name() or self.request.user.username
            tell([note.author], kind=Notification.Type.SYSTEM, title=f"{who} marked your note done",
                 message=note.body[:200], link="/", ref=f"note:{note.pk}", data={"note": str(note.pk)})
        self._tag_the_named(note)
        # Only somebody newly tagged hears about it: ticking a note off, or
        # fixing a typo, does not tell everyone on it all over again.
        self._tell_the_tagged(note, before)

    def perform_destroy(self, instance):
        self._author_only(instance)
        instance.delete()

    def _author_only(self, note):
        if note.author_id != self.request.user.id:
            raise PermissionDenied("A note belongs to whoever wrote it.")

    @staticmethod
    def _tag_the_named(note):
        """@Full Name in the text tags that person, whether or not they were
        picked from the list: a name typed out in full was being shown on the
        board and never reaching the person."""
        if note.scope != StickyNote.Scope.TEAM or "@" not in note.body:
            return
        from apps.accounts.models import User

        text = note.body.casefold()
        # ponytail: scans every active account per note; fine for a company's
        # staff, index by name if it ever runs to thousands.
        named = [
            u for u in User.objects.filter(is_active=True).exclude(pk=note.author_id)
            if any(f"@{n}".casefold() in text for n in {u.get_full_name().strip(), u.username} if n)
        ]
        if named:
            note.mentions.add(*named)

    def _tell_the_tagged(self, note, before: set):
        """A tag is only useful if it reaches the person tagged."""
        if note.scope != StickyNote.Scope.TEAM:
            return
        from .service import tell

        who = note.author.get_full_name() or note.author.username
        # Through the service, so it reaches the bell and the phone at once
        # and opens the board, rather than sitting in the list until the
        # next refresh.
        tell(
            list(note.mentions.exclude(pk=note.author_id).exclude(pk__in=before)), kind=Notification.Type.SYSTEM,
            title=f"{who} tagged you on a team note", message=note.body[:300],
            link="/", ref=f"note:{note.pk}", data={"note": str(note.pk)},
        )


class CalendarEventViewSet(viewsets.ModelViewSet):
    """The dashboard calendar: what people planned, and what the projects fix.

    A person sees team events and their own. ``agenda`` adds the dates the
    system already knows - project start and delivery dates, milestones -
    for the month asked for, so one view holds everything that is coming.
    """

    serializer_class = CalendarEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return (
            CalendarEvent.objects.filter(Q(scope=CalendarEvent.Scope.TEAM) | Q(owner=user))
            .select_related("owner", "project")
        )

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def _owner_only(self, event):
        if event.owner_id != self.request.user.id:
            raise PermissionDenied("An event belongs to whoever planned it.")

    def perform_update(self, serializer):
        self._owner_only(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._owner_only(instance)
        instance.delete()

    @action(detail=False, methods=["get"])
    def agenda(self, request):
        """Everything dated in ``?month=YYYY-MM`` (this month by default)."""
        import calendar as _cal
        from datetime import date

        raw = request.query_params.get("month") or ""
        try:
            y, m = (int(p) for p in raw.split("-"))
            first = date(y, m, 1)
        except (TypeError, ValueError):
            today = timezone.localdate()
            first = date(today.year, today.month, 1)
        last = date(first.year, first.month, _cal.monthrange(first.year, first.month)[1])

        items = [
            {**CalendarEventSerializer(e, context={"request": request}).data, "kind": "event"}
            for e in self.get_queryset().filter(date__range=(first, last))
        ]
        # The project dates are read from the projects themselves, so the
        # calendar is never a second copy that drifts.
        if request.user.can("view_projects"):
            from apps.teams.models import Project, ProjectMilestone

            projects = Project.objects.exclude(status__in=["completed", "on_hold"])
            for p in projects.filter(target_date__range=(first, last)):
                items.append({
                    "id": f"project-target:{p.pk}", "kind": "project_target", "title": f"{p.name} - closing date",
                    "date": p.target_date.isoformat(), "time": None, "project": str(p.pk), "project_name": p.name,
                    "notes": "", "mine": False,
                })
            for p in projects.filter(start_date__range=(first, last)):
                items.append({
                    "id": f"project-start:{p.pk}", "kind": "project_start", "title": f"{p.name} - start",
                    "date": p.start_date.isoformat(), "time": None, "project": str(p.pk), "project_name": p.name,
                    "notes": "", "mine": False,
                })
            for ms in ProjectMilestone.objects.filter(
                project__in=projects, due_date__range=(first, last), completed_at__isnull=True
            ).select_related("project"):
                items.append({
                    "id": f"milestone:{ms.pk}", "kind": "milestone", "title": ms.title,
                    "date": ms.due_date.isoformat(), "time": None, "project": str(ms.project_id),
                    "project_name": ms.project.name, "notes": "", "mine": False,
                })
        items.sort(key=lambda i: (i["date"], i.get("time") or "", i["title"]))
        return Response({"month": first.strftime("%Y-%m"), "items": items})
