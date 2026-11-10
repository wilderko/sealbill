from collections import namedtuple
from decimal import ROUND_HALF_UP, Decimal

Totals = namedtuple("Totals", "subtotal discount tax total")

_ONE = Decimal(1)
_BPS = Decimal(10000)


def _r(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


def _parts(item) -> tuple[int, int, int]:
    """(gross, discount, tax) in minor units; each step rounded half up."""
    gross = _r(Decimal(item.quantity) * item.unit_price_minor)
    discount = _r(Decimal(gross) * item.discount_rate_bps / _BPS) + item.discount_amount_minor
    taxable = gross - discount
    tax = _r(Decimal(taxable) * item.tax_rate_bps / _BPS)
    return gross, discount, tax


def line_total(item) -> int:
    gross, discount, tax = _parts(item)
    return gross - discount + tax


def totals_of(items) -> Totals:
    subtotal = discount = tax = 0
    for item in items:
        g, d, t = _parts(item)
        subtotal += g
        discount += d
        tax += t
    return Totals(subtotal, discount, tax, subtotal - discount + tax)


def totals(invoice) -> Totals:
    return totals_of(invoice.lines.all())


# The columns the totals are computed from; all plaintext.
AMOUNT_FIELDS = ("quantity", "unit_price_minor", "tax_rate_bps", "discount_rate_bps", "discount_amount_minor")


def stored_total(invoice) -> int:
    """The invoice total read from plaintext columns only, so it works while the vault is sealed."""
    return totals_of(invoice.lines.only("pk", "invoice_id", *AMOUNT_FIELDS)).total
