"""Quotes: what to send, on which rail, to settle what an invoice still owes.

A chain quote (EVM, Tron, Solana, TON) asks for a **unique amount** on the rail's shared
address: the base amount plus `k` steps, with the smallest `k` no other held quote of its
amount group uses (`apps.quotes.groups`: every network of the address, every USD stablecoin),
so an arrival is matched to its invoice by amount alone, and one quote's amount sent on the
wrong network or in the wrong stablecoin is never another quote's exact amount. An amount a
quote of the group released lately is handed out only when no other is free, since matching
still counts it as the first client's for a month. A gateway quote (BTCPay, Lightning) gets
its own target from the gateway instead and is matched by the gateway's id.

The worker side (`expire_quotes`, `release_quotes`) and `close_quotes` run while the vault is
sealed: they write plaintext columns with plain updates and audit through `record_system`.
"""

import bisect
import datetime
import logging
from contextlib import contextmanager

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.audit import services as audit
from apps.invoices.models import Invoice
from apps.invoices.money import stored_total
from apps.invoices.reference import payment_reference
from apps.invoices.services import INVOICE_PLAIN_FIELDS, lock_invoice
from apps.orgs.models import Organization
from apps.payments.models import Payment
from apps.payments.services import confirmed_minor
from apps.quotes import amounts, groups
from apps.quotes.errors import QUOTE_ERROR_CODES, QuoteError
from apps.quotes.models import Quote
from apps.rails import fx, health, http, uris
from apps.rails.gateways import registry
from apps.rails.models import CHAIN_KINDS, RailAccount, WatcherHealth, ton_blocked
from apps.rails.networks import NETWORKS
from apps.vault.registry import plaintext_columns

__all__ = [
    "QuoteError", "QUOTE_ERROR_CODES", "QUOTE_LIFETIME_S", "QUOTE_AMOUNT_HOLD_S", "QUOTE_REQUEST_BUDGET_S",
    "create_quote",
    "expire_quotes", "release_quotes", "check_capacity", "close_quotes", "gateway_money_may_be_coming", "payment_still_waiting",
    "could_not_look",
]

logger = logging.getLogger(__name__)

QUOTE_LIFETIME_S = 1800
QUOTE_REQUEST_BUDGET_S = 5  # a quote asked for in a web request waits at most this long per outbound call
QUOTE_AMOUNT_HOLD_S = 86400  # an amount stays reserved this long after its quote closes
REUSE_MIN_LEFT_S = 300  # an open quote with less time left than this is not handed out again
QUOTABLE = ("sent", "overdue", "declared", "partially_paid")
# The network a gateway quote names for each asset its account may take.
GATEWAY_NETWORKS = {
    "btcpay": {"BTC": "bitcoin", "XMR": "monero", "ZEC": "zcash"},
    "lightning": {"BTC": "lightning"},
}
CLOSED_STATES = ("expired", "cancelled", "paid")
_ACCOUNT_FIELDS = plaintext_columns(RailAccount)
# Capacity (final review I3): every unreleased quote holds an amount for about a day, and a public
# payment link makes as many invoices as anyone asks for. One invoice holds at most this many
# unreleased quotes; a further one is refused (`too_many_quotes`) until one is released, a day after it
# closed. Twenty leaves room for a client who switches networks or refreshes (residual R1).
QUOTES_PER_INVOICE = 20
# Fewer free amounts than this, in the worst window of 100 of an amount group, is a staff item: the
# periodic `check_capacity` (residual R2) fails a watcher named `amounts:<family>:<unit>:<account pk>`
# ("few_free_amounts") and ends the episode only once every window has enough free again, so staff
# are told once per episode (`jobs._alert_failing_watchers`).
FEW_FREE_AMOUNTS = 20
CAPACITY_PREFIX = "amounts:"


FX_HEALTH = "fx"  # the exchange-rate sources, as a watcher: failing while a quote could not get a rate


