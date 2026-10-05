"""Coordinates, at the precision the column actually holds.

A map gives back a float with every digit it has — 24.858363984674 — and the
column keeps seven decimal places, which is about a centimetre. DRF counts
the digits it is handed before it rounds anything, so a perfectly ordinary
pin dropped on the map came back as "Ensure that there are no more than 10
digits in total". Rounding on the way in is the fix: nobody is asking for
more precision than a centimetre, and nobody typed those digits.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from rest_framework import serializers

#: What the latitude/longitude columns hold: max_digits=10, decimal_places=7.
PLACES = Decimal("0.0000001")


class Coordinate(serializers.DecimalField):
    """A latitude or longitude, rounded to what the column holds."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 10)
        kwargs.setdefault("decimal_places", 7)
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if data in (None, ""):
            return super().to_internal_value(data)
        try:
            data = Decimal(str(data)).quantize(PLACES, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            # Not a number at all — let the usual validation say so.
            pass
        return super().to_internal_value(data)
