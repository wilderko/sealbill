"""QR codes as inline SVG `data:` images (the CSP allows `img-src data:`, nothing external)."""
import base64
import io

import qrcode
from qrcode.image.svg import SvgPathImage


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text, image_factory=SvgPathImage, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
