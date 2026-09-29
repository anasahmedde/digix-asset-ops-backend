"""Narrowing a queryset to what one person is entitled to see.

A capability answers "may this person open this screen?". Scoping answers
the next question — "and which rows on it?" — which is a different one, and
the one that was missing: an external client viewer could open the client
list and read every client on it.

The rule for a client portal account is deliberately strict. A
`client_viewer` with no client attached sees **nothing**, not everything: an
external login that nobody has pointed at a client is a misconfiguration,
and the safe reading of a misconfiguration is no access.
"""


def client_of(user):
    """The client a portal login belongs to, or None."""
    return getattr(user, "client_id", None)


def is_client_portal(user) -> bool:
    return (
        getattr(user, "role", None) == "client_viewer"
        and not getattr(user, "is_superuser", False)
    )


def for_client(queryset, user, *paths):
    """Narrow to the viewer's own client, by whichever path reaches it.

    `paths` are ORM lookups from this model to a client id, tried in order;
    the first that the model actually has is used. Give more than one where
    a model can reach its client two ways (its own FK, or through a site).
    """
    if not is_client_portal(user):
        return queryset

    mine = client_of(user)
    if not mine:
        return queryset.none()

    from django.core.exceptions import FieldError
    from django.db.models import Q

    condition = Q()
    matched = False
    for path in paths:
        try:
            # Probe the lookup before trusting it — a wrong path would
            # otherwise widen the result rather than narrow it.
            queryset.model._meta.get_field(path.split("__")[0])
        except Exception:
            continue
        try:
            probe = queryset.filter(**{path: mine})
            str(probe.query)
        except (FieldError, ValueError):
            continue
        condition |= Q(**{path: mine})
        matched = True

    if not matched:
        # We could not work out how this model relates to a client, so the
        # honest answer is to show none of it rather than all of it.
        return queryset.none()
    return queryset.filter(condition).distinct()
