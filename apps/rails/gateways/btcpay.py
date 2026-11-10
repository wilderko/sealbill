"""BTCPay Server (Greenfield API) as a gateway for BTC on-chain, Monero and Zcash.

Each quote gets its own BTCPay invoice. What BTCPay tells us is read again from its API, never
taken from a webhook body. XMR and ZEC are never confirmed by code: BTCPay's `Settled` leaves
them waiting for staff. Logs carry the account pk and error kinds only: no amounts, addresses,
invoice ids or URLs.

BTCPay settles after the speed policy's confirmations, never at 0 (`BTCPAY_SPEED_POLICIES`), and
a settled invoice can still turn `Invalid` (a double spend, a reorg, a payment that never
confirmed): a quote is read again for `INVALID_WATCH_WINDOW` after its payment was confirmed,
and an `Invalid` then goes to a person. Nothing is reversed by code (audit MONEY-4).
"""

import datetime
import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from apps.invoices.currencies import minor_units
from apps.payments import matching
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails import health, http
from apps.rails.gateways import idle_probe, registry
from apps.rails.gateways.rotation import rotated
from apps.rails.models import BTCPAY_SPEED_POLICIES, RailAccount
from apps.vault.vault import VaultSealed, vault

logger = logging.getLogger(__name__)

SERVICE = "btcpay"
METHOD_NETWORKS = {"BTC-CHAIN": "bitcoin", "XMR-CHAIN": "monero", "ZEC-CHAIN": "zcash"}
DECIMALS = {"BTC": 8, "XMR": 12, "ZEC": 8}
LATE_POLL_WINDOW = datetime.timedelta(hours=24)
# How long BTCPay may still call a settled invoice `Invalid`: it watches a payment for its
# "monitoring expiration" (a day by default) and a reorg can come later still.
INVALID_WATCH_WINDOW = datetime.timedelta(hours=72)
SPEED_POLICY_KIND = "speed_policy"  # the watcher's kind while the account's speed_policy is not allowed


def confirmed_recently(now) -> Q:
    """The quotes whose BTCPay payment was confirmed (by code, or by staff for XMR/ZEC) in the
    last `INVALID_WATCH_WINDOW`, whatever the quote's state since: a paid quote is released a day
    after it closes. Plaintext only."""
    from apps.payments.models import Payment

    recent = Payment.objects.filter(rail="btcpay", state="confirmed", quote__isnull=False,
                                    confirmed_at__gte=now - INVALID_WATCH_WINDOW).values("quote_id")
    return Q(pk__in=recent)


def key_permissions(base_url: str, api_key: str) -> list[str]:
    """The permissions BTCPay lists for `api_key` itself (`GET /api/v1/api-keys/current`), each a
    name optionally scoped to a store (`btcpay.store.canviewinvoices:<store id>`). Raises
    `http.Unavailable` / `http.Refused` like every call here, and Unavailable for an answer of
    another shape."""
    data = http.get_json(f"{str(base_url).rstrip('/')}/api/v1/api-keys/current",
                         headers={"Authorization": f"token {api_key}", "Accept": "application/json"},
                         service=SERVICE, guard=http.STAFF_URL)
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise _bad(ValueError("permissions"))
    return permissions


def _major(minor: int, currency: str) -> str:
    return format(Decimal(minor).scaleb(-minor_units(currency)), "f")


def _dec(value) -> Decimal:
    d = Decimal(str(value))
    if not d.is_finite() or d < 0:
        raise ValueError("not a non-negative number")
    return d


def _base_units(amount: Decimal, asset: str) -> int:
    scaled = amount.scaleb(DECIMALS[asset])
    if scaled != scaled.to_integral_value():
        raise ValueError("not a whole number of base units")
    return int(scaled)


def _rounded_units(amount: Decimal, asset: str, rounding) -> int:
    return int(amount.scaleb(DECIMALS[asset]).to_integral_value(rounding=rounding))


