"""Matching: what money that reached one of our addresses settles.

A transfer read off a chain (`ingest`) or reported by a gateway (`record_gateway_payment`)
becomes a `Payment`. Code confirms one only on evidence: an allow-listed token, our address, a
final transaction made while its quote was live, an amount the quote accepts, and a transfer
`(network, tx, log index)` that has settled nothing else. Anything else waits in the review
queue for a person, and staff are told by e-mail.

A transfer is matched against its whole **amount group** (`apps.quotes.groups`): every network
and every USD stablecoin its address takes, compared by value, since a client who picks the wrong
one still sends the amount they were shown. It is attributed only to a quote of its own network
and asset; a quote elsewhere in the group, or one released in the last 30 days, can only make it
`ambiguous`. A transfer that could belong to more than one invoice is never confirmed by code.

A client's declaration, or a reference in a TON comment, says whose a transfer is; it picks
between quotes but never outweighs a unique amount that names another invoice, and a
declaration never takes it away either: one the amount contradicts is ignored, and stays open
for staff. A declaration never confirms what the amount alone would not: when only the claim
tied the transfer to its quote (it chose between quotes of different invoices, picked one while
another invoice's quote may be the one paid short, names a shortfall below the window, or a
quote issued after the transfer), the payment is attributed in review, credit worked out, for
staff to confirm, and so is every claim that arrives after the transfer was recorded. A TON
comment is the payer's own text in the transaction and may confirm. A declaration only ever
considers its own organization's addresses.

Every path runs while the vault is sealed: rows are read and locked through plaintext columns,
audit goes through `record_system`, and staff letters are addressed when they are sent. Logs
carry ids and reason codes only, never an address, an amount or a hash.
"""

import base64
import datetime
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from apps.audit import services as audit
from apps.invoices.models import Invoice
from apps.invoices.reference import is_valid, payment_reference
from apps.invoices.services import INVOICE_PLAIN_FIELDS, SETTLED, lock_invoice
from apps.payments.models import REVIEW_REASONS, Declaration, Payment
from apps.payments.services import (
    DECLARATION_PLAIN_FIELDS, MAX_PAYMENT_MINOR, PAYMENT_PLAIN_FIELDS, apply_payments,
)
from apps.orgs.models import Organization
from apps.quotes import amounts, groups
from apps.quotes.groups import RELEASED_MEMORY_S  # a released amount still stands in the way this long
from apps.quotes.models import Quote
from apps.rails import addresses, health, http
from apps.rails.chains.base import STATUSES, Transfer, adapter_for
from apps.rails.models import CHAIN_KINDS, RailAccount, ton_blocked
from apps.rails.networks import NETWORKS, Asset, asset_by_contract
from apps.vault.registry import plaintext_columns
from apps.webhooks import events as webhooks

__all__ = [
    "CLOCK_SKEW_S", "DROPPED_AFTER_S", "DECLARATION_LOOKUP_S", "DECLARATIONS_PER_PASS", "RELEASED_MEMORY_S",
    "ingest", "settle_if_final", "refresh_finality", "record_gateway_payment", "gateway_ceiling", "verify_declaration",
    "verify_open_declarations", "match_declarations",
]

logger = logging.getLogger(__name__)

CLOCK_SKEW_S = 120  # a quote is a candidate for transfers up to this long before it was issued
DROPPED_AFTER_S = 600  # "dropped" twice, this far apart and nothing else between: rejected
DECLARATION_LOOKUP_S = 86400  # a declared hash is looked up this long, then left to staff
DECLARATIONS_PER_PASS = 100  # declarations looked up in one `declarations_verify` pass

CANDIDATE_STATES = ("open", "expired", "cancelled", "paid")  # held; a released amount only stands in the way
OPEN_INVOICE = ("sent", "overdue", "declared", "partially_paid")  # what a comment reference may name
# Money arriving for one of these is never applied by code. A settled invoice is closed too:
# applying it would only make it `overpaid`, which nobody is asked to look at.
CLOSED_INVOICE = ("draft", "void", *SETTLED)
DUST_USD = Decimal("0.10")  # below this, a chain transfer is address-poisoning spam, not a payment
UNATTRIBUTED = ("no_matching_invoice", "ambiguous")  # what a later declaration may still resolve
GATEWAY_KINDS = ("btcpay", "lightning")
GATEWAY_STATES = ("detected", "confirmed", "needs_review")
STAFF_CONFIRMATION_REQUIRED = "staff_confirmation_required"  # logged when a gateway tries to confirm one
_HEX_HASH_FAMILIES = ("evm", "tron", "ton")  # their hashes are hex, so case carries nothing (TON: base64 too)
_ACCOUNT_FIELDS = plaintext_columns(RailAccount)
_TON_B64 = re.compile(r"^[A-Za-z0-9+/_-]{43}=?$")  # 32 bytes, padded or not
_DIGITS = re.compile(r"^[0-9]{1,78}$")
_DECISION_FIELDS = ["state", "review_reason", "invoice", "quote", "amount_minor", "refund_due_base_units",
                    "confirmed_at"]


@dataclass
class _Decision:
    state: str  # detected | confirmed | needs_review
    reason: str = ""
    invoice_id: int | None = None
    quote: Quote | None = None
    invoice: Invoice | None = None  # locked, plaintext only: set whenever a quote is
    credit: int = 0  # in the invoice's minor units
    refund: str = ""  # base units paid above the quoted amount
    outcome: str = ""  # the amount rule's verdict against the quote, final or not ("" = acceptable)


