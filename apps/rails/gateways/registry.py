"""Which gateway serves a rail account of a given kind.

A gateway is registered by account kind (Tasks 10 and 11 register BTCPay and Lightning) with a
factory that takes the `RailAccount` and returns an object following `Gateway`. Until a kind is
registered, `for_account` refuses with `QuoteError("rail_unavailable")`, so a quote for that
rail is never saved.
"""

from typing import Callable, Protocol

from apps.quotes.errors import QuoteError

GATEWAY_KINDS = ("btcpay", "lightning")


class Gateway(Protocol):
    def create_target(self, quote) -> None:
        """Ask the gateway for a payment target for this unsaved quote and fill it in memory:
        `external_id`, `amount_base_units`, `payment_uri`, `checkout_url` and/or `bolt11`, and
        the rate fields when the gateway prices the target itself (it may also set
        `min_accept_base_units` and `ceiling_base_units`; both default to the amount). Never
        saves. Called outside any invoice lock, because it waits on the network. Raises
        `http.Unavailable` / `http.Refused` when the gateway cannot answer, and
        `fx.RatesUnavailable` / `fx.RatesDiverge` when it prices the target from exchange rates
        that cannot be had."""

    def poll(self, quote) -> None:
        """Re-read the target from the gateway and record what has arrived for `quote`."""


_FACTORIES: dict[str, Callable[..., Gateway]] = {}


def register(kind: str, factory: Callable[..., Gateway]) -> None:
    """Serve accounts of `kind` with `factory(account)`."""
    if kind not in GATEWAY_KINDS:
        raise ValueError(f"{kind!r} is not a gateway kind")
    _FACTORIES[kind] = factory


def for_account(account) -> Gateway:
    factory = _FACTORIES.get(account.kind)
    if factory is None:
        raise QuoteError("rail_unavailable")
    return factory(account)