@contextmanager
def _mapped_errors(rail: str = ""):
    """Exchange-rate and gateway failures as quote refusals ("could not look", never "unpaid").
    A source that could not answer is recorded as a failing watcher: the exchange rates under
    `fx`, the gateway under `rail` (`btcpay:<pk>`, `lightning:<pk>`), named as the poller names it.
    Rates that answered but disagree are a refusal, not a failure to look."""
    try:
        yield
    except fx.RatesUnavailable as exc:
        code, kind = "rates_unavailable", type(exc).__name__
        _failed(FX_HEALTH, str(exc) or "unavailable")
    except fx.RatesDiverge as exc:
        code, kind = "rates_diverge", type(exc).__name__
    except (http.Unavailable, http.Refused) as exc:
        code, kind = "rail_unavailable", type(exc).__name__
        if rail:
            _failed(rail, exc.kind if isinstance(exc, http.Unavailable) else "refused")
    else:
        return
    logger.warning("quote refused: %s (%s)", code, kind)
    raise QuoteError(code)


def _failed(name: str, kind: str) -> None:
    try:
        health.failed(name, kind)
    except Exception as exc:  # noqa: BLE001 - the refusal stands; its bookkeeping must not become a 500
        logger.error("recording %s as failing did not work (%s)", name, type(exc).__name__)


def _looked(*sources: str, rail: str = "") -> None:
    """A quote was made: the sources it asked are answering again. `sources` are rate sources;
    "identity" and "btcpay" mean the exchange rates were never asked."""
    try:
        if any(s not in ("", "identity", "btcpay") for s in sources):
            health.ok(FX_HEALTH)
        if rail:
            health.ok(rail)
    except Exception as exc:  # noqa: BLE001 - the quote stands
        logger.error("recording a source as answering did not work (%s)", type(exc).__name__)


def _enabled(account: RailAccount, network: str, asset: str) -> bool:
    assets = account.assets or []
    if account.kind in GATEWAY_NETWORKS:
        return asset in assets and GATEWAY_NETWORKS[account.kind].get(asset) == network
    net = NETWORKS.get(network)
    return (account.kind in CHAIN_KINDS and net is not None and net.family == account.kind
            and network in (account.networks or []) and asset in net.assets and asset in assets)


def pair_offered(invoice: Invoice, network: str, asset: str) -> bool:
    """Whether the invoice lets its rails be quoted in this pair: every pair when it names none
    (`Invoice.offered_pairs`; a payment link's custom method set names some)."""
    pairs = invoice.offered_pairs or []
    return not pairs or [network, asset] in [list(p) for p in pairs]


def _payable(invoice: Invoice, account_pk: int, network: str, asset: str) -> tuple[int, RailAccount]:
    """(remainder, account) when `invoice` may be quoted on that rail now; QuoteError otherwise.

    `invoice` is a plaintext-only row (a plain read, or `lock_invoice`)."""
    if invoice.status not in QUOTABLE:
        raise QuoteError("not_payable")
    account = RailAccount.objects.only(*_ACCOUNT_FIELDS).filter(pk=account_pk).first()
    if (account is None or not account.active or account.org_id != invoice.org_id
            or account.method_key not in (invoice.offered_methods or [])):
        raise QuoteError("method_not_offered")
    if not _enabled(account, network, asset) or not pair_offered(invoice, network, asset):
        raise QuoteError("asset_not_enabled")
    if (account.kind in CHAIN_KINDS and not account.address) or ton_blocked(account.kind):
        raise QuoteError("rail_unavailable")  # TON: off until its credit leg is verified
    remainder = stored_total(invoice) - confirmed_minor(invoice)
    if remainder <= 0:
        raise QuoteError("nothing_due")
    return remainder, account


def _reusable(invoice_pk, account_pk, network, asset, remainder, now) -> Quote | None:
    return (
        Quote.objects.filter(
            invoice_id=invoice_pk, rail_account_id=account_pk, network=network, asset=asset, state="open",
            invoice_amount_minor=remainder,
            expires_at__gte=now + datetime.timedelta(seconds=REUSE_MIN_LEFT_S),
        ).order_by("-expires_at", "-pk").first()
    )


def _cancel_same_key(invoice_pk, account_pk, network, asset, now) -> list[int]:
    """A refresh: the open quotes for this key are cancelled. Their amounts stay held."""
    pks = list(Quote.objects.filter(invoice_id=invoice_pk, rail_account_id=account_pk, network=network,
                                    asset=asset, state="open").values_list("pk", flat=True))
    Quote.objects.filter(pk__in=pks, state="open").update(state="cancelled", closed_at=now)
    return pks