# ---------------------------------------------------------------------------
# Chain transfers
# ---------------------------------------------------------------------------
def ingest(transfer: Transfer, *, account: RailAccount, declared_invoice: Invoice | None = None) -> Payment | None:
    """Record a transfer a watcher found, or that a client's declaration named, and decide it.

    Returns None, storing nothing, for a transfer that is not money to `account.address` (another
    address, another chain family, a zero amount). A transfer already recorded is returned as it
    is, unless it sits in review as nobody's and `declared_invoice` now says whose it is: it is
    then attributed with the same rules, credit worked out, but left in review (a claim made
    after the transfer was seen never confirms it). Otherwise, in this order:

    1. a token not on the allow-list goes to review (`token_not_allowed`), with no invoice;
    2. a declaration or a TON comment reference picks that invoice's quote here (see `_attribute`):
       a declaration the amount contradicts is ignored, a TON comment it contradicts is
       `ambiguous`, and one that only the claim ties to its quote attributes the payment but leaves
       it in review, which a TON comment, the payer's own text, need not; otherwise, by amount
       (`_by_amount`), the quote here whose amount it is exactly, else the one invoice whose window
       here holds it, unless the amount group says it could be another invoice's (`ambiguous`; no
       window at all is `no_matching_invoice`);
    3. against that quote: paid already (`duplicate`), for a closed invoice (`invoice_closed`), late
       (`after_expiry`), above the ceiling (`overpaid_beyond_ceiling`), or confirmed;
    4. a transfer that is not final yet is stored `detected` and decided by `settle_if_final`.
    """
    net = NETWORKS.get(transfer.network)
    if net is None or net.family != account.kind or transfer.amount_base_units == 0:
        return None
    if not _ours(account, transfer.to_address):
        return None
    tx = _tx_key(transfer.network, transfer.tx_hash)
    found = _existing(transfer.network, tx, transfer.log_index)
    if found is not None:
        if declared_invoice is not None and _unattributed(found):
            return _reattribute(found.pk, declared_invoice.pk)
        return found
    try:
        with transaction.atomic():
            return _record(transfer, tx, account, declared_invoice.pk if declared_invoice is not None else None)
    except IntegrityError:
        # The same transfer was recorded meanwhile (a scan and a declaration at once): theirs stands.
        found = _existing(transfer.network, tx, transfer.log_index)
        if found is None:
            raise
        return found


def _ours(account: RailAccount, address: str) -> bool:
    try:
        return bool(account.address) and addresses.normalize(account.kind, address) == account.address
    except ValueError:
        return False


def _tx_key(network: str, tx_hash: str) -> str:
    tx = (tx_hash or "").strip()
    net = NETWORKS.get(network)
    if net is None or net.family not in _HEX_HASH_FAMILIES:
        return tx
    if net.family == "ton" and _TON_B64.match(tx):  # explorers and toncenter show base64: the key is hex
        try:
            return base64.b64decode(tx.replace("-", "+").replace("_", "/").ljust(44, "="), validate=True).hex()
        except ValueError:
            return tx
    if net.family == "tron":  # TronGrid ids carry no 0x; some explorers and wallets add one
        return tx.lower().removeprefix("0x")
    return tx.lower()


def _existing(network: str, tx_hash: str, log_index: int) -> Payment | None:
    return (Payment.objects.only(*PAYMENT_PLAIN_FIELDS)
            .filter(network=network, tx_hash=tx_hash, log_index=log_index).first())


def _unattributed(payment: Payment) -> bool:
    return (payment.rail in CHAIN_KINDS and payment.state == "needs_review" and payment.invoice_id is None
            and payment.review_reason in UNATTRIBUTED)


def _asset(network: str, contract: str) -> Asset | None:
    """The allow-listed asset a transfer moved: by contract, or the native coin for ""."""
    if not contract:
        return next((a for a in NETWORKS[network].assets.values() if not a.contract), None)
    return asset_by_contract(network, contract)


def _received(amount: int, spec: Asset | None) -> Decimal:
    """The amount in whole units, for display (the digit string is the record). Zero for a
    token we know nothing about, and for an amount no allow-listed token's supply allows."""
    if spec is None:
        return Decimal(0)
    value = Decimal(amount).scaleb(-spec.decimals)
    return value if value.adjusted() < 20 else Decimal(0)  # the column holds 20 whole digits


def _record(transfer: Transfer, tx: str, account: RailAccount, declared_pk: int | None) -> Payment:
    spec = _asset(transfer.network, transfer.contract)
    contract = (transfer.contract or "").strip()
    payment = Payment(
        rail=account.kind, rail_account_id=account.pk, network=transfer.network, asset=spec.symbol if spec else "",
        amount_minor=0, amount_base_units=str(transfer.amount_base_units),
        received_amount=_received(transfer.amount_base_units, spec),
        contract=(contract.lower() if account.kind == "evm" else contract)[:128], to_address=account.address,
        from_address=(transfer.from_address or "").strip()[:128], tx_hash=tx, log_index=transfer.log_index,
        block_number=transfer.block_number, block_time=transfer.block_time, final=transfer.final,
    )
    if spec is None:
        decision = _Decision("needs_review", "token_not_allowed")
    elif declared_pk is None and _is_dust(spec, transfer.amount_base_units):
        decision = _Decision("needs_review", "dust")
    else:
        decision = _decide(payment, spec, declared_pk, transfer.comment)
    return _write(payment, decision, timezone.now())


def _is_dust(spec: Asset, amount: int) -> bool:
    """Worth less than `DUST_USD`. Stablecoins count at face value; the native coin at the price
    already in the cache (`fx.price_in` wrote it). This never calls out: `ingest` runs once per
    transfer in the serial worker, and with no cached price nothing is called dust. A transfer a
    client declared is evidence of intent and is never dust."""
    whole = Decimal(amount).scaleb(-spec.decimals)
    if spec.contract:  # the allow-list holds only USD stablecoins with a contract
        return whole < DUST_USD
    price = cache.get(f"fx:{spec.symbol}:USD")
    return price is not None and whole * price.rate < DUST_USD


def _reattribute(payment_pk: int, declared_pk: int) -> Payment:
    """A transfer in review as nobody's, now claimed. The claim came after the transfer was
    recorded, so anyone could have read the hash off the chain first: it never confirms. The
    payment stays in review, attributed to the claimed invoice (and its quote, when one fits)
    with the credit worked out, so staff confirm it in one step. Its reason is the amount rule's
    verdict against that quote, or else the one it had (no window held it, or more than one).
    Left as it is when the claim attributes nothing (another invoice's amount, a rival claim)."""
    with transaction.atomic():
        row = Payment.objects.only(*PAYMENT_PLAIN_FIELDS).get(pk=payment_pk)
        spec = _asset(row.network, row.contract) if row.network in NETWORKS else None
        if spec is None or not _unattributed(row):
            return row
        decision = _decide(row, spec, declared_pk, "")  # locks the invoice before the payment row
        if decision.invoice_id != declared_pk:
            return row  # the claim attributed nothing: ignored, or nobody's
        locked = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment_pk)
        if not _unattributed(locked):
            return locked  # staff acted meanwhile
        review = _Decision("needs_review", decision.outcome or locked.review_reason, decision.invoice_id,
                           decision.quote, decision.invoice, decision.credit, decision.refund)
        return _write(locked, review, timezone.now(), was_reason=locked.review_reason)


