"""What every chain watcher answers, and which one serves a network.

A watcher reads the chain, never a claim: it reports transfers that reached one of our
addresses (`scan`), the transfers inside one transaction a client named (`transfers_in_tx`),
and whether a transfer it reported earlier is final, still pending, or gone (`status`). It
raises `apps.rails.http.Unavailable` whenever it could not look, which callers never read as
"not paid". Matching (`apps.payments.matching`) decides what a transfer settles.
"""

from __future__ import annotations

import datetime
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from apps.rails.networks import FAMILIES, NETWORKS

if TYPE_CHECKING:
    from apps.payments.models import Payment
    from apps.rails.models import RailAccount

Status = Literal["final", "pending", "dropped"]
STATUSES: tuple[str, ...] = ("final", "pending", "dropped")


@dataclass(frozen=True)
class Transfer:
    """One movement of an asset to an address, as the chain records it.

    `contract` is "" for the native coin; `amount_base_units` is an exact integer; `log_index`
    is the token log's position in its transaction, -1 for a native value transfer. `comment`
    is the payer's text where the chain carries one (TON).

    Identity: `(tx_hash, log_index)` is what keeps one transfer from settling twice. Every
    adapter must emit the same canonical `tx_hash` and `log_index` on both `scan` and
    `transfers_in_tx` (EVM: lowercase 0x hash, the log's place among its transaction's logs, -1
    for native; a reorg that moves the transaction to another block keeps it)."""

    network: str
    contract: str
    to_address: str
    from_address: str
    amount_base_units: int
    tx_hash: str
    log_index: int
    block_number: int | None
    block_time: datetime.datetime
    final: bool
    comment: str = ""

    def __post_init__(self):
        amount = self.amount_base_units
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("a transfer amount is a non-negative whole number of base units")
        if not isinstance(self.tx_hash, str) or not self.tx_hash.strip():
            raise ValueError("a transfer names its transaction")  # the hash is what makes it unique
        if not isinstance(self.log_index, int) or isinstance(self.log_index, bool):
            raise ValueError("a log index is a whole number")
        if not isinstance(self.block_time, datetime.datetime) or self.block_time.tzinfo is None:
            raise ValueError("a block time is an aware datetime")


def scan_back_to(account: RailAccount, network: str) -> datetime.datetime | None:
    """Where a watcher's first look at `account` on `network` starts (it has no cursor yet):
    `CLOCK_SKEW_S` before the earliest unreleased quote on the account's address, or None when
    there is none, and the first look then only marks where the chain stands.

    A client may pay a quote before the worker has ever looked at a new wallet (it was
    restarting, or busy). A first look that only took the chain's state as its starting point
    would take that payment in with it and never report it (audit E2E-2). Every quote on the
    address counts, from any account and on any network of its family: a client who picked the
    wrong network still sent the money here. Except one thing: a quote of another account whose
    own cursor on `network` was written at or after the quote was issued. That account is still
    watched while the quote is unreleased, and its window covers the quote; reading back for it
    would make an account that replaces a retired one walk a day of blocks for nothing."""
    from apps.payments.matching import CLOCK_SKEW_S
    from apps.quotes.models import Quote
    from apps.rails.models import WatchCursor

    if not account.address:
        return None
    watching: dict[int, datetime.datetime] = {}
    for stream, written in (WatchCursor.objects.filter(network=network, address=account.address)
                            .values_list("stream", "updated_at")):
        owner = stream.rpartition(":")[2]
        if owner.isdigit() and int(owner) != account.pk:
            watching[int(owner)] = max(written, watching.get(int(owner), written))
    quotes = (Quote.objects.filter(address=account.address, rail_account__kind=account.kind)
              .exclude(state="released").order_by("issued_at", "pk").values_list("issued_at", "rail_account_id"))
    for issued, owner in quotes:
        if owner != account.pk and owner in watching and watching[owner] >= issued:
            continue  # that account's own watching covers it
        return issued - datetime.timedelta(seconds=CLOCK_SKEW_S)
    return None


class ChainAdapter(Protocol):
    def scan(self, account: RailAccount, network: str) -> list[Transfer]:
        """Transfers to `account.address` on `network` since the stored cursor."""

    def transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        """The transfers in one successful transaction ([] when it is not found or failed)."""

    def status(self, payment: Payment) -> Status:
        """Whether the transfer behind `payment` is final, still pending, or no longer on chain."""


_ADAPTERS: dict[str, ChainAdapter] = {}


def register(family: str, adapter: ChainAdapter) -> None:
    """Serve every network of `family` with `adapter`."""
    if family not in FAMILIES:
        raise ValueError(f"{family!r} is not a chain family")
    _ADAPTERS[family] = adapter


def unregister(family: str) -> None:
    _ADAPTERS.pop(family, None)


def adapter_for(network_key: str) -> ChainAdapter:
    """The adapter for `network_key`'s family; LookupError for an unknown network or family
    nobody registered."""
    net = NETWORKS.get(network_key)
    if net is None:
        raise LookupError(f"unknown network {network_key!r}")
    adapter = _ADAPTERS.get(net.family)
    if adapter is None:
        raise LookupError(f"no chain adapter for {net.family!r}")
    return adapter


@contextmanager
def override_adapter(family: str, adapter: ChainAdapter):
    """Serve `family` with `adapter` inside the block, then put back what was there (tests)."""
    previous = _ADAPTERS.get(family)
    register(family, adapter)
    try:
        yield adapter
    finally:
        if previous is None:
            unregister(family)
        else:
            _ADAPTERS[family] = previous