def _held_amounts(network: str, asset: str, address: str, candidates: list[int]) -> set[int]:
    """Which of `candidates` (base units of `asset` on `network`) a quote of the same amount group
    still holds, on whichever network and in whichever asset of the group it was issued."""
    by_value = {str(groups.group_amount(network, asset, c)): c for c in candidates}
    return {
        by_value[a] for a in Quote.objects.filter(
            amount_group=groups.amount_group(network, asset, address), group_amount__in=list(by_value),
        ).exclude(state="released").values_list("group_amount", flat=True)
    }


def _released_lately(network: str, asset: str, address: str, candidates: list[int], now) -> set[int]:
    """Which of `candidates` a quote of the same amount group held until lately: released, and
    closed within `groups.RELEASED_MEMORY_S` of `now`. Matching still lets such an amount stand in
    a transfer's way (it may be the first client's late payment), so handing it out again would
    send the next client's exact payment to review."""
    by_value = {str(groups.group_amount(network, asset, c)): c for c in candidates}
    return {
        by_value[a] for a in Quote.objects.filter(
            amount_group=groups.amount_group(network, asset, address), group_amount__in=list(by_value),
            state="released", closed_at__gte=now - datetime.timedelta(seconds=groups.RELEASED_MEMORY_S),
        ).values_list("group_amount", flat=True)
    }


def _check_quotes_per_invoice(invoice_pk: int) -> None:
    if Quote.objects.filter(invoice_id=invoice_pk).exclude(state="released").count() >= QUOTES_PER_INVOICE:
        logger.info("quote refused: the invoice already holds %s unreleased quotes", QUOTES_PER_INVOICE)
        raise QuoteError("too_many_quotes")


def _capacity_name(group: str) -> str | None:
    """The watcher of an amount group ("family:unit:address"), named by the lowest account on that
    address rather than by the address."""
    family, unit, address = (group.split(":", 2) + ["", ""])[:3]
    account = (RailAccount.objects.filter(address__iexact=address).order_by("pk").values_list("pk", flat=True)
               .first() if address else None)
    return f"{CAPACITY_PREFIX}{family}:{unit}:{account}" if account is not None else None


def _worst_free(rows: list[tuple[int, int]]) -> int:
    """`rows` are the (group amount, group step) of a group's unreleased quotes. The fewest amounts left
    free in any window of 100 an allocation could look at: a window starting at a held amount, on its
    asset's grid, is the worst there is (moving a window's start to its first held amount loses none)."""
    held = sorted({amount for amount, _step in rows})
    worst = amounts.MAX_SUFFIX + 1
    for start, step in rows:
        top = start + amounts.MAX_SUFFIX * step
        lo, hi = bisect.bisect_left(held, start), bisect.bisect_right(held, top)
        taken = sum(1 for h in held[lo:hi] if (h - start) % step == 0)
        worst = min(worst, amounts.MAX_SUFFIX + 1 - taken)
    return worst


def check_capacity() -> dict[str, int]:
    """The `quote_capacity` worker job (every 5 minutes, residual R2): for each amount group with
    unreleased quotes, the worst window of free amounts. A group with fewer than `FEW_FREE_AMOUNTS` free
    fails its watcher; a watcher of a group that has enough free in every window again (or no quote left)
    recovers. Allocations never touch it, so a quote at another price cannot end an episode. Plaintext
    only (sealed-safe). Returns {watcher name: free} for the groups that are low."""
    by_group: dict[str, list[tuple[int, int]]] = {}
    for group, value, network, asset in (Quote.objects.exclude(state="released").exclude(amount_group="")
                                         .values_list("amount_group", "group_amount", "network", "asset")):
        spec = NETWORKS.get(network).assets.get(asset) if network in NETWORKS else None
        if spec is None or not str(value).isdigit():
            continue
        by_group.setdefault(group, []).append((int(value), groups.group_amount(network, asset, spec.step)))
    low = {}
    for group, rows in by_group.items():
        free = _worst_free(rows)
        name = _capacity_name(group) if free < FEW_FREE_AMOUNTS else None
        if name is not None:
            low[name] = min(free, low.get(name, free))
    for name, free in low.items():
        if not health.failing_now(name):
            logger.info("quote amounts running out for %s (%s free in the worst window)", name, free)
        health.failed(name, "few_free_amounts")
    for name in (WatcherHealth.objects.filter(name__startswith=CAPACITY_PREFIX, failing_since__isnull=False)
                 .exclude(name__in=list(low)).values_list("name", flat=True)):
        health.ok(name)
    return low