def _bad(exc) -> http.Unavailable:
    logger.warning("btcpay answered something unreadable (%s)", type(exc).__name__)
    return http.Unavailable(SERVICE, "bad_answer")


class BTCPayGateway:
    def __init__(self, account):
        try:
            creds = account.credentials or {}
        except VaultSealed:
            raise QuoteError("rail_unavailable") from None
        cfg = account.config or {}
        self.account_pk = account.pk
        self.base = str(cfg.get("url", "")).rstrip("/")
        self.store = str(cfg.get("store_id", ""))
        self.speed = cfg.get("speed_policy") or "MediumSpeed"
        self._key = creds.get("api_key", "")
        if not (self.base and self.store and self._key):
            raise QuoteError("rail_unavailable")

    # -- wire
    def _headers(self):
        return {"Authorization": f"token {self._key}", "Accept": "application/json"}

    def _url(self, *parts):
        return "/".join([self.base, "api/v1/stores", self.store, "invoices", *parts])

    def create_target(self, quote) -> None:
        if self.speed not in BTCPAY_SPEED_POLICIES:
            # Saved before validation refused it: no new invoice settles at 0 confirmations. The
            # account's existing quotes are still read; Needs a person says why no quote is made.
            logger.warning("btcpay account %s has a speed_policy that is not allowed; no new invoice",
                           self.account_pk)
            try:
                health.failed(f"btcpay:{self.account_pk}", SPEED_POLICY_KIND)
            except Exception as exc:  # noqa: BLE001 - the refusal stands; its bookkeeping must not become a 500
                logger.error("recording btcpay account %s as failing did not work (%s)", self.account_pk,
                             type(exc).__name__)
            raise QuoteError("rail_unavailable")
        inv = quote.invoice
        meta = {"orderId": f"q{quote.pk}" if quote.pk else f"i{inv.pk}", "invoicePk": inv.pk}
        if quote.pk:
            meta["quotePk"] = quote.pk
        body = {"amount": _major(quote.invoice_amount_minor, inv.currency), "currency": inv.currency,
                "metadata": meta,
                "checkout": {"expirationMinutes": 30, "speedPolicy": self.speed, "paymentMethods": [quote.asset]}}
        created = http.post_json(self._url(), body, headers=self._headers(), service=SERVICE, guard=http.STAFF_URL)
        try:
            external_id, checkout = str(created["id"]), str(created["checkoutLink"])
            if not external_id:
                raise ValueError("no id")
        except (KeyError, TypeError, ValueError) as exc:
            raise _bad(exc) from None
        methods = http.get_json(self._url(external_id, "payment-methods"),
                                headers=self._headers(), service=SERVICE, guard=http.STAFF_URL)
        try:
            method = self._method_for(methods, quote.network, quote.asset)
            amount = _base_units(_dec(method["amount"]), quote.asset)
            if amount <= 0:
                raise ValueError("zero amount")
            link, rate = str(method["paymentLink"]), str(method["rate"])
            _dec(rate)
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as exc:
            raise _bad(exc) from None
        quote.external_id, quote.checkout_url, quote.payment_uri = external_id, checkout[:500], link
        quote.amount_base_units, quote.rate, quote.rate_source = str(amount), rate[:64], "btcpay"
        expiry = created.get("expirationTime")
        if isinstance(expiry, int) and not isinstance(expiry, bool):
            at = datetime.datetime.fromtimestamp(expiry, datetime.timezone.utc)
            if at < quote.expires_at:
                quote.expires_at = at

    @staticmethod
    def _method_for(methods, network, asset):
        for m in methods:
            if METHOD_NETWORKS.get(m.get("paymentMethodId")) == network:
                return m
        raise ValueError("no payment method for this quote")

    # -- poll
    def _read(self, quote):
        eid = quote.external_id
        inv = http.get_json(self._url(eid), headers=self._headers(), service=SERVICE, guard=http.STAFF_URL)
        methods = http.get_json(self._url(eid, "payment-methods"),
                                headers=self._headers(), service=SERVICE, guard=http.STAFF_URL)
        try:
            if not isinstance(inv, dict) or not isinstance(methods, list) or not methods:
                raise ValueError("shape")
            # `totalPaid` is everything paid through any method, converted; `paymentMethodPaid` is
            # what arrived through this one. Only the latter says which coin was sent.
            ours = f"{quote.asset}-CHAIN"
            ids = [m.get("paymentMethodId") for m in methods]
            wanted = next((m for m in methods if m.get("paymentMethodId") == ours), None)
            foreign = [m for m in methods if m is not wanted and _dec(m.get("paymentMethodPaid") or 0) > 0]
            mismatch = ids != [ours] or bool(foreign)
            method = foreign[0] if foreign else (wanted or methods[0])
            return (str(inv["status"]), str(inv.get("additionalStatus") or "None"),
                    METHOD_NETWORKS.get(method.get("paymentMethodId"), ""), mismatch,
                    _dec(method.get("paymentMethodPaid") or 0), _dec(method["amount"]))
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as exc:
            raise _bad(exc) from None

    def probe(self) -> bool:
        """One read-only call that shows whether BTCPay answers (`idle_probe`): this store's newest
        invoice, which the key is known to read. False when the store has none yet."""
        newest = (Quote.objects.filter(rail_account_id=self.account_pk).exclude(external_id="")
                  .order_by("-pk").values_list("external_id", flat=True).first())
        if not newest:
            return False
        http.get_json(self._url(newest), headers=self._headers(), service=SERVICE, guard=http.STAFF_URL)
        return True

    def poll(self, quote) -> None:
        status, extra, network, mismatch, paid, due = self._read(quote)
        full = quote.invoice_amount_minor
        if due > 0 and paid < due:
            credit = int((Decimal(full) * paid / due).to_integral_value(rounding=ROUND_FLOOR))
        else:
            credit = full
        external_id = f"btcpay:{self.store}:{quote.external_id}"
        record = dict(network=network or quote.network, external_id=external_id, received_amount=paid)

        def review(reason):
            return matching.record_gateway_payment(quote, state="needs_review", review_reason=reason,
                                                   amount_minor=credit, **record)

        if status == "Invalid" and matching.flag_gateway_invalidated(quote, external_id=external_id,
                                                                     reason="btcpay_invalid"):
            return  # confirmed already: a person decides, nothing is undone here
        if extra == "Marked":
            review("btcpay_marked")
        elif extra == "PaidLate":
            review("btcpay_late")
        elif status == "Invalid" or (status in ("Processing", "Settled") and mismatch):
            review("btcpay_invalid")  # a payment on another coin or network is never confirmed
        elif status == "Expired" and extra == "PaidPartial":
            review("btcpay_partial")
        elif status == "Settled" and paid < due:
            review("btcpay_partial")  # the store's tolerance does not override the organisation's allowance
        elif status == "Processing":
            matching.record_gateway_payment(quote, state="detected", amount_minor=credit, **record)
        elif status == "Settled":
            # Every coin alike (owner, 2026-11-09): BTCPay calls an invoice Settled once the store's
            # confirmation threshold is met (XMR 10, ZEC 6 on 2026-11-09), which is the chain's proof.
            # Monero and Zcash used to wait for a person here.
            asset = quote.asset
            over = str(int((paid - due).scaleb(DECIMALS[asset]).to_integral_value(rounding=ROUND_FLOOR)))
            if _rounded_units(paid, asset, ROUND_FLOOR) > matching.gateway_ceiling(quote, _rounded_units(due, asset, ROUND_CEILING)):
                # Overpaid beyond the organisation's ceiling, whatever BTCPay's own flag says.
                matching.record_gateway_payment(quote, state="needs_review", review_reason="overpaid_beyond_ceiling",
                                                amount_minor=full, refund_due_base_units=over, **record)
                return
            refund = over if extra == "PaidOver" and paid > due else ""
            matching.record_gateway_payment(quote, state="confirmed", amount_minor=full,
                                            refund_due_base_units=refund, **record)
        elif status == "Expired" and paid == 0:
            Quote.objects.filter(pk=quote.pk, state="open").update(state="expired", closed_at=timezone.now())