def _decide(payment: Payment, spec: Asset, declared_pk: int | None, comment: str) -> _Decision:
    """Attribute the transfer behind `payment` and decide its outcome. Locks the invoice and the
    quote it is attributed to; writes nothing."""
    amount = int(payment.amount_base_units)
    quote, invoice_id, reason, hold = _attribute(payment, spec, amount, declared_pk, comment)
    if quote is None:
        return _Decision("needs_review", reason, invoice_id, outcome=reason)
    invoice = lock_invoice(quote.invoice_id)
    quote = Quote.objects.select_for_update().get(pk=quote.pk)
    reason, credit, refund = _outcome(quote, invoice.status, amount, payment.block_time)
    verdict = reason or hold  # a hold: attributed, credit worked out, but only staff confirm it
    if not payment.final and not hold:  # decided again, with the quote as it then is, once final
        return _Decision("detected", "", invoice.pk, quote, invoice, credit, refund, outcome=reason)
    return _Decision("needs_review" if verdict else "confirmed", verdict, invoice.pk, quote, invoice, credit, refund,
                     outcome=verdict)


@dataclass
class _Candidates:
    """The quotes one transfer could be mistaken for, sorted out once (see `_candidates`).

    `pool` is the transfer's whole amount group, `here` the part it can be attributed to. Every
    comparison across the group is by value (`groups.group_amount`), so 108.27 USDT on BNB Smart
    Chain (18 decimals) is 108.27 USDC on Arbitrum (6)."""
    block_time: datetime.datetime
    value: int  # the transfer at the group's decimals
    pool: list[Quote]  # the group: issued by the block time plus the skew; held, or released lately
    here: list[Quote]  # of the pool, held, on the transfer's own network and in its own asset
    exact: list[Quote]  # of the pool, asking exactly the transfer's value
    holding: list[Quote]  # of the pool, whose window holds the transfer (the exact ones among them)
    fits: list[Quote]  # of `here`, whose window holds it, issued by the block time (no skew)

    def exact_here(self) -> list[Quote]:
        return [q for q in self.exact if q in self.here]


def _candidates(payment: Payment, spec: Asset, amount: int) -> _Candidates:
    """One query over the transfer's amount group (`apps.quotes.groups`): every network and, for
    a USD stablecoin, every USD stablecoin its address takes, since a client who picked the wrong
    one in their exchange still sends the amount they were shown (audit finding MONEY-6).

    A candidate was issued no later than `CLOCK_SKEW_S` after the block, and is held (open,
    expired, cancelled or paid) or was released within `RELEASED_MEMORY_S` of the block: an
    amount handed to the next invoice is still remembered as the first one's (MONEY-3). The skew
    lets a quote stand in a transfer's way, or win it by its exact amount; winning it by its
    window takes a quote issued by the block time, so one asked for after the money moved never
    wins it (MONEY-3, scenario 2)."""
    latest_issue = payment.block_time + datetime.timedelta(seconds=CLOCK_SKEW_S)
    remembered = payment.block_time - datetime.timedelta(seconds=RELEASED_MEMORY_S)
    group = groups.amount_group(payment.network, spec.symbol, payment.to_address)
    pool = list(Quote.objects.filter(amount_group=group, issued_at__lte=latest_issue)
                .filter(Q(state__in=CANDIDATE_STATES) | Q(state="released", closed_at__gte=remembered))
                .order_by("issued_at", "pk"))
    value = groups.group_amount(payment.network, spec.symbol, amount)
    here = [q for q in pool if (q.network, q.asset) == (payment.network, spec.symbol) and q.state != "released"]
    holding = [q for q in pool if _at_value(q, q.min_accept) <= value <= _at_value(q, q.ceiling)]
    return _Candidates(
        block_time=payment.block_time, value=value, pool=pool, here=here,
        exact=[q for q in holding if _at_value(q, q.amount) == value], holding=holding,
        fits=[q for q in here if q in holding and _issued_by(q, payment.block_time)],
    )


def _issued_by(quote: Quote, block_time) -> bool:
    """`quote` was issued no later than `block_time`, judged to the second: every chain stamps
    its blocks in whole seconds, so a quote issued during the block's second counts as issued by
    then. Anything later is a quote asked for after the money moved."""
    return quote.issued_at.replace(microsecond=0) <= block_time


def _at_value(quote: Quote, base_units: int) -> int:
    """`base_units` of the quote's asset on its network, at its group's decimals."""
    return groups.group_amount(quote.network, quote.asset, base_units)


def _invoices(quotes: list[Quote]) -> set[int]:
    return {q.invoice_id for q in quotes}


def _attribute(payment: Payment, spec: Asset, amount: int, declared_pk: int | None,
               comment: str) -> tuple[Quote | None, int | None, str, str]:
    """(quote, invoice pk, review reason, hold): a quote when the transfer is attributed to one,
    otherwise why not, with the invoice a claim named when there was one. A hold is a review
    reason that stands even when the amount is acceptable, because only a claim tied the transfer
    to that quote: a declaration, which anyone who read the hash off the chain can make, chose
    between quotes of different invoices that the amount names equally (`ambiguous`), picked a
    quote while another invoice's quote may be the one paid short (`ambiguous`, see
    `_asked_for_more`), or tied the transfer to a quote whose window did not hold it when it was
    made: a shortfall below the window (`no_matching_invoice`) or a quote issued after it
    (`before_quote`). A TON comment is the payer's own text in the transaction, so it may do all
    of these. A claim never confirms what the amount alone would not.

    A claim may add to what the amount says, never take from it (MONEY-2): a declaration that
    another invoice's amount here contradicts (that invoice's exact amount, or only its windows
    hold the transfer) is ignored, the amount decides as if it had not been made, and the
    declaration stays open for staff. A TON comment that the amount contradicts leaves the
    transfer `ambiguous`: the payer's own two statements disagree."""
    by_comment = _comment_invoice(payment.network, payment.to_address, comment)
    if declared_pk is not None and by_comment is not None and by_comment != declared_pk:
        return None, None, "ambiguous", ""  # two claims that disagree
    claimed = declared_pk if declared_pk is not None else by_comment
    c = _candidates(payment, spec, amount)
    if claimed is not None and (any(q.invoice_id != claimed for q in c.exact_here())
                                or (c.fits and claimed not in _invoices(c.fits))):
        if by_comment is not None:
            return None, None, "ambiguous", ""
        claimed = None  # the amount names another invoice: the declaration is ignored here
    if claimed is None:
        return _by_amount(c)
    mine = [q for q in c.here if q.invoice_id == claimed]
    if mine:
        exact_mine = [q for q in c.exact_here() if q.invoice_id == claimed]
        mine_fit = [q for q in c.fits if q.invoice_id == claimed]
        pick = (exact_mine or mine_fit or mine)[-1]  # exact, else in the window, else the newest
        loose = not exact_mine and not mine_fit and amount <= pick.ceiling  # only the claim ties it
        if loose and _rival_claim(payment, claimed):
            return None, None, "ambiguous", ""  # only a claim ties it, and two invoices make one
        if by_comment == claimed:
            hold = ""  # the payer's own text in the transaction
        elif loose:
            # A shortfall below the window, or a window that opened only after the money moved.
            hold = "no_matching_invoice" if amount < pick.min_accept else "before_quote"
        elif any(q.invoice_id != claimed for q in c.exact):
            hold = "ambiguous"  # the amount is exactly another invoice's too, elsewhere in the group
        elif not exact_mine and any(q.invoice_id != claimed for q in c.holding):
            hold = "ambiguous"  # the claim chose between windows of different invoices
        elif not exact_mine and _asked_for_more(pick, c):
            hold = "ambiguous"  # it may be another invoice's short payment
        else:
            hold = ""
        return pick, claimed, "", hold
    here = Quote.objects.filter(network=payment.network, asset=spec.symbol, address=payment.to_address)
    if here.filter(invoice_id=claimed).exclude(state="released").exists():
        return None, claimed, "before_quote", ""
    return None, claimed, "no_matching_invoice", ""