def _amount_taken(network: str, asset: str, address: str, amount: int) -> bool:
    return Quote.objects.filter(amount_group=groups.amount_group(network, asset, address),
                                group_amount=str(groups.group_amount(network, asset, amount))
                                ).exclude(state="released").exists()


def _allocate(locked: Invoice, rail_account, account: RailAccount, network: str, asset: str, *,
              remainder: int, pair: fx.RatePair, usd_price, now) -> Quote:
    """Save a chain quote at the smallest amount free in its amount group, preferring one that no
    quote of the group released lately (`_released_lately`); such an amount is handed out only
    when nothing else is free, so no capacity is lost. The partial unique constraint is the
    arbiter: a slot another request took after the lookup fails its insert inside its own
    savepoint (the outer transaction stays usable) and the next `k` is tried. A group with all
    100 amounts held refuses the quote (`no_free_amount`); how full the groups are is measured apart
    from any allocation (`check_capacity`)."""
    spec = NETWORKS[network].assets[asset]
    base = amounts.base_units_for(remainder, locked.currency, pair.rate, spec)
    candidates = [base + k * spec.step for k in range(amounts.MAX_SUFFIX + 1)]
    held = _held_amounts(network, asset, account.address, candidates)
    lately = _released_lately(network, asset, account.address, candidates, now)
    org_settings = Organization.objects.filter(pk=locked.org_id).values_list("settings", flat=True).first()
    reference = payment_reference(locked)
    for amount in [a for a in candidates if a not in lately] + [a for a in candidates if a in lately]:
        if amount in held:
            continue
        min_accept, ceiling = amounts.windows(amount, spec, usd_price, org_settings or {})
        quote = Quote(
            invoice=locked, rail_account=rail_account, network=network, asset=asset, address=account.address,
            amount_base_units=str(amount), min_accept_base_units=str(min_accept),
            ceiling_base_units=str(ceiling), invoice_amount_minor=remainder,
            rate=str(pair.rate), rate_second=str(pair.second), rate_source=pair.source,
            issued_at=now, expires_at=now + datetime.timedelta(seconds=QUOTE_LIFETIME_S),
            payment_uri=uris.payment_uri(network, asset, account.address, amount, reference),
        )
        try:
            with transaction.atomic():
                quote.save()
            return quote
        except IntegrityError:
            if not _amount_taken(network, asset, account.address, amount):
                raise  # not a race for this amount: a real error
    raise QuoteError("no_free_amount")


def _audit_create(quote: Quote, cancelled: list[int]) -> None:
    audit.record_system("quote.create", quote, {
        "quote": quote.pk, "invoice": quote.invoice_id, "rail": quote.rail_account_id,
        "network": quote.network, "asset": quote.asset, "amount_base_units": quote.amount_base_units,
        "invoice_amount_minor": quote.invoice_amount_minor, "cancelled": cancelled,
    })


def create_quote(invoice: Invoice, rail_account: RailAccount, network: str, asset: str, *,
                 refresh: bool = False, now=None) -> Quote:
    """A quote for what `invoice` still owes, on `rail_account`, paid as `asset` on `network`.

    Refused (QuoteError) unless the invoice is sent, overdue, declared or partially paid, offers
    `rail:<pk>` with that account active and in the invoice's organization, and the account has
    that network and asset enabled. An open quote with the same key and remainder and at least
    300 s left is returned as it is, unless `refresh`, which cancels it (its amount stays held).

    Nothing that waits on the network runs under the invoice lock: exchange rates for a chain
    quote, and the gateway's `create_target` for a BTCPay or Lightning quote, come first. Called
    from a web request, it runs inside `http.request_budget(QUOTE_REQUEST_BUDGET_S)`. A source
    that could not answer is recorded in `WatcherHealth` (`fx`, or the gateway's own name).
    """
    now = now or timezone.now()
    plain = Invoice.objects.only(*INVOICE_PLAIN_FIELDS).get(pk=invoice.pk)
    remainder, account = _payable(plain, rail_account.pk, network, asset)
    if not refresh:
        found = _reusable(plain.pk, account.pk, network, asset, remainder, now)
        if found is not None:
            return found
    if account.kind in GATEWAY_NETWORKS:
        return _gateway_quote(plain, rail_account, network, asset, remainder, refresh=refresh, now=now)
    with _mapped_errors():
        pair = fx.price_in(asset, plain.currency)
        usd = pair if plain.currency == "USD" else fx.price_in(asset, "USD")
        usd_price = usd.rate
    _looked(pair.source, usd.source)
    with transaction.atomic():
        locked = lock_invoice(plain.pk)
        remainder, account = _payable(locked, rail_account.pk, network, asset)
        if not refresh:
            found = _reusable(locked.pk, account.pk, network, asset, remainder, now)
            if found is not None:
                return found
        _check_quotes_per_invoice(locked.pk)
        cancelled = _cancel_same_key(locked.pk, account.pk, network, asset, now) if refresh else []
        quote = _allocate(locked, rail_account, account, network, asset, remainder=remainder, pair=pair,
                          usd_price=usd_price, now=now)
        _audit_create(quote, cancelled)
    return quote


