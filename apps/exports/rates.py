"""Accounting rates: read from the cache by the export, filled by the hourly worker job.

`daily_rate` never calls out. `backfill` is the only code here that does, and it is bounded.
"""
import datetime
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.exports.models import DailyRate
from apps.rails import http

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNTING_CURRENCY = "EUR"
CRYPTO = {"BTC", "ETH", "USDT", "USDC"}
BITSTAMP_QUOTES = {"EUR", "USD"}


def accounting_currency(org) -> str:
    value = (org.settings or {}).get("accounting_currency") if isinstance(org.settings, dict) else None
    return str(value).upper() if value else DEFAULT_ACCOUNTING_CURRENCY


def daily_rate(base: str, quote: str, day: datetime.date) -> Decimal | None:
    """The cached price of 1 `base` in `quote` on `day`; 1 for the same currency; None when unknown."""
    if base == quote:
        return Decimal(1)
    row = DailyRate.objects.filter(day=day, base=base, quote=quote).first()
    if row is None or not row.rate:
        return None
    try:
        return Decimal(row.rate)
    except InvalidOperation:
        return None


def rate_source(base: str, quote: str, day: datetime.date) -> str:
    if base == quote:
        return "identity"
    row = DailyRate.objects.filter(day=day, base=base, quote=quote).first()
    return row.source if row is not None and row.rate else "missing"


class _NoRate(Exception):
    """The source answered but has no such rate."""


def _dec(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise http.Unavailable("rates", "unparsable") from None
    if not d.is_finite() or d <= 0:
        raise http.Unavailable("rates", "unusable")
    return d


def _frankfurter(base: str, quote: str, day: datetime.date) -> Decimal:
    root = settings.FX_FRANKFURTER_URL.rstrip("/")
    data = http.get_json(f"{root}/{day.isoformat()}?base={base}&symbols={quote}", service="frankfurter")
    try:
        return _dec(data["rates"][quote])
    except (KeyError, TypeError):
        raise _NoRate from None


def _bitstamp_close(asset: str, fiat: str, day: datetime.date) -> Decimal:
    """`day`'s closing price on Bitstamp (owner's choice over Kraken, 2026-11-09); only closed days are asked."""
    start = int(datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.timezone.utc).timestamp())
    pair = f"{asset}{fiat}".lower()
    if not pair.isalnum():
        raise _NoRate
    data = http.get_json(
        f"https://www.bitstamp.net/api/v2/ohlc/{pair}/?step=86400&limit=1&start={start}", service="bitstamp")
    try:
        for candle in data["data"]["ohlc"]:
            if int(candle["timestamp"]) == start:
                return _dec(candle["close"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise http.Unavailable("bitstamp", "unparsable") from None
    raise http.Unavailable("bitstamp", "no candle")  # the day may not have closed on their side yet


def fetch(base: str, quote: str, day: datetime.date) -> tuple[Decimal, str]:
    """(rate, source). Unavailable: try again later. _NoRate: nobody has this pair."""
    if base in CRYPTO:
        if quote in BITSTAMP_QUOTES:
            return _bitstamp_close(base, quote, day), "bitstamp"
        if quote in CRYPTO:
            raise _NoRate
        return _bitstamp_close(base, "EUR", day) * _frankfurter("EUR", quote, day), "bitstamp×ecb"
    if quote in CRYPTO:
        raise _NoRate
    return _frankfurter(base, quote, day), "frankfurter"


def needed_pairs() -> set[tuple[datetime.date, str, str]]:
    """Every (day, currency, accounting currency) the books need, from plaintext columns only."""
    from apps.invoices.models import Invoice
    from apps.payments.models import Payment

    today = timezone.now().date()
    out = set()
    # Carried-over history (external_reference `zaprite:…` / `rf:…`, a plaintext column) is left alone: its old
    # BTC days lie beyond what the daily candles reach and would be retried every hour for ever.
    history = Q(external_reference__startswith="zaprite:") | Q(external_reference__startswith="rf:")
    invoices = (Invoice.objects.exclude(status="draft").exclude(history).select_related("org")
                .only("currency", "issue_date", "org__settings").iterator(chunk_size=500))
    for inv in invoices:
        out.add((inv.issue_date, inv.currency, accounting_currency(inv.org)))
    payments = (Payment.objects.filter(state="confirmed", confirmed_at__isnull=False, invoice__isnull=False)
                .exclude(Q(invoice__external_reference__startswith="zaprite:")
                         | Q(invoice__external_reference__startswith="rf:"))
                .select_related("invoice", "invoice__org")
                .only("confirmed_at", "invoice__currency", "invoice__org__settings").iterator(chunk_size=500))
    for pay in payments:
        out.add((timezone.localtime(pay.confirmed_at, datetime.timezone.utc).date(), pay.invoice.currency,
                 accounting_currency(pay.invoice.org)))
    # A day still running has no closing rate yet.
    return {(d, b, q) for d, b, q in out if b != q and d < today}


def backfill(limit: int = 50) -> int:
    """Fetch at most `limit` missing pairs, newest day first; returns how many were stored."""
    have = set(DailyRate.objects.values_list("day", "base", "quote"))
    missing = sorted(needed_pairs() - have, reverse=True)[:limit]
    stored = 0
    for day, base, quote in missing:
        try:
            rate, source = fetch(base, quote, day)
        except http.Unavailable:
            continue
        except (http.Refused, _NoRate):
            DailyRate.objects.get_or_create(day=day, base=base, quote=quote, defaults={"rate": "", "source": "none"})
            continue
        DailyRate.objects.get_or_create(
            day=day, base=base, quote=quote, defaults={"rate": format(rate, "f"), "source": source})
        stored += 1
    return stored