def _by_amount(c: _Candidates) -> tuple[Quote | None, int | None, str, str]:
    """The decision without a claim. The quote here whose amount the transfer is exactly wins,
    unless the amount is exactly another invoice's anywhere in the group: on another network or
    in another asset (a legacy pair issued before amount groups), or released lately (MONEY-3,
    MONEY-6). Otherwise the one invoice whose window here holds it (a refreshed quote and its
    held predecessor are one invoice: the newest wins), unless another invoice's window anywhere
    in the group holds it too, another invoice's exact amount is elsewhere in the group, or
    another invoice's quote may be the one paid short (`_asked_for_more`, MONEY-1). Every one of
    those is `ambiguous`; no window here at all is `no_matching_invoice`."""
    exact_here = c.exact_here()
    if exact_here:
        if len(_invoices(c.exact)) > 1:
            return None, None, "ambiguous", ""
        return exact_here[-1], exact_here[-1].invoice_id, "", ""
    if not c.fits:
        return None, None, "no_matching_invoice", ""
    pick = c.fits[-1]
    if len(_invoices(c.holding)) > 1 or _asked_for_more(pick, c):
        return None, None, "ambiguous", ""
    return pick, pick.invoice_id, "", ""


def _asked_for_more(pick: Quote, c: _Candidates) -> bool:
    """A quote of another invoice, anywhere in the group and live when the transfer was made,
    asked for more than arrived: the transfer may be its short payment (an exchange kept a fee).

    When the transfer only overpays `pick`, any such quote counts (plan B's ruling C1). When it
    is a short payment of `pick` (inside its allowance), the other quote counts only within two
    allowances (audit finding MONEY-1): two of its own, or two of its organization's
    `underpay_allowance_usd` cap where that is larger (a quote small enough for the share to be
    the smaller allowance still loses a fee of up to twice the cap). The client one cent above
    who lost a typical fee lands in the neighbour's window, while a larger invoice elsewhere on
    the address does not hold up every fee-short payment."""
    overpaid = c.value > _at_value(pick, pick.amount)
    for q in c.pool:
        if q.invoice_id == pick.invoice_id or not _live_at(q, c.block_time):
            continue
        asked = _at_value(q, q.amount)
        gap = asked - c.value
        if gap > 0 and (overpaid or gap <= 2 * (asked - _at_value(q, q.min_accept)) or gap <= 2 * _usd_cap(q)):
            return True
    return False


def _usd_cap(quote: Quote) -> int:
    """The `underpay_allowance_usd` cap of the quote's organization as an amount of its asset, in
    group units: at face value for a USD stablecoin (a hair wider than at its market price, the
    safe side here), at the price already cached for anything else. Never calls out: with no price
    cached, or a setting no window could be built with, it is 0 and the quote's own allowance
    stands."""
    net = NETWORKS.get(quote.network)
    spec = net.assets.get(quote.asset) if net is not None else None
    if spec is None:
        return 0
    if spec.symbol in groups.USD_STABLECOINS:
        price = Decimal(1)
    else:
        cached = cache.get(f"fx:{spec.symbol}:USD")
        if cached is None:
            return 0
        price = cached.rate
    try:
        return _at_value(quote, amounts.usd_allowance(spec, price, _org_settings(quote.invoice_id)))
    except (ValueError, ArithmeticError):
        return 0


def _org_settings(invoice_id: int) -> dict:
    """The settings of the organization that issued `invoice_id`. Plaintext only."""
    org_id = Invoice.objects.filter(pk=invoice_id).values_list("org_id", flat=True).first()
    return Organization.objects.filter(pk=org_id).values_list("settings", flat=True).first() or {}


def _live_at(quote: Quote, block_time) -> bool:
    """`quote` could still be paid when the transfer was made: not past its expiry and not closed
    before it. Judged by the times alone: a released quote keeps when it closed. (Candidates are
    already issued by then, within the skew.)"""
    if block_time > quote.expires_at:
        return False
    return quote.state == "open" or quote.closed_at is None or block_time <= quote.closed_at


def _rival_claim(payment: Payment, claimed: int) -> bool:
    """Another invoice's open declaration names the transaction behind `payment`."""
    others = Declaration.objects.only(*DECLARATION_PLAIN_FIELDS).filter(state="open").exclude(invoice_id=claimed)
    family = NETWORKS[payment.network].family
    if family == "tron":  # a Tron claim may carry a 0x the stored id does not
        others = others.filter(tx_hash__iendswith=payment.tx_hash)
    elif family != "ton":  # a TON claim may be written in base64: compare in Python
        others = others.filter(tx_hash__iexact=payment.tx_hash)
    return any(d.network in ("", payment.network) and _tx_key(payment.network, d.tx_hash) == payment.tx_hash
               for d in others)


