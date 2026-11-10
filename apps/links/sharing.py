"""What staff hand out of a payment link: its QR code (PNG, SVG), a printable sheet, a button snippet.

The URL is the link's own (`PaymentLink.url_for`); nothing here stores or logs it. The files carry
the modules of the QR and no text, so the SVG and the PNG say nothing a scanner would not.
"""
import base64
import html
import io

import qrcode
from django.template.loader import render_to_string
from qrcode.image.svg import SvgPathImage

QR_BORDER = 4  # modules; the quiet zone the QR standard asks for
_LEVEL = qrcode.constants.ERROR_CORRECT_M


def qr_png(url: str, *, box_size: int = 10) -> bytes:
    qr = qrcode.QRCode(error_correction=_LEVEL, box_size=box_size, border=QR_BORDER)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def qr_svg(url: str) -> bytes:
    img = qrcode.make(url, image_factory=SvgPathImage, border=QR_BORDER, error_correction=_LEVEL)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def button_snippet(url: str, title: str) -> str:
    """An anchor that opens the link in a new tab. The title is text somebody typed: it is escaped."""
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
            f'{html.escape(title, quote=True)}</a>')


def filename(link, extension: str) -> str:
    return f"payment-link-{int(link.pk)}.{extension}"


def sheet_pdf(title: str, url: str) -> bytes:
    """One A4 page: the title, a large QR and the URL, to print and put up."""
    from weasyprint import HTML

    from apps.invoices.pdf import STATIC_PDF_DIR, SafeFetcher

    page = render_to_string("pdf/link_sheet.html", {"title": title, "url": url,
                                                    "qr": png_data_uri(qr_png(url, box_size=20))})
    return HTML(string=page, base_url=str(STATIC_PDF_DIR) + "/", url_fetcher=SafeFetcher()).write_pdf()
