from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.models import Alert
from apps.chat.models import ChatMessage
from apps.tickets.models import Ticket

from .models import Notification
from .push import send_push_to_user
from .serializers import NotificationSerializer

logger = logging.getLogger(__name__)

RESOLVED_STATUSES = (Ticket.Status.APPROVED, Ticket.Status.CLOSED)


def _push_ws(notification: Notification):
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        group_name = f"notifications_{notification.recipient_id}"
        try:
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "send_notification",
                    "notification": NotificationSerializer(notification).data,
                },
            )
        except Exception:
            logger.exception("Failed to send WS notification to %s", group_name)

    # Also deliver an OS-level push to the recipient's registered devices.
    try:
        send_push_to_user(
            notification.recipient_id,
            notification.title,
            notification.message,
            {**(notification.data or {}), "notification_type": notification.notification_type},
        )
    except Exception:
        logger.exception("Failed push to %s", notification.recipient_id)


# ── Chat message → push to the other participants ────────────────────

@receiver(post_save, sender=ChatMessage)
def push_chat_message(sender, instance: ChatMessage, created: bool, **kwargs):
    if not created or instance.message_type == ChatMessage.MessageType.SYSTEM:
        return
    try:
        sender_name = instance.sender.get_full_name().strip() or instance.sender.username
        recipient_ids = (
            instance.room.participants.exclude(id=instance.sender_id)
            .values_list("id", flat=True)
        )
        preview = instance.content if instance.message_type == ChatMessage.MessageType.TEXT else "📎 Attachment"
        for uid in recipient_ids:
            send_push_to_user(
                uid,
                sender_name,
                preview[:140],
                {"notification_type": "chat_message", "room_id": str(instance.room_id)},
            )
    except Exception:
        logger.exception("Failed chat push for message %s", instance.pk)


# ── Alert → Notification ─────────────────────────────────────────────

@receiver(post_save, sender=Alert)
def create_notifications_for_alert(sender, instance: Alert, created: bool, **kwargs):
    if not created:
        return

    recipients = User.objects.filter(
        is_active=True,
        role__in=("super_admin", "group_head", "ops_manager"),
    )

    alert_data = {
        "severity": instance.severity,
        "category": instance.category,
        "alert_id": str(instance.id),
    }
    if instance.device_id:
        alert_data["device_id"] = str(instance.device_id)
    if instance.site_id:
        alert_data["site_id"] = str(instance.site_id)

    notifications = Notification.objects.bulk_create(
        [
            Notification(
                recipient=user,
                notification_type=Notification.Type.ALERT,
                title=instance.title,
                message=instance.message,
                alert=instance,
                data=alert_data,
            )
            for user in recipients
        ]
    )

    for notif in notifications:
        _push_ws(notif)


# ── Ticket → Notification ────────────────────────────────────────────

@receiver(pre_save, sender=Ticket)
def capture_ticket_previous_state(sender, instance: Ticket, **kwargs):
    if instance.pk:
        try:
            old = Ticket.objects.get(pk=instance.pk)
            instance._prev_assigned_to_id = old.assigned_to_id
            instance._prev_status = old.status
        except Ticket.DoesNotExist:
            instance._prev_assigned_to_id = None
            instance._prev_status = None
    else:
        instance._prev_assigned_to_id = None
        instance._prev_status = None


@receiver(post_save, sender=Ticket)
def handle_ticket_notifications(sender, instance: Ticket, created: bool, **kwargs):
    prev_assigned = getattr(instance, "_prev_assigned_to_id", None)
    prev_status = getattr(instance, "_prev_status", None)

    if instance.assigned_to_id and (created or prev_assigned != instance.assigned_to_id):
        _create_ticket_assignment_notification(instance)

    # New unassigned ticket: park centrally and route to Operations for assignment.
    if created and not instance.assigned_to_id:
        for ops in User.objects.filter(role__in=("super_admin", "group_head", "ops_manager"), is_active=True):
            notification = Notification.objects.create(
                recipient=ops,
                notification_type=Notification.Type.TICKET_UPDATE,
                title=f"New ticket awaiting assignment: {instance.ticket_number}",
                message=instance.title,
                ticket=instance,
                is_actionable=True,
            )
            _push_ws(notification)

    if not created and prev_status != instance.status:
        if instance.status == Ticket.Status.PENDING_REVIEW:
            _create_review_request_notification(instance)
        elif instance.status == Ticket.Status.APPROVED:
            _create_approval_notification(instance)
        elif instance.status == Ticket.Status.REJECTED:
            _create_rejection_notification(instance)
        elif instance.status in RESOLVED_STATUSES:
            _resolve_ticket_notifications(instance)
        elif instance.assigned_to_id:
            _create_ticket_status_notification(instance)