def _comment_invoice(network: str, address: str, comment: str) -> int | None:
    """The open invoice whose ISO 11649 reference the comment is, among invoices quoted here.
    None when it is the reference of more than one (numbers are unique only per organization)."""
    ref = "".join((comment or "").split()).upper()
    if not is_valid(ref):
        return None
    quoted = Quote.objects.filter(network=network, address=address).exclude(state="released").values("invoice_id")
    named = [invoice.pk for invoice in
             Invoice.objects.only("pk", "kind", "number").filter(pk__in=quoted, status__in=OPEN_INVOICE)
             if payment_reference(invoice) == ref]
    return named[0] if len(named) == 1 else None


def _outcome(quote: Quote, invoice_status: str, amount: int, block_time) -> tuple[str, int, str]:
    """(review reason, or "" to confirm; credit in invoice minor units; refund due in base units).

    Lateness is judged by the block time alone, against the quote's expiry and the moment it
    closed, so a transfer made in time and final only after the quote expired, after the client
    asked for a fresh quote, or after the quote was released while it waited (audit finding
    MONEY-7), still counts. A released quote that a confirmed payment paid is still paid. An
    invoice already settled or voided is said before lateness: the refund is due because nothing
    was owed, however late the money (E2E-3). The credit is what the quote settles, or for an
    attributed shortfall below the window its share, rounded down."""
    quoted = quote.amount
    if amount >= quote.min_accept:
        credit = quote.invoice_amount_minor
    else:
        credit = amount * quote.invoice_amount_minor // quoted
    refund = str(amount - quoted) if amount > quoted else ""
    closed_before = (quote.state in ("expired", "cancelled", "released") and quote.closed_at is not None
                     and block_time > quote.closed_at)
    if quote.state == "paid" or (quote.state == "released" and _paid_by_a_payment(quote)):
        reason = "duplicate"
    elif invoice_status in CLOSED_INVOICE:
        reason = "invoice_closed"
    elif block_time > quote.expires_at or closed_before:
        reason = "after_expiry"
    elif amount > quote.ceiling:
        reason = "overpaid_beyond_ceiling"
    elif credit <= 0:
        reason = "no_matching_invoice"  # dust: not worth one minor unit of the invoice
    else:
        reason = ""
    return reason, min(credit, MAX_PAYMENT_MINOR), refund


def _paid_by_a_payment(quote: Quote) -> bool:
    """A released quote keeps no `paid` state: a confirmed payment of it is what says it was paid
    (code and staff both mark the quote they confirm). Never true of the payment being decided,
    which is not confirmed yet."""
    return Payment.objects.filter(quote_id=quote.pk, state="confirmed").exists()


def _write(payment: Payment, d: _Decision, now, *, extra_fields=(), was_reason: str | None = None) -> Payment:
    """Save `payment` with decision `d` and do what follows from it: a confirmed payment
    consumes its quote and is applied, a review tells staff, and each is audited."""
    payment.state, payment.review_reason = d.state, d.reason
    payment.invoice_id, payment.quote_id = d.invoice_id, d.quote.pk if d.quote is not None else None
    payment.amount_minor = d.credit
    payment.refund_due_base_units = d.refund if d.state != "detected" else ""
    payment.confirmed_at = now if d.state == "confirmed" else None
    if payment.pk is None:
        payment.save()
    else:
        payment.save(update_fields=[*_DECISION_FIELDS, *extra_fields])
    detail = _detail(payment)
    if was_reason is not None:
        audit.record_system("payment.reattribute", payment, {**detail, "was_reason": was_reason})
    if d.state == "confirmed":
        _consume(d.quote, payment, now)
        audit.record_system("payment.confirm", payment, detail)
        webhooks.payment_event("payment.confirmed", payment)
        apply_payments(d.invoice)
        match_declarations(payment)
        logger.info("payment #%s confirmed for invoice #%s", payment.pk, payment.invoice_id)
    elif d.state == "needs_review":
        audit.record_system("payment.review", payment, detail)
        if payment.review_reason != "dust":  # spam is listed, collapsed; nobody is told about it
            _tell_staff_review(payment)
            webhooks.payment_event("payment.needs_review", payment)
        logger.info("payment #%s needs review (%s)", payment.pk, payment.review_reason)
    else:
        audit.record_system("payment.detect", payment, detail)
        webhooks.payment_event("payment.detected", payment)
        logger.info("payment #%s detected for invoice #%s", payment.pk, payment.invoice_id)
    return payment


def _detail(p: Payment) -> dict:
    return {
        "payment": p.pk, "invoice": p.invoice_id, "quote": p.quote_id, "rail": p.rail, "network": p.network,
        "asset": p.asset, "tx_hash": p.tx_hash, "log_index": p.log_index, "external_id": p.external_id,
        "amount_base_units": p.amount_base_units, "amount_minor": p.amount_minor, "state": p.state,
        "reason": p.review_reason, "refund_due_base_units": p.refund_due_base_units, "final": p.final,
    }


def _consume(quote: Quote, payment: Payment, now) -> None:
    """The quote a payment was confirmed against is `paid` before the payment is applied (so
    `apply_payments` cancels only the others), a partial payment's too: a second transfer
    against it is then exactly a duplicate."""
    unpaid = Quote.objects.filter(pk=quote.pk, state__in=("open", "expired", "cancelled"))
    if unpaid.update(state="paid", closed_at=now):
        quote.state, quote.closed_at = "paid", now
        audit.record_system("quote.paid", quote, {"quote": quote.pk, "invoice": quote.invoice_id,
                                                  "payment": payment.pk})


def match_declarations(payment: Payment) -> None:
    """A client's open claim naming the transaction behind this confirmed payment is borne out.
    (`apply_payments` matches every open claim when the invoice is settled; a partial payment
    needs this, whether code confirmed it or staff did.) Plaintext only."""
    if not payment.tx_hash or payment.invoice_id is None:
        return
    claims = Declaration.objects.only(*DECLARATION_PLAIN_FIELDS).filter(invoice_id=payment.invoice_id, state="open")
    pks = [d.pk for d in claims.exclude(tx_hash="")
           if d.network in ("", payment.network) and _tx_key(payment.network, d.tx_hash) == payment.tx_hash]
    if pks and Declaration.objects.filter(pk__in=pks, state="open").update(state="matched"):
        for pk in pks:
            audit.record_system("declaration.matched", Declaration(pk=pk),
                                {"invoice": payment.invoice_id, "payment": payment.pk})


def _tell_staff_review(payment: Payment) -> None:
    plain = {"payment_id": payment.pk, "reason": payment.review_reason, "network": payment.network,
             "amount_base_units": payment.amount_base_units, "asset": payment.asset}
    if payment.rail in GATEWAY_KINDS:
        plain["received_amount"] = _plain_decimal(payment.received_amount)
    _tell_staff("review_staff", payment, plain)


