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
    # Found leaking during the organogram walkthrough: a repair's cost
    # reached technicians through the maintenance record and the ticket,
    # and an asset's installation figures through the device payload.
    "cost", "repair_cost", "planned_installation_cost",
    "actual_installation_cost", "approved_total", "variance",
    "contingency", "quoted_price", "price",
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


def scrub(data, context=None, *, also=()):
    """Blank every amount in an already-built payload.

    For the code that never passes through a serializer — a hand-rolled
    APIView, a dict assembled by hand, a nested summary. Walks lists and
    dicts to any depth, so a total buried three levels down is caught too.

    `also` names fields that mean money *in this payload*. Some names are
    ambiguous — `total` is a cost on a plan and a row count on a list — so
    the caller says which it is rather than the catalogue guessing.
    """
    if viewer_sees_prices(context):
        return data
    return _blank(data, MONEY_FIELDS | frozenset(also))


def _blank(node, names):
    if isinstance(node, dict):
        return {
            k: (None if k in names else _blank(v, names))
            for k, v in node.items()
        }
    if isinstance(node, (list, tuple)):
        return [_blank(v, names) for v in node]
    return node


# What a cost document calls its bottom line.
COSTING_TOTALS = ("total", "subtotal", "grand_total", "sum", "overall")


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
