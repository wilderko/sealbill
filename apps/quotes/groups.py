"""Amount groups: the quotes one transfer could be mistaken for.

One receiving address takes every network of its family: an EVM address is the same on
Ethereum, Arbitrum, Base and the rest, and a client who picks the wrong network or the wrong
stablecoin in their exchange still sends the amount they were shown. So a unique amount is
unique per **amount group**, never only per network and asset (audit finding MONEY-6).

A group is (family, unit, receiving address). The unit is "USD" for every USD stablecoin and the
asset's own symbol for anything else, so native ETH is one group across every EVM network of the
address. Inside a group amounts are compared by value, at `GROUP_DECIMALS`: 108.27 USDT on BNB
Smart Chain (18 decimals) and 108.27 USDC on Arbitrum (6) are the same amount.

Pure functions of a quote's own columns, with no database access: `Quote.save` and the
migration that backfilled the columns both use them.
"""
from apps.rails.networks import NETWORKS

USD_STABLECOINS = frozenset({"USDC", "USDT", "USDT0"})
GROUP_DECIMALS = 18  # the most any allow-listed asset has
# A released amount is remembered this long after its quote closed: the day it is held
# (`quotes.services.QUOTE_AMOUNT_HOLD_S`), then 30 days in which it may have been handed on.
# Matching lets such an amount stand in a transfer's way (audit finding MONEY-3), so the
# allocator hands it out only when nothing else in the group is free.
RELEASED_MEMORY_S = 31 * 86400


def amount_group(network: str, asset: str, address: str) -> str:
    """The amount group of a quote for `asset` on `network` paid to `address`: "family:unit:address",
    or "" for a quote without an address (BTCPay and Lightning are matched by the gateway's id)."""
    if not address:
        return ""
    net = NETWORKS.get(network)
    family = net.family if net is not None else network  # off the allow-list: a family of its own
    if family == "evm":
        address = address.lower()  # one EVM address, however it is written
    unit = "USD" if asset in USD_STABLECOINS else asset
    return f"{family}:{unit}:{address}"


def group_amount(network: str, asset: str, amount: int) -> int:
    """`amount` (base units of `asset` on `network`) at `GROUP_DECIMALS`, so that amounts of one
    group compare by value whatever their decimals. Exact: it only ever multiplies."""
    net = NETWORKS.get(network)
    spec = net.assets.get(asset) if net is not None else None
    if spec is None:
        return amount  # off the allow-list: its group holds this asset alone, compared as it is
    if spec.decimals > GROUP_DECIMALS:
        raise ValueError(f"{asset} on {network} has more than {GROUP_DECIMALS} decimals")
    return amount * 10 ** (GROUP_DECIMALS - spec.decimals)