def _tell_staff(kind: str, payment: Payment, plain: dict) -> None:
    from apps.notify import services as notify

    notify.enqueue_addressed_later(kind, recipient="staff", plain_context=plain,
                                   invoice=Invoice(pk=payment.invoice_id) if payment.invoice_id else None)


def _plain_decimal(value) -> str:
    return format(Decimal(value), "f")


# ---------------------------------------------------------------------------
# Finality
# ---------------------------------------------------------------------------
def _watched(payment: Payment) -> bool:
    """A chain payment whose transfer may still change: detected, or in review but not final."""
    return payment.rail in CHAIN_KINDS and (
        payment.state == "detected" or (payment.state == "needs_review" and not payment.final))


def settle_if_final(payment: Payment, status: str) -> Payment:
    """Apply what the chain says about the transfer behind `payment`.

    `final`: a detected payment is decided against its quote as it now is (a non-final review
    payment only becomes final). `pending`: unchanged. `dropped`: rejected, but only once the
    chain has said so twice, at least `DROPPED_AFTER_S` apart, with no other answer between;
    one missing answer from one node is not a vanished transaction. `check_attempts` counts the
    dropped answers in a row and `last_checked_at` is when that run began (or the last answer
    that was not "dropped"). Anything not watched is returned unchanged.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown chain status {status!r}")
    now = timezone.now()
    with transaction.atomic():
        plain = Payment.objects.only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        if not _watched(plain):
            return plain
        deciding = status == "final" and plain.state == "detected"
        invoice = lock_invoice(plain.invoice_id) if deciding else None  # the invoice before the payment
        row = Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)
        if not _watched(row) or (deciding and (row.state, row.invoice_id) != ("detected", invoice.pk)):
            return row
        if status == "dropped":
            return _dropped(row, now)
        row.check_attempts, row.last_checked_at = 0, now
        if status == "pending":
            row.save(update_fields=["check_attempts", "last_checked_at"])
            return row
        row.final = True
        if not deciding:  # in review already: it stays there, now final
            row.save(update_fields=["final", "check_attempts", "last_checked_at"])
            audit.record_system("payment.final", row, _detail(row))
            return row
        quote = Quote.objects.select_for_update().filter(pk=row.quote_id).first()
        if quote is None:  # a detected chain payment always has one; never confirm without it
            decision = _Decision("needs_review", "no_matching_invoice", invoice.pk)
        else:
            reason, credit, refund = _outcome(quote, invoice.status, int(row.amount_base_units), row.block_time)
            decision = _Decision("needs_review" if reason else "confirmed", reason, invoice.pk, quote, invoice,
                                 credit, refund)
        return _write(row, decision, now, extra_fields=["final", "check_attempts", "last_checked_at"])


def _dropped(row: Payment, now) -> Payment:
    if row.check_attempts < 1 or row.last_checked_at is None:
        row.check_attempts, row.last_checked_at = 1, now
        row.save(update_fields=["check_attempts", "last_checked_at"])
        return row
    if now - row.last_checked_at < datetime.timedelta(seconds=DROPPED_AFTER_S):
        Payment.objects.filter(pk=row.pk).update(check_attempts=F("check_attempts") + 1)
        row.check_attempts += 1
        return row
    row.state, row.review_reason, row.check_attempts = "rejected", "dropped", row.check_attempts + 1
    row.save(update_fields=["state", "review_reason", "check_attempts"])
    audit.record_system("payment.reject", row, _detail(row))
    _tell_staff_review(row)  # the client may have seen "payment seen": staff should know
    webhooks.payment_event("payment.rejected", row)
    logger.warning("payment #%s rejected (dropped)", row.pk)
    return row


def refresh_finality() -> int:
    """Ask the chain about every chain payment still waiting on it; returns how many changed
    state. A chain that cannot answer leaves its payments as they are and is recorded in
    `WatcherHealth` as `finality:<network>`; so is an unexpected error on one row, which is
    logged by its type and never stops the rows after it."""
    rows = list(
        Payment.objects.only(*PAYMENT_PLAIN_FIELDS).filter(rail__in=CHAIN_KINDS)
        .filter(Q(state="detected") | Q(state="needs_review", final=False)).order_by("pk")
    )
    if ton_blocked("ton"):  # TON payments stay as they are until TON_ENABLED is on
        rows = [row for row in rows if row.rail != "ton"]
    changed, failed, looked = 0, {}, set()
    for row in rows:
        try:
            status = adapter_for(row.network).status(row)
        except (http.Unavailable, http.Refused, LookupError) as exc:
            failed.setdefault(row.network, _failure_kind(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rows after it
            failed.setdefault(row.network, _unexpected(f"the finality check of payment #{row.pk}", exc))
            continue
        looked.add(row.network)
        try:
            if settle_if_final(row, status).state != row.state:
                changed += 1
        except Exception as exc:  # noqa: BLE001 - likewise; the next pass asks again
            failed.setdefault(row.network, _unexpected(f"settling payment #{row.pk}", exc))
    for network, kind in failed.items():
        health.failed(f"finality:{network}", kind)
    for network in looked - failed.keys():
        health.ok(f"finality:{network}")
    return changed


def _unexpected(what: str, exc: Exception) -> str:
    """Log an error nobody planned for by its type name only (its message may carry a hash or an
    address), and return that name as the watcher's error kind."""
    kind = type(exc).__name__
    logger.error("%s failed (%s)", what, kind)
    return kind


def _failure_kind(exc: Exception) -> str:
    if isinstance(exc, http.Unavailable):
        return exc.kind
    if isinstance(exc, http.Refused):
        return str(exc.status)
    return "no_adapter"


