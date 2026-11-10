"""Alby Hub (or any NIP-47 wallet) over a receive-only Nostr Wallet Connect connection.

Only `make_invoice` and `lookup_invoice` are ever called; the connection string should carry
no spending permission, and this class has no method that could use one. The NWC URL is a
secret and is never logged. Facts measured on a live Alby Hub (see the Liberation.Services
Encomienda notes): `ACCEPTED` is a hold, not a payment, so only `SETTLED` counts, and the hub
refuses any amount that is not a whole number of satoshis.
"""

import asyncio
import logging

from django.conf import settings

from apps.rails import http
from apps.rails.gateways.lightning.base import FractionalSats

logger = logging.getLogger(__name__)
SERVICE = "lightning"
TIMEOUT_S = 30
SETTLED = "SETTLED"


async def _call(nwc_url, what, timeout):
    from nostr_sdk import NostrWalletConnect, NostrWalletConnectUri

    nwc = NostrWalletConnect(NostrWalletConnectUri.parse(nwc_url))
    try:
        return await asyncio.wait_for(what(nwc), timeout)
    finally:
        client = getattr(nwc, "client", None)
        if callable(client):
            try:
                await client().shutdown()
            except Exception:  # noqa: BLE001 - best effort teardown
                logger.debug("nwc client shutdown failed")


def _run(nwc_url, what):
    """Run one request, waiting at most `TIMEOUT_S`, or the active `http.request_budget` inside
    a web request. Any failure is "could not look" (`Unavailable`); the cause is not logged
    because exception text can carry the connection string."""
    if not getattr(settings, "RAILS_HTTP_ENABLED", True):  # as `http._enabled`: a forgotten patch fails loudly
        raise RuntimeError("outbound rails HTTP is disabled (RAILS_HTTP_ENABLED=False)")
    timeout = http.capped(TIMEOUT_S)  # read here: asyncio.run gives the coroutine a copied context
    try:
        return asyncio.run(_call(nwc_url, what, timeout))
    except Exception as exc:  # noqa: BLE001
        logger.warning("nwc request failed (%s)", type(exc).__name__)
        raise http.Unavailable(SERVICE, "nwc") from None


def _method_name(method) -> str:
    """The wire name of one advertised method. `nostr-sdk` maps the methods it knows to enum
    members and the rest to `Method.UNKNOWN(unknown=<name>)`; `str()` carries the name in both."""
    text = str(method)
    if "unknown=" in text:
        return text.split("unknown=", 1)[1].rstrip(")").strip().lower()
    return text.replace("Method.", "").rstrip("()").strip().lower()


def capabilities(nwc_url: str) -> set[str]:
    """The methods this connection allows, by wire name (`get_info`). Goes through `_run`, so
    the test guard applies; any failure is `http.Unavailable` ("could not look")."""

    async def go(nwc):
        return await nwc.get_info()

    answer = _run(nwc_url, go)
    try:
        return {_method_name(m) for m in answer.methods}
    except Exception:  # noqa: BLE001
        raise http.Unavailable(SERVICE, "bad_answer") from None


class NwcBackend:
    def __init__(self, nwc_url: str):
        self._url = nwc_url

    def probe(self) -> None:
        """Whether the wallet answers at all: one `get_info`, which needs no invoice (`idle_probe`)."""
        capabilities(self._url)

    def create_invoice(self, amount_msat, description, expiry_s):
        from nostr_sdk import MakeInvoiceRequest

        if not isinstance(amount_msat, int) or isinstance(amount_msat, bool) or amount_msat <= 0:
            raise ValueError("an invoice needs a positive amount")
        if amount_msat % 1000:
            raise FractionalSats("Alby Hub refuses an amount that is not a whole number of sats")

        async def go(nwc):
            return await nwc.make_invoice(MakeInvoiceRequest(
                amount=amount_msat, description=description, description_hash=None, expiry=expiry_s))

        answer = _run(self._url, go)
        bolt11 = getattr(answer, "invoice", "") or ""
        payment_hash = getattr(answer, "payment_hash", "") or ""
        if not (isinstance(bolt11, str) and isinstance(payment_hash, str) and bolt11 and payment_hash):
            raise http.Unavailable(SERVICE, "bad_answer")
        return bolt11, payment_hash

    def invoice_state(self, payment_hash):
        from nostr_sdk import LookupInvoiceRequest

        async def go(nwc):
            return await nwc.lookup_invoice(LookupInvoiceRequest(payment_hash=payment_hash, invoice=None))

        answer = _run(self._url, go)
        state = getattr(answer, "state", None)
        name = getattr(state, "name", None) or str(state or "")
        if name == SETTLED:  # equality only: ACCEPTED is a hold
            msat = getattr(answer, "amount", None)
            if not isinstance(msat, int) or isinstance(msat, bool) or msat < 0:
                raise http.Unavailable(SERVICE, "bad_answer")
            return "settled", msat
        if name == "EXPIRED":
            return "expired", 0
        if name in ("PENDING", "ACCEPTED"):
            return "open", 0
        return "unknown", 0