def _build_ticket_data(ticket: Ticket):
    data = {
        "ticket_id": str(ticket.id),
        "priority": ticket.priority,
        "status": ticket.status,
        "category": ticket.category,
        "ticket_title": ticket.title,
    }
    if ticket.site_id:
        data["site_id"] = str(ticket.site_id)
    if ticket.device_id:
        data["device_id"] = str(ticket.device_id)
    return data


def _create_ticket_assignment_notification(ticket: Ticket):
    title = f"Ticket assigned: {ticket.title}"
    message = (
        f"You have been assigned ticket #{str(ticket.id)[:8]}. "
        f"Priority: {ticket.get_priority_display()}."
    )

    notif = Notification.objects.create(
        recipient_id=ticket.assigned_to_id,
        notification_type=Notification.Type.TICKET_ASSIGNED,
        title=title,
        message=message,
        ticket=ticket,
        is_actionable=True,
        data=_build_ticket_data(ticket),
    )
    _push_ws(notif)


def _create_ticket_status_notification(ticket: Ticket):
    title = f"Ticket updated: {ticket.title}"
    message = f"Status changed to {ticket.get_status_display()}."

    notif = Notification.objects.create(
        recipient_id=ticket.assigned_to_id,
        notification_type=Notification.Type.TICKET_UPDATE,
        title=title,
        message=message,
        ticket=ticket,
        data=_build_ticket_data(ticket),
    )
    _push_ws(notif)


def _create_review_request_notification(ticket: Ticket):
    """Notify the reporter / supervisors that work is submitted for review."""
    recipients = []
    if ticket.reported_by_id:
        recipients.append(ticket.reported_by_id)

    admins = User.objects.filter(
        is_active=True,
        role__in=("super_admin", "group_head", "ops_manager"),
    ).exclude(id__in=recipients).values_list("id", flat=True)
    recipients.extend(admins)

    data = _build_ticket_data(ticket)
    data["submitted_by"] = str(ticket.completed_by_id) if ticket.completed_by_id else None

    for uid in recipients:
        notif = Notification.objects.create(
            recipient_id=uid,
            notification_type=Notification.Type.TICKET_REVIEW,
            title=f"Review requested: {ticket.title}",
            message=f"Ticket #{str(ticket.id)[:8]} has been submitted for your review.",
            ticket=ticket,
            is_actionable=True,
            data=data,
        )
        _push_ws(notif)


def _create_approval_notification(ticket: Ticket):
    """Notify the assignee that their work was approved."""
    if not ticket.assigned_to_id:
        return

    notif = Notification.objects.create(
        recipient_id=ticket.assigned_to_id,
        notification_type=Notification.Type.TICKET_UPDATE,
        title=f"Ticket approved: {ticket.title}",
        message=f"Your work on ticket #{str(ticket.id)[:8]} has been approved.",
        ticket=ticket,
        data=_build_ticket_data(ticket),
    )
    _push_ws(notif)


def _create_rejection_notification(ticket: Ticket):
    """Notify the assignee that their work was rejected."""
    if not ticket.assigned_to_id:
        return

    comments = ticket.review_comments or "No comments provided."
    notif = Notification.objects.create(
        recipient_id=ticket.assigned_to_id,
        notification_type=Notification.Type.TICKET_UPDATE,
        title=f"Ticket rejected: {ticket.title}",
        message=f"Your submission was rejected. Feedback: {comments}",
        ticket=ticket,
        is_actionable=True,
        data=_build_ticket_data(ticket),
    )
    _push_ws(notif)


def _resolve_ticket_notifications(ticket: Ticket):
    now = timezone.now()

    actionable = Notification.objects.filter(
        ticket=ticket,
        is_actionable=True,
        resolved_at__isnull=True,
    )
    actionable.update(resolved_at=now)

    if ticket.assigned_to_id:
        notif = Notification.objects.create(
            recipient_id=ticket.assigned_to_id,
            notification_type=Notification.Type.TICKET_UPDATE,
            title=f"Ticket closed: {ticket.title}",
            message=f"Status: {ticket.get_status_display()}.",
            ticket=ticket,
            data=_build_ticket_data(ticket),
        )
        _push_ws(notif)
