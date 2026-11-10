"""Interface helpers for the staff and client templates.

`{% pill value %}` writes a status as a word in a coloured pill. The colour is the money state the
word belongs to (waiting, seen, paid, part-paid, needs a person, late, void, refunded); the word is
always there, so the colour never carries the meaning alone.
"""
from django import template
from django.utils.html import format_html

register = template.Library()

TONES = {
    # invoices
    "draft": "draft", "sent": "waiting", "overdue": "late", "declared": "seen", "partially_paid": "partial",
    "paid": "paid", "overpaid": "attention", "void": "void",
    # payments
    "detected": "seen", "confirmed": "paid", "rejected": "void", "needs_review": "attention", "reversed": "refunded",
    # declarations
    "open": "attention", "matched": "paid", "dismissed": "void",
    # encryption keys
    "active": "paid", "pending": "waiting", "failed": "late", "revoked": "void", "replaced": "void",
    "expired": "void", "cancelled": "void", "withdrawn": "void",
    # e-mail and webhook deliveries
    "queued": "waiting",
}


@register.simple_tag
def pill(value, label=None):
    value = str(value or "")
    tone = TONES.get(value, "waiting")
    return format_html('<span class="pill pill--{}">{}</span>', tone, label or value.replace("_", " "))


@register.simple_tag
def invoice_ref(number, pk):
    """An invoice as staff name it: its number, or "Draft #<pk>" before it has one."""
    return number or f"Draft #{pk}"


@register.filter
def get(mapping, key):
    return (mapping or {}).get(key, "")


# The audit trail in words. The code stays in a title attribute, to search the log by.
AUDIT_WORDS = {
    "contact.create": "Contact created", "contact.update": "Contact updated",
    "declaration.create": "Client said they paid", "declaration.dismiss": "Declaration dismissed",
    "declaration.matched": "Declaration matched a payment", "fx_leg.accept": "Exchange rate accepted",
    "invoice.create": "Draft created", "invoice.update": "Draft edited", "invoice.delete": "Draft deleted",
    "invoice.send": "Sent", "invoice.overdue": "Became overdue", "invoice.void": "Voided",
    "invoice.reissue_links": "Client links reissued", "invoice.reminder": "Payment reminder queued",
    "payment.record": "Payment recorded by hand", "payment.detect": "Payment seen", "payment.detected": "Payment seen",
    "payment.await_staff": "Payment waits for staff", "payment.review": "Payment sent to review",
    "payment.confirm": "Payment confirmed", "payment.reject": "Payment rejected", "payment.reverse": "Payment reversed",
    "payment.assign": "Payment assigned", "payment.refund_ack": "Refund marked as sent", "invoice.overpayment_keep": "Overpayment kept", "payment.final": "Payment final",
    "payment.reattribute": "Payment reattributed", "payment.gateway_invalid": "Gateway called the payment invalid",
    "payment.gateway_invalid_ack": "Kept after the gateway called it invalid",
    "payment.gateway_ignored": "Gateway update ignored",
    "quote.create": "Quote issued", "quote.close": "Quote closed", "quote.paid": "Quote paid",
    "outbox.retry": "E-mail retried", "outbox.dismiss": "E-mail dismissed", "outbox.failed": "E-mail failed",
    "outbox.cancelled": "E-mail cancelled",
    "link.create": "Payment link created", "link.update": "Payment link edited", "link.rotate": "Link URL replaced",
    "link.image": "Image set", "link.image_removed": "Image removed", "coupon.create": "Coupon created",
    "coupon.activate": "Coupon turned on", "coupon.deactivate": "Coupon turned off",
    "link_order.paid": "Order paid", "link_order.fulfil": "Order marked fulfilled",
    "link_alert.raise": "Raised for a person", "link_alert.resolve": "Resolved by a person",
}
MODEL_WORDS = {"invoices.invoice": "Invoice", "payments.payment": "Payment", "payments.declaration": "Declaration",
               "quotes.quote": "Quote", "contacts.contact": "Contact", "links.paymentlink": "Payment link",
               "links.coupon": "Coupon", "links.linkorder": "Order", "links.linkalert": "Item"}


@register.filter
def audit_words(action):
    return AUDIT_WORDS.get(action, action)


@register.filter
def model_words(label):
    return MODEL_WORDS.get(label, label)


# Networks and rails as staff read them, in the names clients see ("Arbitrum One", "Monero").
RAIL_WORDS = {"bank": "Bank transfer", "fiat_link": "Payment link", "manual": "Recorded by hand", "btcpay": "BTCPay",
              "lightning": "Lightning", "evm": "EVM chains", "tron": "Tron", "solana": "Solana", "ton": "TON"}


@register.filter
def network_words(network, rail=""):
    """A payment's or a declaration's network by name; its rail's name when it has no network."""
    if network == "other":
        return "Other"
    if network:
        from apps.portal.crypto import network_name

        return network_name(network)
    return RAIL_WORDS.get(rail, rail) or "-"
