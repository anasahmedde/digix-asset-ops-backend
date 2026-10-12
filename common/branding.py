"""The letterhead every printed document carries.

DIGIX on the left, Adsells on the right - the two names the client's
paperwork goes out under. The logos live next to this file so a build has
them wherever it runs; a document only needs the one table.
"""
from pathlib import Path

from reportlab.lib.units import mm
from reportlab.platypus import Image, Table, TableStyle

HERE = Path(__file__).resolve().parent / "branding"
DIGIX = HERE / "digix-dark.png"
ADSELLS = HERE / "adsells.png"


def _logo(path: Path, height: float) -> Image:
    """A logo scaled to a height, keeping its proportions."""
    image = Image(str(path))
    ratio = height / float(image.imageHeight)
    image.drawHeight = height
    image.drawWidth = float(image.imageWidth) * ratio
    return image


def letterhead(width: float = 174 * mm, height: float = 9 * mm) -> Table:
    """One row: DIGIX left, Adsells right, spanning ``width``."""
    digix = _logo(DIGIX, height)
    adsells = _logo(ADSELLS, height * 1.6)
    table = Table([[digix, adsells]], colWidths=[width / 2, width / 2], rowHeights=[height * 1.6 + 2 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table