def _gateway_quote(plain: Invoice, rail_account: RailAccount, network: str, asset: str, remainder: int, *,
                   refresh: bool, now) -> Quote:
    _check_quotes_per_invoice(plain.pk)  # before the gateway is asked for a target
    quote = Quote(invoice=plain, rail_account=rail_account, network=network, asset=asset, address="",
                  invoice_amount_minor=remainder, issued_at=now,
                  expires_at=now + datetime.timedelta(seconds=QUOTE_LIFETIME_S))
    gateway = registry.for_account(rail_account)
    rail = f"{rail_account.kind}:{rail_account.pk}"
    with _mapped_errors(rail):
        gateway.create_target(quote)
    _looked(quote.rate_source, rail=rail)
    # The target is priced for the remainder read above; a payment landing in between leaves
    # this quote asking for more than is now owed, which settles as overpaid and is flagged.
    with transaction.atomic():
        locked = lock_invoice(plain.pk)
        current, account = _payable(locked, rail_account.pk, network, asset)
        if not refresh:
            found = _reusable(locked.pk, account.pk, network, asset, current, now)
            if found is not None:
                return found  # a parallel request won; the unused target expires at the gateway
        _check_quotes_per_invoice(locked.pk)
        cancelled = _cancel_same_key(locked.pk, account.pk, network, asset, now) if refresh else []
        quote.min_accept_base_units = quote.min_accept_base_units or quote.amount_base_units
        quote.ceiling_base_units = quote.ceiling_base_units or quote.amount_base_units
        quote.save()
        _audit_create(quote, cancelled)
    return quote


def expire_quotes(now=None) -> int:
    """Open quotes past their expiry become `expired`; their amounts stay held. Sealed-safe."""
    now = now or timezone.now()
    return Quote.objects.filter(state="open", expires_at__lt=now).update(state="expired", closed_at=now)


def gateway_money_may_be_coming() -> Q:
    """The BTCPay and Lightning quotes whose money may still be on its way: a payment the gateway
    has seen but not settled, or no successful read since the quote expired (the gateway was down,
    or the vault sealed, all that time). They are polled whatever their age and never released;
    a gateway quote holds no amount slot, so keeping one costs nothing. Plaintext only."""
    detected = Payment.objects.filter(state="detected", quote__isnull=False).values("quote_id")
    unread = Q(state__in=("expired", "cancelled")) & (Q(read_at__isnull=True) | Q(read_at__lt=F("expires_at")))
    return Q(rail_account__kind__in=tuple(GATEWAY_NETWORKS)) & (Q(pk__in=detected) | unread)


