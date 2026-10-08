"""Dates that only make sense in order.

A due date before the job was raised, an end before its start, a handover
that has not happened yet: each screen let these through on its own, and the
register then reported tickets overdue the day they were opened. The rules
live here once, and every serializer that carries such dates declares them.
"""

from datetime import date, datetime

from django.utils import timezone
from rest_framework import serializers


def _day(value):
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value if isinstance(value, date) else None


def refuse_past(value, field):
    """A deadline read straight off a request: today or later, or refused."""
    from rest_framework.fields import DateField

    if value in (None, ""):
        return None
    day = value if isinstance(value, date) else DateField().to_internal_value(value)
    if day < timezone.localdate():
        raise serializers.ValidationError({field: ["That date has passed — pick today or later."]})
    return day


class DateOrder:
    """Mixed into a serializer ahead of ``ModelSerializer``.

    - ``date_order``: ``(later, earlier, "the start date")`` — ``later`` cannot
      fall before ``earlier``, which may read through a relation
      (``"device.installation_date"``).
    - ``future_dates``: deadlines; a new or changed one cannot be in the past.
      An old one left as it is still saves, so editing something else on a
      late job is not refused.
    - ``past_dates``: things that have happened; they cannot be in the future.
    """

    date_order: tuple = ()
    future_dates: tuple = ()
    past_dates: tuple = ()

    def to_internal_value(self, data):
        attrs = super().to_internal_value(data)
        instance = getattr(self, "instance", None)

        def current(path):
            # "device.installation_date" reads through the related record.
            head, *rest = path.split(".")
            value = attrs[head] if head in attrs else getattr(instance, head, None)
            for part in rest:
                value = getattr(value, part, None)
            return value

        today = timezone.localdate()
        errors = {}
        def changed(path):
            head = path.split(".")[0]
            return head in attrs and attrs[head] != getattr(instance, head, None)

        for later, earlier, label in self.date_order:
            # Only what is being changed is held to the rule: an order whose
            # approval stamped a date after its delivery can still have its
            # notes edited.
            if not (changed(later) or changed(earlier)):
                continue
            a, b = _day(current(later)), _day(current(earlier))
            if a and b and a < b:
                errors[later] = f"Cannot be before {label} ({b:%b %d, %Y})."
        for field in self.future_dates:
            new = _day(attrs.get(field))
            if new and new < today and new != _day(getattr(instance, field, None)):
                errors.setdefault(field, "That date has passed — pick today or later.")
        for field in self.past_dates:
            new = _day(attrs.get(field))
            if new and new > today:
                errors.setdefault(field, "That has not happened yet — it cannot be a future date.")
        if errors:
            raise serializers.ValidationError(errors)
        return attrs