def factory(account):
    return BTCPayGateway(account)


POLL_SECONDS = 60  # the period of the `btcpay_poll` job


def btcpay_poll() -> int:
    """Re-read BTCPay for every quote that may have news. Returns the quotes looked at.

    That is every open quote, every quote closed in the last day, a quote whose payment was
    confirmed in the last 72 hours (`confirmed_recently`: BTCPay may still call it Invalid), and
    whatever its age a quote whose payment BTCPay has seen but not settled, or which has not been
    read since it expired (`gateway_money_may_be_coming`). Silent while the vault is sealed (the
    sealed alert covers it). An unreachable BTCPay is recorded as a failing watcher and leaves
    every quote exactly as it was; only a successful read counts as one (`Quote.read_at`)."""
    from apps.quotes.models import Quote
    from apps.quotes.services import gateway_money_may_be_coming
    if not vault.is_unsealed():
        return 0
    now = timezone.now()
    candidates = Quote.objects.filter(rail_account__kind="btcpay").exclude(external_id="").filter(
        Q(check_soon=True) | Q(state="open") | Q(state="expired", expires_at__gte=now - LATE_POLL_WINDOW)
        | Q(state="cancelled", closed_at__gte=now - LATE_POLL_WINDOW) | gateway_money_may_be_coming()
        | confirmed_recently(now))
    by_account: dict[int, list[int]] = {}
    for pk, account_pk in candidates.order_by("pk").values_list("pk", "rail_account_id"):
        by_account.setdefault(account_pk, []).append(pk)
    looked = 0
    for account_pk, pks in by_account.items():
        name = f"btcpay:{account_pk}"
        failure = None
        unavailable_in_a_row = 0
        try:
            account = RailAccount.objects.get(pk=account_pk)
            gateway = registry.for_account(account)
        except (QuoteError, VaultSealed):
            if not vault.is_unsealed():
                return looked  # sealed mid-pass: the sealed alert covers it
            health.failed(name, "unconfigured")
            continue
        for pk in rotated(pks, POLL_SECONDS, now):
            quote = Quote.objects.get(pk=pk)
            try:
                gateway.poll(quote)
                Quote.objects.filter(pk=pk).update(check_soon=False, read_at=timezone.now())
                looked += 1
                unavailable_in_a_row = 0
            except VaultSealed:
                return looked
            except http.Unavailable as exc:
                failure = exc.kind
                # One failing lookup must not hide the quotes behind it; a server that is
                # really down is asked three times per pass, not once per quote. The next
                # pass starts elsewhere.
                unavailable_in_a_row += 1
                if unavailable_in_a_row >= 3:
                    break
            except http.Refused:
                failure = failure or "refused"
            except Exception as exc:  # noqa: BLE001 - one quote must not stop the rest
                logger.error("btcpay poll failed for account %s (%s)", account_pk, type(exc).__name__)
                failure = failure or "error"
        if failure:
            health.failed(name, failure)
        else:
            _read_fine(name, gateway)
    idle_probe.probe_idle("btcpay", by_account, answered=_read_fine)
    return looked


def _read_fine(name, gateway) -> None:
    """BTCPay answered. A speed policy we refuse still makes no new quote, so it stays failing."""
    if gateway.speed not in BTCPAY_SPEED_POLICIES:
        health.failed(name, SPEED_POLICY_KIND)
    else:
        health.ok(name)
