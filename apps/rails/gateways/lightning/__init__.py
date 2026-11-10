"""Lightning as a gateway: one BOLT11 invoice per quote, from our own node.

The system never pays anything. It makes invoices and reads whether they were settled. Alby Hub
over a receive-only NWC connection is the primary backend; LND, Core Lightning and LNbits sit
behind the same interface (they are verified against documentation and mocks only, and Project 2
tests them on a testnet). Failures only record health; they never change a quote's state. Logs
carry the account pk and error kinds only: no amounts, payment hashes, invoices or URLs.
"""

import datetime
import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from django.db.models import Q
from django.utils import timezone

from apps.invoices.currencies import minor_units
from apps.orgs.models import Organization
from apps.payments import matching
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails import fx, health, http
from apps.rails.gateways import registry
from apps.rails.gateways.lightning.cln import ClnBackend
from apps.rails.gateways.lightning.lnbits import LnbitsBackend
from apps.rails.gateways.lightning.lnd import LndBackend
from apps.rails.gateways.lightning.nwc import NwcBackend
from apps.rails.gateways.rotation import rotated
from apps.rails.models import RailAccount
from apps.vault.vault import VaultSealed, vault

logger = logging.getLogger(__name__)

EXPIRY_S = 1800
LATE_POLL_WINDOW = datetime.timedelta(hours=24)
SATS_PER_BTC = Decimal(10) ** 8
MSAT_PER_BTC = Decimal(10) ** 11
SECRET_FIELD = {"nwc": "nwc_url", "lnd": "macaroon", "cln": "rune", "lnbits": "invoice_key"}


def _backend(account):
    try:
        creds = account.credentials or {}
    except VaultSealed:
        raise QuoteError("rail_unavailable") from None
    cfg = account.config or {}
    kind = cfg.get("backend")
    secret = str(creds.get(SECRET_FIELD.get(kind, ""), ""))
    url = str(cfg.get("url", ""))
    if kind == "nwc" and secret:
        return NwcBackend(secret)
    if url and secret:
        if kind == "lnd":
            return LndBackend(url, secret)
        if kind == "cln":
            return ClnBackend(url, secret)
        if kind == "lnbits":
            return LnbitsBackend(url, secret)
    raise QuoteError("rail_unavailable")


class LightningGateway:
    def __init__(self, account):
        self.account_pk = account.pk
        self.backend = _backend(account)

    def _sats(self, quote) -> tuple[int, str, str, str]:
        """Sats for the remainder, with the rate fields. `fx.RatesUnavailable` and
        `fx.RatesDiverge` reach the caller, which maps them and records the exchange rates'
        health (`apps.quotes.services`)."""
        inv = quote.invoice
        if inv.currency == "BTC":  # the remainder is already in sats (minor units of BTC)
            if minor_units("BTC") != 8:
                raise QuoteError("rail_unavailable")
            return quote.invoice_amount_minor, "1", "1", "identity"
        pair = fx.price_in("BTC", inv.currency)
        major = Decimal(quote.invoice_amount_minor).scaleb(-minor_units(inv.currency))
        sats = int((major / pair.rate * SATS_PER_BTC).to_integral_value(rounding=ROUND_CEILING))
        return max(sats, 1), format(pair.rate, "f")[:64], format(pair.second, "f")[:64], pair.source[:64]

    def _description(self, quote) -> str:
        inv = quote.invoice
        slug = Organization.objects.filter(pk=inv.org_id).values_list("slug", flat=True).first() or ""
        return f"{slug} {inv.number or f'P{inv.pk}'}"

    def create_target(self, quote) -> None:
        sats, rate, second, source = self._sats(quote)
        bolt11, payment_hash = self.backend.create_invoice(sats * 1000, self._description(quote), EXPIRY_S)
        quote.external_id, quote.bolt11 = payment_hash, bolt11
        quote.payment_uri = f"lightning:{bolt11}"
        quote.amount_base_units = str(sats)
        quote.rate, quote.rate_second, quote.rate_source = rate, second, source
        soonest = timezone.now() + datetime.timedelta(seconds=EXPIRY_S)
        if soonest < quote.expires_at:
            quote.expires_at = soonest

    def poll(self, quote) -> bool:
        """Read the invoice and record what became of it. False when the node's answer says
        nothing (an unknown state): that is not a read, so the quote stays watched."""
        state, msat = self.backend.invoice_state(quote.external_id)
        if state == "settled":
            self._settled(quote, msat)
        elif state == "expired":
            Quote.objects.filter(pk=quote.pk, state="open").update(state="expired", closed_at=timezone.now())
        return state != "unknown"

    @staticmethod
    def _settled(quote, msat):
        due = int(quote.amount_base_units) * 1000
        full = quote.invoice_amount_minor
        record = dict(network="lightning", external_id=f"ln:{quote.external_id}",
                      received_amount=Decimal(msat) / MSAT_PER_BTC)
        if msat >= due:
            extra = (msat - due) // 1000  # sub-satoshi dust is not a refund
            refund = str(extra) if extra > 0 else ""
            if msat > matching.gateway_ceiling(quote, due):  # beyond the organisation's ceiling, as on chains
                matching.record_gateway_payment(quote, state="needs_review", review_reason="overpaid_beyond_ceiling",
                                                amount_minor=full, refund_due_base_units=refund, **record)
                return
            matching.record_gateway_payment(quote, state="confirmed", amount_minor=full,
                                            refund_due_base_units=refund, **record)
        else:  # never confirmed at full credit
            credit = int((Decimal(full) * msat / due).to_integral_value(rounding=ROUND_FLOOR)) if due else 0
            matching.record_gateway_payment(quote, state="needs_review", review_reason="no_matching_invoice",
                                            amount_minor=credit, **record)


