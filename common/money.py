"""Hide money from people who are not supposed to see it.

``view_prices`` is a capability like any other, but it is the one that has
to hold everywhere at once: a cost that leaks through a project's budget is
as exposed as one on a purchase order. So rather than ask every serializer
to remember, this mixin blanks the money fields on the way out, by name.

The server blanking them is what makes the rule real — the screen hiding a
column only makes it tidy.
"""

# Field names that carry an amount, anywhere in the system. A serializer
# opts in with the mixin; the names it does not have are simply not there.
MONEY_FIELDS = frozenset({
    "unit_price", "unit_cost", "line_total", "total_amount", "total_value",
    "purchase_price", "amount", "actual_amount", "subtotal", "tax_amount",
    "grand_total", "budget", "budgeted", "planned_cost", "actual_cost",
    "estimated_cost", "labour_cost", "material_cost", "overhead_cost",
    "paid_amount", "balance", "margin", "selling_price", "last_unit_price",
    "declared_value", "insured_value", "rate", "value",
})


def viewer_sees_prices(context) -> bool:
    """Whether whoever asked for this payload may see money."""
    request = context.get("request") if context else None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    # Anything that is not one of our users (a token, a service) is left as
    # it was rather than silently blanked.
    can = getattr(user, "can", None)
    return can("view_prices") if callable(can) else True


class HidesMoney:
    """Blank the amounts unless the reader is allowed to see them.

    Mix in before ``ModelSerializer``. Nested serializers get the same
    context, so a purchase order's lines are covered by the order's check.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if viewer_sees_prices(self.context):
            return data
        for name in MONEY_FIELDS & set(data):
            data[name] = None
        return data
