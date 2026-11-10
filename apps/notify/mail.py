"""Context builders for the e-mails the invoicing code sends."""
from django.utils import translation
from django.utils.formats import date_format

from apps.invoices.money import totals
from apps.invoices.pdf import format_money


def ref_of(invoice) -> str:
    return invoice.number or f"#{invoice.pk}"


def org_name(org) -> str:
    return org.legal_name or org.slug


def contact_name(contact) -> str:
    return contact.display_name or contact.legal_name or ""


def invoice_context(invoice, language, *, view_url, pay_url) -> dict:
    from apps.links.services import buyer_name

    typed = buyer_name(invoice)  # a payment link's order: the name typed at checkout, never the stored one
    with translation.override(language):
        due = date_format(invoice.due_date, "DATE_FORMAT") if invoice.due_date else ""
        return {
            "ref": ref_of(invoice), "org": org_name(invoice.org),
            "name": typed if typed is not None else contact_name(invoice.contact),
            "is_request": invoice.kind == "payment_request", "message": invoice.notes or "",
            "total": format_money(totals(invoice).total, invoice.currency), "due": due,
            "view_url": view_url, "pay_url": pay_url,
        }


# Builders for messages addressed at flush (OutboxMessage.recipient set). Each takes the
# message, with its invoice and contact loaded, and returns the body context: the plain
# context plus names and money formatted in the active language (the letter's language).

def _link_order(msg) -> dict | None:
    """A payment link's order: the buyer's name as typed and the links it keeps (`apps.links`)."""
    from apps.links.services import letter_details

    return letter_details(msg.invoice) if msg.invoice_id else None


def receipt_context(msg) -> dict:
    plain = dict(msg.plain_context or {})
    invoice = msg.invoice
    # The receipt carries no link (the capability tokens are stored only as hashes), unless it is a
    # payment link's order, which keeps them encrypted for its letters.
    order = _link_order(msg)
    return {
        **plain, "ref": ref_of(invoice), "org": org_name(invoice.org),
        "name": order["name"] if order else contact_name(msg.contact),
        "total": format_money(plain["paid_minor"], plain["currency"]), "view_url": order["view_url"] if order else "",
    }


def partial_context(msg) -> dict:
    plain = dict(msg.plain_context or {})
    invoice = msg.invoice
    paid, total, currency = plain["paid_minor"], plain["total_minor"], plain["currency"]
    # No pay link either (the letter points the client back to the one in the original e-mail), unless
    # it is a payment link's order: nobody mailed it that link, so this letter carries it.
    order = _link_order(msg)
    return {
        **plain, "ref": ref_of(invoice), "org": org_name(invoice.org),
        "name": order["name"] if order else contact_name(msg.contact),
        "paid": format_money(paid, currency), "remaining": format_money(max(0, total - paid), currency),
        "pay_url": order["pay_url"] if order else "",
    }


def order_paid_staff_context(msg) -> dict:
    """The staff letter for a paid order of a payment link that asks for one: the order's number and the
    link's staff page only. Staff letters go in plaintext, so they never name the buyer or the amount."""
    from django.conf import settings
    from django.urls import reverse

    plain = dict(msg.plain_context or {})
    invoice = msg.invoice if msg.invoice_id else None
    return {
        **plain, "ref": ref_of(invoice) if invoice is not None else f"#{plain.get('invoice_id', '')}",
        "link_url": settings.SITE_URL.rstrip("/") + reverse("staff:link_detail", args=[plain["link_id"]]),
        "invoice_url": settings.SITE_URL.rstrip("/") + reverse("staff:invoice", args=[plain["invoice_id"]]),
    }


# Staff letters about crypto payments, queued by the matching engine (possibly while sealed)
# with ids, codes and base-unit amounts only. They are written in English, for staff.

GATEWAY_NETWORK_NAMES = {"bitcoin": "Bitcoin", "lightning": "Lightning", "monero": "Monero", "zcash": "Zcash"}


def _network_name(key: str) -> str:
    from apps.rails.networks import NETWORKS

    net = NETWORKS.get(key)
    return net.name if net else GATEWAY_NETWORK_NAMES.get(key, key)


def _crypto_amount(plain: dict) -> str:
    """The amount in whole units of its asset: exact from base units where the asset is on the
    allow-list, else what the gateway reported, else the bare base units of an unlisted token."""
    from apps.rails.networks import NETWORKS
    from apps.rails.uris import decimal_amount

    net = NETWORKS.get(plain.get("network", ""))
    spec = net.assets.get(plain.get("asset", "")) if net else None
    digits = plain.get("amount_base_units") or ""
    if spec is not None and digits:
        return f"{decimal_amount(int(digits), spec.decimals)} {spec.symbol}"
    if plain.get("received_amount"):
        return f"{plain['received_amount']} {plain.get('asset', '')}".strip()
    if digits:
        return f"{digits} base units of an unlisted token"
    return ""


def _payment_letter(msg) -> dict:
    from django.conf import settings
    from django.urls import reverse

    plain = dict(msg.plain_context or {})
    invoice = msg.invoice if msg.invoice_id else None
    return {
        **plain, "ref": str(plain.get("payment_id", "")), "network_name": _network_name(plain.get("network", "")),
        "amount": _crypto_amount(plain), "invoice_ref": ref_of(invoice) if invoice is not None else "",
        "admin_url": settings.SITE_URL.rstrip("/") + reverse("staff:needs_person"),
    }


def review_staff_context(msg) -> dict:
    from apps.payments.models import REVIEW_REASONS

    context = _payment_letter(msg)
    reason = context.get("reason", "")
    context["reason_text"] = str(REVIEW_REASONS.get(reason, reason))
    return context


def confirm_staff_context(msg) -> dict:
    return _payment_letter(msg)
