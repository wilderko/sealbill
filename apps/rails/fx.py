"""Exchange rates from two independent sources; refuse when they disagree.

Decimal throughout: prices are parsed from the JSON strings, never from floats.

The EUR to other-fiat leg (CZK, PLN, HUF, ...) has one source, Frankfurter (ECB), and is
multiplied into both prices, so the two-source check cannot catch it (audit MONEY-9). It is held
to two rules of its own instead: an answer dated more than `FX_MAX_ECB_AGE_DAYS` ago is refused,
and so is one more than `FX_MAX_LEG_JUMP_BPS` away from the last answer accepted for that pair
(`FxLegRate`). Each leg is a watcher (`fx:EUR:<currency>`): a refused leg reaches the staff alert
and Needs a person under its own name, saying that quotes in that currency are refused and what
to do (`leg_explanation`). A jump that is real is accepted by a person (`accept_leg`), never by
code: the refused answer waits for them in `FxLegRate.pending_rate`.
"""

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.rails import http

logger = logging.getLogger(__name__)

FX_MAX_DIVERGENCE_BPS = 100
# Friday's rate on a Monday is 3 days old; Thursday's on Easter Tuesday morning is 5 (the ECB
# publishes nothing on Good Friday or Easter Monday), and those quotes must still be made.
FX_MAX_ECB_AGE_DAYS = 5
FX_MAX_LEG_JUMP_BPS = 1000  # 10 % from the last accepted rate of the pair
CACHE_SECONDS = 60
LEG_PREFIX = "fx:"  # a leg's watcher: fx:<base>:<quote>
# Pairs Bitstamp quotes directly for BTC, ETH, USDT and USDC; every other fiat goes EUR x ECB.
DIRECT_FIAT = {"EUR", "USD", "GBP"}
CRYPTO = {"BTC", "ETH", "USDT", "USDC"}


class RatesUnavailable(Exception):
    pass


class RatesDiverge(Exception):
    def __init__(self, bps: int):
        super().__init__(f"exchange-rate sources diverge by {bps} bps")
        self.bps = bps


@dataclass(frozen=True)
class RatePair:
    rate: Decimal
    second: Decimal
    source: str


