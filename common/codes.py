"""
Central, transaction-safe code/number generation.

Every human-readable identifier in the platform (asset codes, supplier codes,
PO numbers, invoice numbers, work-order numbers, …) is produced here so the
format is consistent and configurable from the Setup screens via
``apps.setup.models.NumberingScheme``.

Usage::

    from common.codes import generate_code, Entity
    supplier.code = generate_code(Entity.SUPPLIER, model=Supplier, field="code")

If no active :class:`NumberingScheme` exists for the entity, a safe UUID-based
fallback code is returned so object creation never fails.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import transaction

# Re-export the entity choices so callers don't need to import the setup app.
try:  # pragma: no cover - import-time convenience only
    from apps.setup.models import NumberingScheme

    Entity = NumberingScheme.Entity
except Exception:  # pragma: no cover
    NumberingScheme = None
    Entity = None


# Fallback prefixes used only when a scheme has not been configured yet.
_FALLBACK_PREFIXES = {
    "asset": "DGX",
    "supplier": "SUP",
    "client": "CLI",
    "purchase_order": "PO",
    "quotation": "QT",
    "invoice": "INV",
    "work_order": "WO",
    "project": "PRJ",
    "ticket": "TKT",
    "goods_receipt": "GRN",
    "issuance": "ISS",
    "inventory_item": "ITM",
}


def _fallback_code(entity: str) -> str:
    prefix = _FALLBACK_PREFIXES.get(entity, "GEN")
    return f"{prefix}-{datetime.now().year}-{uuid.uuid4().hex[:5].upper()}"


def generate_code(entity, *, model=None, field: str = "code", max_tries: int = 50) -> str:
    """
    Return the next code for ``entity``.

    ``entity`` may be a ``NumberingScheme.Entity`` value or its string.
    When ``model`` is given, the generated code is checked for uniqueness
    against ``model.<field>`` and regenerated on collision (protects against
    clashes with legacy, manually-entered codes).
    """
    entity = str(getattr(entity, "value", entity))

    # Import lazily to avoid app-loading / circular-import issues.
    from apps.setup.models import NumberingScheme as _Scheme

    for _ in range(max_tries):
        with transaction.atomic():
            scheme = (
                _Scheme.objects.select_for_update()
                .filter(entity=entity, is_active=True)
                .first()
            )
            if scheme is None:
                code = _fallback_code(entity)
            else:
                code = scheme.build(scheme.next_number)
                scheme.next_number += 1
                scheme.save(update_fields=["next_number", "updated_at"])

        if model is None or not model._default_manager.filter(**{field: code}).exists():
            return code

    # Extremely unlikely: fall back to a guaranteed-unique value.
    return _fallback_code(entity)