def factory(account):
    return LightningGateway(account)


POLL_SECONDS = 30  # the period of the `lightning_poll` job


def lightning_poll() -> int:
    """Re-read the node for every Lightning quote that may have news. Returns quotes looked at.

    That is every open quote, every quote closed in the last day, and whatever its age a quote
    not read since it expired (`gateway_money_may_be_coming`). Silent while the vault is sealed.
    An unreachable node is recorded as a failing watcher and leaves every quote exactly as it
    was; only an answer that says something counts as a read (`Quote.read_at`)."""
    from apps.quotes.services import gateway_money_may_be_coming
    if not vault.is_unsealed():
        return 0
    now = timezone.now()
    candidates = Quote.objects.filter(rail_account__kind="lightning").exclude(external_id="").filter(
        Q(check_soon=True) | Q(state="open") | Q(state="expired", expires_at__gte=now - LATE_POLL_WINDOW)
        | Q(state="cancelled", closed_at__gte=now - LATE_POLL_WINDOW) | gateway_money_may_be_coming())
    by_account: dict[int, list[int]] = {}
    for pk, account_pk in candidates.order_by("pk").values_list("pk", "rail_account_id"):
        by_account.setdefault(account_pk, []).append(pk)
    looked = 0
    for account_pk, pks in by_account.items():
        name = f"lightning:{account_pk}"
        failure = None
        unavailable_in_a_row = 0
        try:
            gateway = registry.for_account(RailAccount.objects.get(pk=account_pk))
        except (QuoteError, VaultSealed):
            if not vault.is_unsealed():
                return looked
            health.failed(name, "unconfigured")
            continue
        for pk in rotated(pks, POLL_SECONDS, now):
            quote = Quote.objects.get(pk=pk)
            try:
                read = gateway.poll(quote)
                done = {"check_soon": False}
                if read is not False:
                    done["read_at"] = timezone.now()
                Quote.objects.filter(pk=pk).update(**done)
                looked += 1
                unavailable_in_a_row = 0
            except VaultSealed:
                return looked
            except http.Unavailable as exc:
                failure = exc.kind
                # One failing lookup (NWC turns every error into Unavailable) must not
                # hide the quotes behind it; a node that is really down is asked three
                # times per pass, not once per quote. The next pass starts elsewhere.
                unavailable_in_a_row += 1
                if unavailable_in_a_row >= 3:
                    break
            except http.Refused:
                failure = failure or "refused"
            except Exception as exc:  # noqa: BLE001 - one quote must not stop the rest
                logger.error("lightning poll failed for account %s (%s)", account_pk, type(exc).__name__)
                failure = failure or "error"
        if failure:
            health.failed(name, failure)
        else:
            health.ok(name)
    return looked