def _dec(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RatesUnavailable("unparsable price") from None
    if not d.is_finite() or d <= 0:
        raise RatesUnavailable("unusable price")
    return d


def _bitstamp(asset, fiat) -> Decimal:
    """Bitstamp's public ticker (the owner's choice over Kraken, 2026-11-09): last trade price."""
    pair = f"{asset}{fiat}".lower()
    if not pair.isalnum():
        raise RatesUnavailable("bitstamp pair unusable")
    data = http.get_json(f"https://www.bitstamp.net/api/v2/ticker/{pair}/", service="bitstamp")
    try:
        return _dec(data["last"])
    except (KeyError, TypeError):
        raise RatesUnavailable("bitstamp answer unparsable") from None


def _coinbase(asset, fiat) -> Decimal:
    data = http.get_json(f"https://api.coinbase.com/v2/prices/{asset}-{fiat}/spot", service="coinbase")
    try:
        return _dec(data["data"]["amount"])
    except (KeyError, TypeError):
        raise RatesUnavailable("coinbase answer unparsable") from None


def _ecb(currency) -> Decimal:
    base = settings.FX_FRANKFURTER_URL.rstrip("/")
    data = http.get_json(f"{base}/latest?base=EUR&symbols={currency}", service="frankfurter")
    try:
        rate = _dec(data["rates"][currency])
        day = datetime.date.fromisoformat(data["date"])
        if len(format(rate, "f")) > 40:
            raise ValueError("an implausible rate")
    except (KeyError, TypeError, ValueError):
        raise RatesUnavailable("frankfurter answer unparsable") from None
    return _checked_leg("EUR", currency, rate, day)


def _leg_failed(name: str, kind: str, message: str) -> RatesUnavailable:
    from apps.rails import health

    logger.warning("fx: the %s leg was refused (%s)", name, kind)
    try:
        health.failed(name, kind)
    except Exception as exc:  # noqa: BLE001 - the refusal stands; its bookkeeping must not become a 500
        logger.error("recording %s as failing did not work (%s)", name, type(exc).__name__)
    return RatesUnavailable(message)


def _checked_leg(base: str, quote: str, rate: Decimal, day: datetime.date) -> Decimal:
    """`rate` (1 `base` in `quote`, dated `day` by its source) if it is recent and close to the last
    one accepted for the pair, which it then replaces; else RatesUnavailable. A refused answer is
    never stored, so asking again cannot walk the reference away."""
    from apps.rails import health
    from apps.rails.models import FxLegRate

    name = f"fx:{base}:{quote}"
    age = (timezone.now().date() - day).days
    if age > FX_MAX_ECB_AGE_DAYS:
        raise _leg_failed(name, "stale", f"frankfurter {base}/{quote} rate is stale ({age} days old)")
    if age < -1:  # a day ahead is a time zone; more is a source that cannot be trusted with the date
        raise _leg_failed(name, "stale", f"frankfurter {base}/{quote} rate is dated ahead of today")
    last = FxLegRate.objects.filter(base=base, quote=quote).first()
    if last is not None:
        try:
            previous = _dec(last.rate)
        except RatesUnavailable:
            previous = None  # an unreadable reference is replaced, as if there were none
        if previous is not None:
            bps = abs(rate - previous) / previous * 10000
            if bps > FX_MAX_LEG_JUMP_BPS:
                # Kept for a person to accept (`accept_leg`); the reference does not move.
                FxLegRate.objects.filter(pk=last.pk).update(
                    pending_rate=format(rate, "f"), pending_source_date=day, pending_seen_at=timezone.now())
                raise _leg_failed(name, "jump", f"frankfurter {base}/{quote} moved more than 10 % since the "
                                                f"last accepted rate ({int(bps)} bps): a person must check it")
    FxLegRate.objects.update_or_create(base=base, quote=quote, defaults={
        "rate": format(rate, "f"), "source_date": day,
        "pending_rate": "", "pending_source_date": None, "pending_seen_at": None})
    try:
        health.ok(name)
    except Exception as exc:  # noqa: BLE001 - as above
        logger.error("recording %s as answering did not work (%s)", name, type(exc).__name__)
    return rate


def _guard(call, *args) -> Decimal:
    try:
        return call(*args)
    except (http.Unavailable, http.Refused):
        raise RatesUnavailable(f"{call.__name__.strip('_')} unavailable") from None


def _direct(asset, fiat) -> RatePair:
    primary = _guard(_bitstamp, asset, fiat)
    second = _guard(_coinbase, asset, fiat)
    bps = abs(primary - second) / primary * 10000
    if bps > FX_MAX_DIVERGENCE_BPS:
        raise RatesDiverge(int(bps.to_integral_value()))
    return RatePair(primary, second, "bitstamp+coinbase")


def _checked(pair: RatePair) -> RatePair:
    """Legs inside the bound can still compound past it: check the composite too."""
    bps = abs(pair.rate - pair.second) / pair.rate * 10000
    if bps > FX_MAX_DIVERGENCE_BPS:
        raise RatesDiverge(int(bps.to_integral_value()))
    return pair


def _compute(asset, currency) -> RatePair:
    if currency in DIRECT_FIAT:
        return _direct(asset, currency)
    if currency in CRYPTO:
        a, b = price_in(asset, "USD"), price_in(currency, "USD")
        return _checked(RatePair(a.rate / b.rate, a.second / b.second, "bitstamp+coinbase"))
    eur = price_in(asset, "EUR")
    ecb = _guard(_ecb, currency)
    return _checked(RatePair(eur.rate * ecb, eur.second * ecb, "bitstamp+coinbase×ecb"))


def price_in(asset: str, currency: str, use_cache: bool = True) -> RatePair:
    """Price of 1 unit of `asset` in `currency` major units. `use_cache=False` really asks the
    sources (the health probe); the answer is still stored for everyone else."""
    if asset == currency:
        return RatePair(Decimal(1), Decimal(1), "identity")
    key = f"fx:{asset}:{currency}"
    hit = cache.get(key) if use_cache else None
    if hit is not None:
        return hit
    pair = _compute(asset, currency)
    cache.set(key, pair, timeout=CACHE_SECONDS)
    return pair


def leg_of(watcher_name: str) -> tuple[str, str] | None:
    """(base, quote) of a leg's watcher name (`fx:EUR:CZK`), else None."""
    parts = watcher_name.split(":")
    if len(parts) == 3 and parts[0] + ":" == LEG_PREFIX and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def leg_explanation(watcher_name: str, kind: str) -> str:
    """For staff, in the alert and on Needs a person: a refused leg means quotes in that currency
    are refused (payments are still seen), and what to do about it. Rates only, no client data."""
    from apps.rails.models import FxLegRate

    base, quote = leg_of(watcher_name) or ("", watcher_name)
    refused = f"Quotes in {quote} are refused"
    if kind == "jump":
        row = FxLegRate.objects.filter(base=base, quote=quote).first()
        moved = (f" ({row.rate} to {row.pending_rate} {quote} per {base})"
                 if row is not None and row.pending_rate else "")
        return (f"{refused}: the ECB rate from Frankfurter moved more than 10 % since the last one accepted"
                f"{moved}. Check the ECB's rate: if the move is real, accept it on Needs a person; if it is "
                "not, check that FX_FRANKFURTER_URL answers with the ECB's rates.")
    if kind == "stale":
        return (f"{refused}: Frankfurter's newest {base}/{quote} rate is more than {FX_MAX_ECB_AGE_DAYS} days old "
                "(or dated ahead of today). If the ECB has not published for a while (it does not on TARGET "
                "holidays), wait for the ECB; otherwise check that FX_FRANKFURTER_URL answers with current rates.")
    return f"{refused}: its exchange rate could not be used ({kind or 'unknown'})."


def accept_leg(leg, user, note: str) -> bool:
    """A person checked the ECB's rate and accepts the refused answer waiting for `leg` as the new
    reference. Audited under their name with the pair and both rates; the leg's watcher recovers.
    False when nothing is waiting (accepted already, or the rate came back within the bound)."""
    from django.db import transaction

    from apps.audit import services as audit
    from apps.rails import health
    from apps.rails.models import FxLegRate

    note = (note or "").strip()
    if not note:
        raise ValueError("a note is required to accept an exchange rate")
    with transaction.atomic():
        row = FxLegRate.objects.select_for_update().filter(pk=leg.pk).first()
        if row is None or not row.pending_rate or row.pending_source_date is None:
            return False
        old = row.rate
        row.rate, row.source_date = row.pending_rate, row.pending_source_date
        row.pending_rate, row.pending_source_date, row.pending_seen_at = "", None, None
        row.save()
        audit.record(user, "fx_leg.accept", row, {
            "base": row.base, "quote": row.quote, "old_rate": old, "new_rate": row.rate,
            "source_date": row.source_date.isoformat(), "note": note})
        health.ok(f"{LEG_PREFIX}{row.base}:{row.quote}")
    logger.info("fx: a person accepted a new %s/%s reference", row.base, row.quote)
    return True
