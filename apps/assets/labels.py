"""QR / barcode label rendering for device asset codes.

The generated image encodes the device's ``asset_code`` — the same value the
mobile scanner resolves through ``/assets/devices/?search=<code>``.
"""

import io

from django.core.files.base import ContentFile


def render_label(device, fmt: str = "qr") -> ContentFile:
    """Render a printable label for ``device`` and return it as a ContentFile."""
    code = device.asset_code
    if fmt == "code128":
        payload = _render_code128(code)
    else:
        payload = _render_qr(code)
    return ContentFile(payload, name=f"{code}-{fmt}.png")


def _render_qr(code: str) -> bytes:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(code)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Human-readable asset code strip under the QR.
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    measure = ImageDraw.Draw(img)
    bbox = measure.textbbox((0, 0), code, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    canvas = Image.new("RGB", (max(img.width, text_w + 24), img.height + text_h + 20), "white")
    canvas.paste(img, ((canvas.width - img.width) // 2, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(((canvas.width - text_w) // 2, img.height + 2), code, fill="black", font=font)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _render_code128(code: str) -> bytes:
    from barcode import Code128
    from barcode.writer import ImageWriter

    buf = io.BytesIO()
    # ImageWriter draws the human-readable text under the bars by default.
    Code128(code, writer=ImageWriter()).write(buf)
    return buf.getvalue()
