"""Telling people a price has outrun the plan.

A line priced over its reference stops the order until the side that owns
the figure agrees to it. Nobody watches a queue they were never pointed at,
so both halves of that conversation are pushed: out to the owner when the
order is raised, and back to the buyer when the answer comes.
"""

from apps.notifications.models import Notification


def _push(notification):
    """Send it down the socket as well, so it lands while they are looking."""
    from apps.notifications.signals import _push_ws

    try:
        _push_ws(notification)
    except Exception:  # noqa: BLE001 — a dropped socket must not fail the order
        pass


def _money(value, currency):
    return f"{currency} {value:,.0f}" if value is not None else "no figure"


def tell_the_owner(items, raised_by=None):
    """Point the owning side at lines they have to agree before the order moves.

    ``items`` are the flagged lines of one order. Grouped by owner so a buyer
    adding six lines does not post six notifications to the same desk.
    """
    from django.contrib.auth import get_user_model

    from .views import PurchaseOrderViewSet  # the one list of who may decide

    by_owner = {}
    for item in items:
        by_owner.setdefault(item.variance_owner, []).append(item)

    User = get_user_model()
    sent = []
    for owner, lines in by_owner.items():
        roles = PurchaseOrderViewSet.VARIANCE_DECIDERS.get(owner, ())
        if not roles:
            continue
        order = lines[0].parent_order
        who = User.objects.filter(is_active=True, role__in=roles).exclude(
            pk=getattr(raised_by, "pk", None)
        )
        first = lines[0]
        detail = (
            f"{first.description} at {_money(first.unit_price, order.currency)} "
            f"against {_money(first.reference_unit_price, order.currency)}"
            + (f", and {len(lines) - 1} more" if len(lines) > 1 else "")
        )
        for user in who:
            note = Notification.objects.create(
                recipient=user,
                notification_type=Notification.Type.SYSTEM,
                title=f"{lines[0].order_number} is priced over plan",
                message=(
                    f"{len(lines)} line(s) on {lines[0].order_number} cost more than was planned: "
                    f"{detail}. The order cannot go up for signature until you agree them."
                ),
                data={
                    "order": str(order.pk),
                    "order_number": lines[0].order_number,
                    "variance_owner": owner,
                    "items": [str(line.pk) for line in lines],
                },
            )
            _push(note)
            sent.append(note)
    return sent


def tell_the_buyer(item, decider, approved):
    """Send the answer back to whoever raised the order."""
    order = item.parent_order
    buyer = item.order_raised_by
    if buyer is None or buyer.pk == getattr(decider, "pk", None):
        return None
    note = Notification.objects.create(
        recipient=buyer,
        notification_type=Notification.Type.SYSTEM,
        title=(
            f"Price agreed on {item.order_number}" if approved
            else f"Price refused on {item.order_number}"
        ),
        message=(
            f"{decider.get_full_name() or decider.username} "
            + ("agreed" if approved else "refused")
            + f" {_money(item.unit_price, order.currency)} for '{item.description}'"
            + (f" — {item.variance_notes}" if item.variance_notes else ".")
        ),
        data={
            "order": str(order.pk),
            "order_number": item.order_number,
            "item": str(item.pk),
            "approved": approved,
        },
    )
    _push(note)
    return note
