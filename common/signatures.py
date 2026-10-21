"""A person's signature on the documents the app prints.

Uploaded once on the person (Teams, or their own profile) and placed on the
signing line above their name wherever a document records them - the PO's
Approved by, the GRN's Received by, the issue slip's Issued by, and so on.
No signature on file leaves the line blank to sign by hand.
"""
from __future__ import annotations

import io

from reportlab.lib.units import mm
from reportlab.platypus import Image, Spacer

LINE_HEIGHT = 14 * mm


def signature_of(user, *, width=40 * mm, height=LINE_HEIGHT):
    """The person's signature to sit on a signing line, or an empty space."""
    picture = getattr(user, "signature", None) if user is not None else None
    if not picture:
        return Spacer(1, 1)
    try:
        with picture.open("rb") as f:
            data = io.BytesIO(f.read())
        img = Image(data, width=width, height=height, kind="proportional")
        img.hAlign = "LEFT"
        return img
    except Exception:  # noqa: BLE001 - a missing or broken file never stops the document
        return Spacer(1, 1)


def person_named(name):
    """The one active team member with this full name (or username), if any -
    for a document that recorded the person as typed text."""
    from apps.accounts.models import User

    name = (name or "").strip()
    if not name:
        return None
    matches = [u for u in User.objects.filter(is_active=True)
               if (u.get_full_name().strip() or u.username) == name or u.username == name]
    return matches[0] if len(matches) == 1 else None
