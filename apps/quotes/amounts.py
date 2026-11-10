"""Quote arithmetic: integer base units out, Decimal rates in, never a float."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from apps.invoices.currencies import minor_units
from apps.orgs.models import default_org_settings
from apps.rails.networks import Asset

MAX_SUFFIX = 99  # a unique amount is base + k * step, k in 0..MAX_SUFFIX
_PRECISION = 80  # digits: room for 78-digit amounts and long derived rates
_BPS = 10000


def _positive(value, what: str) -> Decimal:
    d = Decimal(value)
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{what} must be a positive number")
    return d


def base_units_for(invoice_amount_minor: int, currency: str, rate: Decimal, asset: Asset) -> int:
    """How many base units of `asset` are worth `invoice_amount_minor` of `currency` at `rate`
    (the price of one whole `asset` in `currency`), rounded **up** to a multiple of `asset.step`:
    the client is never asked for less than the invoice is worth."""
    if not isinstance(invoice_amount_minor, int) or isinstance(invoice_amount_minor, bool) or invoice_amount_minor <= 0:
        raise ValueError("the amount to quote must be a positive number of minor units")
    rate = _positive(rate, "the rate")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = _PRECISION, ROUND_CEILING  # an inexact quotient errs upwards too
        exact = Decimal(invoice_amount_minor).scaleb(asset.decimals - minor_units(currency)) / rate
        steps = int((exact / asset.step).to_integral_value(rounding=ROUND_CEILING))
    return steps * asset.step


def windows(base: int, asset: Asset, usd_price: Decimal, org_settings: dict) -> tuple[int, int]:
    """(min_accept, ceiling) around a quoted amount, in base units.

    The allowance is the smaller of `underpay_allowance_usd` (converted at `usd_price`, the price
    of one whole `asset` in USD) and `underpay_allowance_bps` of the amount, rounded **down** to
    a base unit, so a transfer is never let through short by more than the rule allows. The
    ceiling adds `overpay_ceiling_bps` of the amount, also rounded down."""
    s = {**default_org_settings(), **(org_settings or {})}
    bps, over = int(s["underpay_allowance_bps"]), int(s["overpay_ceiling_bps"])
    usd = Decimal(str(s["underpay_allowance_usd"]))
    if bps < 0 or over < 0 or not usd.is_finite() or usd < 0:
        raise ValueError("allowances must not be negative")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = _PRECISION, ROUND_FLOOR
        share_allowance = int((Decimal(base) * bps / _BPS).to_integral_value(rounding=ROUND_FLOOR))
    allowance = min(usd_allowance(asset, usd_price, s), share_allowance)  # both rounded down: so is the smaller
    return base - allowance, base + base * over // _BPS


def usd_allowance(asset: Asset, usd_price: Decimal, org_settings: dict) -> int:
    """The organisation's `underpay_allowance_usd` as base units of `asset` at `usd_price` (the
    price of one whole `asset` in USD), rounded down. The cap that `windows` holds the allowance to."""
    usd = Decimal(str({**default_org_settings(), **(org_settings or {})}["underpay_allowance_usd"]))
    if not usd.is_finite() or usd < 0:
        raise ValueError("allowances must not be negative")
    usd_price = _positive(usd_price, "the USD price")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = _PRECISION, ROUND_FLOOR
        return int((usd.scaleb(asset.decimals) / usd_price).to_integral_value(rounding=ROUND_FLOOR))


def overpay_ceiling(base: int, org_settings: dict) -> int:
    """The most that may arrive for `base` before it is overpaid beyond the organisation's
    `overpay_ceiling_bps` (rounded down, as in `windows`). In whatever unit `base` is in."""
    over = int({**default_org_settings(), **(org_settings or {})}["overpay_ceiling_bps"])
    if over < 0:
        raise ValueError("allowances must not be negative")
    return base + base * over // _BPS
