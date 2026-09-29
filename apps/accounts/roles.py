"""Roles as records, so they can be changed without a deployment.

The catalogue in ``capabilities.py`` is the vocabulary — the list of things
a person can be allowed to do. This module makes the *roles* editable: the
eleven built-in ones start from the defaults in code, and any of them can
be adjusted, or a new role written from scratch, by whoever holds
``manage_permissions``.

A built-in role can be edited but never deleted: accounts hold its key, and
a role that vanishes would take their rights with it. A custom role can be
deleted, but only once nobody holds it.
"""

from django.core.cache import cache

CACHE_KEY = "role-capabilities-v1"
# Django's default cache is per-process, so `forget()` clears the web
# worker that served the change and nobody else — a management command, a
# second worker or celery keeps the stale map until it expires. A minute is
# short enough that a permission change is never long in arriving, and long
# enough that "what may this person do" is not a query on every request.
# With a shared cache configured (Redis), invalidation is immediate and
# this is only a backstop.
CACHE_SECONDS = 60


def ensure_seeded():
    """Make sure every built-in role exists as a record.

    Called lazily rather than in a migration, so the code catalogue stays
    the source of truth for a fresh database and nothing has to be
    back-filled by hand when a capability is added.
    """
    from .capabilities import ROLE_DEFAULTS
    from .models import RoleDefinition, User

    labels = dict(User.Role.choices)
    rows = list(RoleDefinition.objects.all())
    existing = {r.key for r in rows}
    missing = [
        RoleDefinition(
            key=key,
            label=labels.get(key, key.replace("_", " ").title()),
            capabilities=sorted(caps),
            is_builtin=True,
        )
        for key, caps in ROLE_DEFAULTS.items()
        if key not in existing
    ]
    if missing:
        RoleDefinition.objects.bulk_create(missing)
        forget()
        rows += missing

    # A capability added to the catalogue after the roles were seeded would
    # otherwise reach nobody: the records hold the list as it was. Anything
    # no role has heard of yet is new, so it is granted to the built-in
    # roles the code says should have it. Only ever added, never removed,
    # and a role somebody has edited keeps every choice they made.
    known = set().union(*(set(r.capabilities or ()) for r in rows)) if rows else set()
    fresh = {k for caps in ROLE_DEFAULTS.values() for k in caps} - known
    if fresh:
        for row in rows:
            if not row.is_builtin:
                continue
            should = ROLE_DEFAULTS.get(row.key, frozenset()) & fresh
            if should:
                row.capabilities = sorted(set(row.capabilities or ()) | should)
                row.save(update_fields=["capabilities", "updated_at"])
        forget()


def capability_map() -> dict[str, frozenset[str]]:
    """Every role's capabilities, keyed by role, read once per process tick."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    from .capabilities import ROLE_DEFAULTS
    from .models import RoleDefinition

    rows = dict(RoleDefinition.objects.values_list("key", "capabilities"))
    # The code defaults are the floor: a database that has not been seeded
    # yet still behaves, and a role added in code appears without a step.
    merged = {key: frozenset(caps) for key, caps in ROLE_DEFAULTS.items()}
    merged.update({key: frozenset(caps or ()) for key, caps in rows.items()})
    cache.set(CACHE_KEY, merged, CACHE_SECONDS)
    return merged


def forget():
    """Drop the cache — called whenever a role's capabilities change."""
    cache.delete(CACHE_KEY)


def defaults_for(role: str) -> frozenset[str]:
    return capability_map().get(role, frozenset())