# ---------------------------------------------------------------------------
# Declarations: the client named a transaction
# ---------------------------------------------------------------------------
def verify_declaration(declaration: Declaration) -> Payment | None:
    """Look up the transaction an open declaration names and ingest its transfers to the watched
    addresses of the declared invoice's organization on that network, with the declared invoice
    as the claim. Returns the payment for the declared invoice (else the first one found), or
    None when nothing reached those addresses.

    A transfer to any other address, another organization's included, is ignored. When the
    chain cannot be asked, `verify_attempts` is counted and `http.Unavailable` raised: could not
    look, never "not paid".
    """
    d = Declaration.objects.only(*DECLARATION_PLAIN_FIELDS).get(pk=declaration.pk)
    tx = (d.tx_hash or "").strip()
    if d.state != "open" or not tx or d.network not in NETWORKS or ton_blocked(NETWORKS[d.network].family):
        return None  # TON: off until its credit leg is verified; the claim stays for staff
    try:
        transfers = adapter_for(d.network).transfers_in_tx(d.network, tx)
    except (http.Unavailable, http.Refused, LookupError) as exc:
        Declaration.objects.filter(pk=d.pk).update(verify_attempts=F("verify_attempts") + 1)
        logger.info("declaration #%s could not be looked up (%s)", d.pk, type(exc).__name__)
        if isinstance(exc, http.Unavailable):
            raise
        raise http.Unavailable(f"chain:{d.network}", _failure_kind(exc)) from None
    invoice = Invoice.objects.only(*INVOICE_PLAIN_FIELDS).get(pk=d.invoice_id)
    family = NETWORKS[d.network].family
    # Only the declaring invoice's own organization's addresses: a transfer to another
    # organization's address is that organization's to find, never this claim's to attribute.
    # A switched-off account still counts while a quote of it is unreleased (audit E2E-4).
    accounts = [a for a in RailAccount.objects.watched().only(*_ACCOUNT_FIELDS)
                .filter(kind=family, org_id=invoice.org_id).order_by("pk")
                if d.network in (a.networks or [])]
    found = []
    for t in transfers:
        account = _receiving_account(accounts, family, t.to_address) if t.network == d.network else None
        payment = ingest(t, account=account, declared_invoice=invoice) if account is not None else None
        if payment is not None:
            found.append(payment)
    if not found:
        return None
    Declaration.objects.filter(pk=d.pk).update(verified_at=timezone.now())
    result = next((p for p in found if p.invoice_id == invoice.pk), found[0])
    if result.invoice_id == invoice.pk and result.state == "confirmed":
        if Declaration.objects.filter(pk=d.pk, state="open").update(state="matched"):
            audit.record_system("declaration.matched", d, {"invoice": invoice.pk, "payment": result.pk})
    logger.info("declaration #%s found payment #%s", d.pk, result.pk)
    return result


def _receiving_account(accounts: list[RailAccount], family: str, address: str) -> RailAccount | None:
    try:
        normalized = addresses.normalize(family, address)
    except ValueError:
        return None
    return next((a for a in accounts if a.address == normalized), None)


def verify_open_declarations(now=None) -> int:
    """Look up open declarations that name a hash on a known network, until found or a day old
    (then they stay open for staff): of each invoice only the newest such one, and at most
    `DECLARATIONS_PER_PASS` a pass, oldest first (audit WEB-1: the job runs in the one serial
    worker, ahead of the chain scans). Returns how many found a payment. A chain that cannot
    answer is recorded in `WatcherHealth` as `declarations:<network>` and asked again; so is an
    unexpected error on one declaration, which never stops the ones after it."""
    now = now or timezone.now()
    since = now - datetime.timedelta(seconds=DECLARATION_LOOKUP_S)
    candidates = (Declaration.objects.filter(state="open", verified_at__isnull=True, created_at__gte=since,
                                             network__in=list(NETWORKS))
                  .exclude(tx_hash=""))
    newest = candidates.order_by().values("invoice_id").annotate(newest=Max("pk")).values("newest")
    rows = list(
        Declaration.objects.filter(pk__in=newest).order_by("created_at", "pk")
        .values_list("pk", "network")[:DECLARATIONS_PER_PASS]
    )
    found, failed, looked = 0, {}, set()
    for pk, network in rows:
        try:
            if verify_declaration(Declaration(pk=pk)) is not None:
                found += 1
        except http.Unavailable as exc:
            failed.setdefault(network, exc.kind)
            continue
        except Exception as exc:  # noqa: BLE001 - oldest first: one bad row must not block every later one
            failed.setdefault(network, _unexpected(f"looking up declaration #{pk}", exc))
            continue
        looked.add(network)
    for network, kind in failed.items():
        health.failed(f"declarations:{network}", kind)
    for network in looked - failed.keys():
        health.ok(f"declarations:{network}")
    return found


# ---------------------------------------------------------------------------
# Gateway payments (BTCPay, Lightning)
# ---------------------------------------------------------------------------
def record_gateway_payment(quote: Quote, *, network: str, external_id: str, received_amount: Decimal,
                           amount_minor: int, state: str, review_reason: str = "",
                           refund_due_base_units: str = "", needs_staff_confirmation: bool = False) -> Payment:
    """Record what a gateway reports for `quote`. Idempotent on `(rail, external_id)`: asking
    again returns the same payment, and only a `detected` payment moves on (to `confirmed` or
    `needs_review`, or to waiting for staff). A confirmed payment consumes its quote and is
    applied, unless the quote was paid by another payment (`duplicate`) or the invoice is closed
    (`invoice_closed`): those go to review. `needs_staff_confirmation` e-mails staff (no gateway sets it since 2026-11-09)."""
    received = _check_gateway_args(network, external_id, received_amount, amount_minor, state, review_reason,
                                   refund_due_base_units, needs_staff_confirmation)
    now = timezone.now()
    with transaction.atomic():
        plain = Quote.objects.get(pk=quote.pk)
        account = RailAccount.objects.only(*_ACCOUNT_FIELDS).get(pk=plain.rail_account_id)
        if account.kind not in GATEWAY_KINDS:
            raise ValueError("a gateway payment needs a BTCPay or Lightning quote")
        invoice = lock_invoice(plain.invoice_id)
        locked = Quote.objects.select_for_update().get(pk=plain.pk)
        settled = state == "confirmed"  # what the gateway says, whatever is then made of it
        if state == "confirmed" and locked.state == "paid":
            state, review_reason = "needs_review", "duplicate"
        elif state == "confirmed" and invoice.status in CLOSED_INVOICE:
            state, review_reason = "needs_review", "invoice_closed"
        elif needs_staff_confirmation and invoice.status in CLOSED_INVOICE:
            # A person would confirm it, and confirming on a closed invoice is refused: it goes to
            # review now, where staff can assign it elsewhere.
            state, review_reason, needs_staff_confirmation = "needs_review", "invoice_closed", False
        decision = _Decision(state, review_reason, invoice.pk, locked, invoice, amount_minor,
                             refund_due_base_units)
        row = (Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS)
               .filter(rail=account.kind, external_id=external_id).first())
        if row is not None:
            return _advance_gateway(row, decision, received, needs_staff_confirmation, settled, now)
        payment = Payment(rail=account.kind, rail_account_id=account.pk, network=network, asset=locked.asset,
                          external_id=external_id, received_amount=received, final=settled,
                          needs_staff_confirmation=needs_staff_confirmation, amount_minor=0)
        try:
            with transaction.atomic():
                _write(payment, decision, now)
                if needs_staff_confirmation:
                    _tell_staff_confirm(payment)
                return payment
        except IntegrityError:
            found = (Payment.objects.only(*PAYMENT_PLAIN_FIELDS)
                     .filter(rail=account.kind, external_id=external_id).first())
            if found is None:
                raise
            return found