def could_not_look(quotes, *, read_past_expiry: bool = False) -> str | None:
    """Why the watchers could not look for money paid against `quotes` (a Quote queryset), or None when
    they could. "Could not look" is not "not paid" (final review I1). The reason is a word, never an address;
    plaintext only, so it answers while the vault is sealed:

    - `quote_held`: a quote still holds its amount (it is not `released`), so a late transfer of that amount
      is still its invoice's;
    - `watcher_failing`: a watcher of an account and network one was quoted on is failing (`scan`/`ingest`
      for a chain, the gateway's own otherwise);
    - `watcher_behind`, asked for with `read_past_expiry`: a chain scan of that account and network has not
      read since the quote expired (its last good read is older, or there was none), on a network the
      account still enables;
    - `blocks_not_scanned`: blocks of that address and network were skipped and nobody has acknowledged them.

    Only the watchers of accounts a watcher still reads count (`RailAccount.objects.watched()`): a retired
    account's last failure is a stale row nothing will ever clear. A BTCPay account failing for its speed
    policy can read; it only refuses new quotes (residual R3).

    The link-order void asks this (`links.services._could_not_look`), and so does the API's
    `money_may_be_coming` for a chain quote (audit LS-2), with `read_past_expiry`: liberation.services holds
    its own void while that is true."""
    from apps.rails.gateways.btcpay import SPEED_POLICY_KIND
    from apps.rails.models import ScanGap

    quoted = list(quotes.values_list("state", "rail_account_id", "rail_account__kind", "rail_account__address",
                                     "network", "address", "expires_at").distinct())
    if any(row[0] != "released" for row in quoted):
        return "quote_held"
    networks_of = dict(RailAccount.objects.watched().filter(pk__in={row[1] for row in quoted})
                       .values_list("pk", "networks"))
    watchers, scans, gaps = set(), {}, Q(pk__in=[])
    for _state, account, kind, account_address, network, address, expires_at in quoted:
        if kind in CHAIN_KINDS:
            for seen in {a for a in (address, account_address) if a}:
                gaps |= Q(network=network, address__iexact=seen)
        if account not in networks_of:
            continue
        if kind in CHAIN_KINDS:
            family = NETWORKS[network].family if network in NETWORKS else kind
            scan = f"scan:{family}:{network}:{account}"
            watchers |= {scan, f"ingest:{family}:{network}:{account}"}
            if network in (networks_of[account] or []):
                scans[scan] = max(filter(None, (scans.get(scan), expires_at)), default=None)
        else:
            watchers.add(f"{kind}:{account}")  # btcpay:<pk>, lightning:<pk>
    if (WatcherHealth.objects.filter(name__in=watchers, failing_since__isnull=False)
            .exclude(name__startswith="btcpay:", last_error_kind=SPEED_POLICY_KIND).exists()):
        return "watcher_failing"
    if read_past_expiry:
        for name, last_ok in WatcherHealth.objects.filter(name__in=scans).values_list("name", "last_ok_at"):
            if scans[name] is not None and (last_ok is None or last_ok < scans[name]):
                return "watcher_behind"
    if ScanGap.objects.filter(gaps, acknowledged_at__isnull=True).exists():
        return "blocks_not_scanned"
    return None


def payment_still_waiting() -> Q:
    """The quotes a payment still waits on: one `detected`, or in review and not final yet. Its
    transfer may still be decided against the quote, so the quote must stay what it was when the
    money was sent (audit finding MONEY-7). Plaintext only."""
    waiting = (Payment.objects.filter(quote__isnull=False)
               .filter(Q(state="detected") | Q(state="needs_review", final=False)).values("quote_id"))
    return Q(pk__in=waiting)


def release_quotes(now=None) -> int:
    """Closed quotes whose hold has run out free their amounts (`released`), except a quote a
    payment still waits on (`payment_still_waiting`), chain or gateway, and a gateway quote whose
    money may still be coming (`gateway_money_may_be_coming`). Sealed-safe."""
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(seconds=QUOTE_AMOUNT_HOLD_S)
    kept = Quote.objects.filter(payment_still_waiting() | gateway_money_may_be_coming()).values("pk")
    return (Quote.objects.filter(state__in=CLOSED_STATES, closed_at__lt=cutoff).exclude(pk__in=kept)
            .update(state="released"))


def close_quotes(invoice: Invoice, *, paid_quote: Quote | None = None) -> int:
    """The invoice needs no more quotes (it is settled or void): `paid_quote`, if given and this
    invoice's, becomes `paid` (from open, expired or cancelled: a late payment accepted still
    marks the quote it paid), and the invoice's other `open` quotes become `cancelled`. Quotes
    already closed are left alone. Returns the number cancelled.

    Call it holding the invoice lock. Plaintext only, so it runs while the vault is sealed."""
    now = timezone.now()
    paid_pk = None
    if paid_quote is not None and Quote.objects.filter(
            pk=paid_quote.pk, invoice_id=invoice.pk, state__in=("open", "expired", "cancelled"),
    ).update(state="paid", closed_at=now):
        paid_pk = paid_quote.pk
        paid_quote.state, paid_quote.closed_at = "paid", now
    pks = sorted(Quote.objects.filter(invoice_id=invoice.pk, state="open").values_list("pk", flat=True))
    cancelled = Quote.objects.filter(pk__in=pks, state="open").update(state="cancelled", closed_at=now)
    if paid_pk is not None or pks:
        audit.record_system("quote.close", invoice, {"invoice": invoice.pk, "paid": paid_pk, "cancelled": pks})
    return cancelled
