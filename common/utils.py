import uuid

from django.utils.text import slugify


def generate_asset_code(prefix: str = "DGX", year: int | None = None) -> str:
    """Generate a unique asset code like DGX-2026-00451."""
    from datetime import datetime

    if year is None:
        year = datetime.now().year
    short_uuid = uuid.uuid4().hex[:5].upper()
    return f"{prefix}-{year}-{short_uuid}"


def upload_to_path(instance, filename: str) -> str:
    """Generate a unique upload path for file fields."""
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    model_name = slugify(instance.__class__.__name__)
    return f"uploads/{model_name}/{uuid.uuid4().hex}.{ext}"