def flag_gateway_invalidated(quote: Quote, *, external_id: str, reason: str) -> bool:
    """The gateway now calls a payment it had settled invalid (BTCPay `Invalid` after `Settled`).

    Money that was counted may not be there, but only a person can tell: the payment stays
    confirmed and the invoice as it is, and staff are told once (`gateway_invalid_at`) by the
    review letter, a `staff.alert` event and the audit log. They reverse the payment from the
    invoice, or keep it from the needs-a-person page (audit MONEY-4). True when `external_id`
    names a confirmed payment of this quote's rail (told now or before), False when there is
    none: the caller then records the report as it would any other. Plaintext only."""
    if reason not in REVIEW_REASONS:
        raise ValueError("a known review reason is needed")
    with transaction.atomic():
        kind = RailAccount.objects.filter(pk=quote.rail_account_id).values_list("kind", flat=True).first()
        if kind not in GATEWAY_KINDS:
            raise ValueError("a gateway payment needs a BTCPay or Lightning quote")
        row = (Payment.objects.select_for_update().only(*PAYMENT_PLAIN_FIELDS)
               .filter(rail=kind, external_id=external_id).first())
        if row is None or row.state != "confirmed":
            return False
        if row.gateway_invalid_at is not None:
            return True
        row.gateway_invalid_at = timezone.now()
        row.save(update_fields=["gateway_invalid_at"])
        audit.record_system("payment.gateway_invalid", row, {**_detail(row), "reason": reason})
        plain = {"payment_id": row.pk, "reason": reason, "network": row.network,
                 "amount_base_units": row.amount_base_units, "asset": row.asset,
                 "received_amount": _plain_decimal(row.received_amount), "confirmed": True}
        _tell_staff("review_staff", row, plain)
        org_id = Invoice.objects.filter(pk=row.invoice_id).values_list("org_id", flat=True).first()
        webhooks.staff_alert({"kind": "payment_invalid_after_confirmation", "payment_id": row.pk,
                              "invoice_id": row.invoice_id, "reason": reason}, org=org_id,
                              invoice=Invoice(pk=row.invoice_id))
    logger.warning("payment #%s was confirmed and the gateway now calls it invalid: a person must look (%s)",
                   row.pk, reason)
    return True


def gateway_ceiling(quote: Quote, due: int) -> int:
    """The most a gateway may report as paid for `quote` (`due` and the answer in the same unit)
    before it is overpaid beyond the organisation's `overpay_ceiling_bps`: beyond it a gateway
    payment goes to review (`overpaid_beyond_ceiling`), as a chain transfer does. Plaintext only."""
    return amounts.overpay_ceiling(due, _org_settings(quote.invoice_id))


def _advance_gateway(row: Payment, d: _Decision, received: Decimal, needs_staff_confirmation: bool, settled: bool,
                     now) -> Payment:
    if row.state != "detected":
        return row  # decided already: a later report never undoes it
    if row.needs_staff_confirmation and d.state == "confirmed":
        # Waiting for a person (XMR, ZEC): no gateway report confirms it. Left as it is.
        audit.record_system("payment.gateway_ignored", row, {**_detail(row), "code": STAFF_CONFIRMATION_REQUIRED})
        logger.info("payment #%s waits for staff; gateway confirmation ignored (%s)", row.pk,
                    STAFF_CONFIRMATION_REQUIRED)
        return row
    if d.state == "detected":
        if needs_staff_confirmation and not row.needs_staff_confirmation:
            row.needs_staff_confirmation, row.received_amount, row.amount_minor = True, received, d.credit
            row.save(update_fields=["needs_staff_confirmation", "received_amount", "amount_minor"])
            audit.record_system("payment.await_staff", row, _detail(row))
            _tell_staff_confirm(row)
        return row
    row.received_amount, row.needs_staff_confirmation, row.final = received, False, settled
    return _write(row, d, now, extra_fields=["received_amount", "needs_staff_confirmation", "final"])


def _tell_staff_confirm(payment: Payment) -> None:
    logger.info("payment #%s waits for staff confirmation", payment.pk)
    _tell_staff("confirm_staff", payment, {"payment_id": payment.pk, "network": payment.network,
                                           "asset": payment.asset,
                                           "received_amount": _plain_decimal(payment.received_amount)})


def _check_gateway_args(network, external_id, received_amount, amount_minor, state, review_reason,
                        refund_due_base_units, needs_staff_confirmation) -> Decimal:
    if state not in GATEWAY_STATES:
        raise ValueError(f"a gateway payment is {', '.join(GATEWAY_STATES)}, not {state!r}")
    if not isinstance(network, str) or not network:
        raise ValueError("a gateway payment names its network")
    if not isinstance(external_id, str) or not external_id or len(external_id) > 128:
        raise ValueError("a gateway payment needs the gateway's id (1 to 128 characters)")
    if (state == "needs_review") != bool(review_reason) or (review_reason and review_reason not in REVIEW_REASONS):
        raise ValueError("a payment for review needs a known reason, and only it has one")
    if needs_staff_confirmation and state != "detected":
        raise ValueError("only a detected payment waits for staff confirmation")
    if (not isinstance(amount_minor, int) or isinstance(amount_minor, bool)
            or not 0 <= amount_minor <= MAX_PAYMENT_MINOR):
        raise ValueError("the credit is a whole number of minor units within the payment cap")
    if state == "confirmed" and amount_minor == 0:
        raise ValueError("a confirmed payment credits something")
    if refund_due_base_units and not (isinstance(refund_due_base_units, str) and _DIGITS.match(refund_due_base_units)):
        raise ValueError("a refund due is digits only, or empty")
    if isinstance(received_amount, (float, bool)):
        raise ValueError("an amount received is never a float")
    try:
        received = Decimal(received_amount)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("the amount received is not a number") from None
    if not received.is_finite() or received < 0 or received.adjusted() >= 20:
        raise ValueError("the amount received must be a non-negative number that fits")
    return received
