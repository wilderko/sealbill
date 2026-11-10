"""Invoice and receipt PDFs (WeasyPrint)."""
import base64
import mimetypes
import os
from decimal import Decimal

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.formats import number_format

from apps.invoices import snapshot
from apps.invoices.currencies import minor_units
from apps.invoices.money import line_total, totals
from apps.invoices.reference import payment_reference

STATIC_PDF_DIR = settings.BASE_DIR / "static" / "pdf"


def format_money(minor: int, currency: str) -> str:
    digits = minor_units(currency)
    value = Decimal(minor).scaleb(-digits)
    return f"{number_format(value, decimal_pos=digits, use_l10n=True, force_grouping=True)} {currency}"


def _make_fetcher_class():
    from weasyprint.urls import URLFetcher

    class SafeFetcher(URLFetcher):
        """Only data: URIs and files under the PDF static dir; no network, no other paths."""

        def fetch(self, url, headers=None):
            from urllib.parse import unquote, urlparse

            if not url.startswith("data:"):
                parsed = urlparse(url)
                real = os.path.realpath(unquote(parsed.path)) if parsed.scheme == "file" else None
                root = os.path.realpath(STATIC_PDF_DIR)
                if real is None or not (real == root or real.startswith(root + os.sep)):
                    raise ValueError(f"refusing to fetch {url!r} for a PDF")
            return super().fetch(url, headers)

    return SafeFetcher


def SafeFetcher(*args, **kwargs):
    """A WeasyPrint URL fetcher restricted to data: URIs and the PDF static dir."""
    return _make_fetcher_class()(*args, allowed_protocols=["data", "file"], **kwargs)


_LOGO_TYPES = {"image/png", "image/jpeg"}


def _logo_uri(org):
    if not org.logo:
        return None
    if mimetypes.guess_type(org.logo.name)[0] not in _LOGO_TYPES:
        return None
    try:
        with org.logo.open("rb") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    else:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _context(invoice, language):
    """The template context. The seller, the buyer and the payment details come from
    `snapshot.for_render`: the live rows for a draft, as frozen at send for anything else."""
    cur = invoice.currency
    t = totals(invoice)
    frozen = snapshot.for_render(invoice)
    lines = []
    for item in invoice.lines.all():
        q = Decimal(item.quantity).normalize()
        lines.append({
            "name": item.name, "description": item.description,
            "quantity": number_format(q, use_l10n=True),
            "unit_price": format_money(item.unit_price_minor, cur),
            "tax_rate": f"{number_format(Decimal(item.tax_rate_bps) / 100, use_l10n=True)} %",
            "total": format_money(line_total(item), cur),
        })
    return {
        "language": language, "invoice": invoice, "lines": lines,
        "doc_title": invoice.number or str(invoice),
        "seller": dict(frozen["seller"]),
        "buyer": dict(frozen["buyer"]),
        "logo_uri": _logo_uri(invoice.org),
        "money": {"subtotal": format_money(t.subtotal, cur), "discount": format_money(t.discount, cur),
                  "has_discount": bool(t.discount), "tax": format_money(t.tax, cur),
                  "total": format_money(t.total, cur)},
        "reference": payment_reference(invoice),
        "methods": snapshot.methods_in(frozen, language),
    }


def _render(template, invoice, language) -> bytes:
    from weasyprint import HTML

    with translation.override(language):
        html = render_to_string(template, _context(invoice, language))
    return HTML(string=html, base_url=str(STATIC_PDF_DIR) + "/", url_fetcher=SafeFetcher()).write_pdf()


def render_invoice_pdf(invoice, language: str) -> bytes:
    return _render("pdf/invoice.html", invoice, language)


def render_receipt_pdf(invoice, language: str) -> bytes:
    return _render("pdf/receipt.html", invoice, language)
