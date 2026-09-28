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


def ensure_seeded():
    """Make sure every built-in role exists as a record.

    Called lazily rather than in a migration, so the code catalogue stays
    the source of truth for a fresh database and nothing has to be
    back-filled by hand when a capability is added.
    """
    from .capabilities import ROLE_DEFAULTS
    from .models import RoleDefinition, User

    labels = dict(User.Role.choices)
    existing = set(RoleDefinition.objects.values_list("key", flat=True))
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
    cache.set(CACHE_KEY, merged, 300)
    return merged


def forget():
    """Drop the cache — called whenever a role's capabilities change."""
    cache.delete(CACHE_KEY)


def defaults_for(role: str) -> frozenset[str]:
    return capability_map().get(role, frozenset())
