"""A payment link's custom method set, stored as method TYPES, never as account ids (spec 2.3a item 5).

A type names a way to pay, not an account, so it applies to whichever organization issues the order:
a US tax resident's order is issued by the US-resident issuer, which offers its own accounts of the
same types.

- `chain:<network>:<asset>`   a stablecoin or native coin on a listed chain (`chain:arbitrum:USDC`)
- `btcpay:<asset>`            BTC, XMR or ZEC through a BTCPay store
- `lightning`                 Bitcoin over Lightning
- `bank:<currency>`           a bank transfer in that currency
- `fiat:<kind>`               an online payment link of that kind (Revolut, PayPal, ...)

`resolve` turns a set into the issuer's offered methods (`rail:`, `bank:`, `fiat:` keys) and the
(network, asset) pairs its rails may be quoted in (`Invoice.offered_pairs`): a rail is offered for the
pairs chosen, never for the others it also takes.
"""
from apps.invoices.currencies import CURRENCIES
from apps.orgs.models import BankAccount, FiatLink
from apps.quotes.services import GATEWAY_NETWORKS, _enabled
from apps.rails.models import BTCPAY_ASSETS, CHAIN_KINDS, RailAccount, ton_blocked
from apps.rails.networks import NETWORKS

FIAT_KINDS = {"revolut": "Revolut", "n26": "N26", "paypal": "PayPal", "wise": "Wise", "other": "Other payment link"}
GATEWAY_NAMES = {"BTC": "Bitcoin", "XMR": "Monero", "ZEC": "Zcash"}


def valid(t: str) -> bool:
    kind, _, rest = str(t).partition(":")
    if kind == "chain":
        network, _, asset = rest.partition(":")
        net = NETWORKS.get(network)
        return net is not None and net.family in CHAIN_KINDS and asset in net.assets
    if kind == "btcpay":
        return rest in BTCPAY_ASSETS
    if kind == "lightning":
        return rest == ""
    if kind == "bank":
        return rest in CURRENCIES
    if kind == "fiat":
        return rest in FIAT_KINDS
    return False


def label(t: str) -> str:
    """The type as staff read it (English, like every staff page)."""
    kind, _, rest = str(t).partition(":")
    if kind == "chain":
        network, _, asset = rest.partition(":")
        net = NETWORKS.get(network)
        return f"{asset} on {net.name if net else network}"
    if kind == "btcpay":
        return f"{GATEWAY_NAMES.get(rest, rest)} through BTCPay"
    if kind == "lightning":
        return "Bitcoin over Lightning"
    if kind == "bank":
        return f"Bank transfer in {rest}"
    if kind == "fiat":
        return FIAT_KINDS.get(rest, rest)
    return t


def _rail_pairs(account: RailAccount) -> list[tuple[str, str]]:
    if ton_blocked(account.kind):
        return []
    if account.kind in GATEWAY_NETWORKS:
        return [(net, asset) for asset, net in GATEWAY_NETWORKS[account.kind].items() if _enabled(account, net, asset)]
    return [(net, asset) for net in account.networks or [] for asset in account.assets or []
            if _enabled(account, net, asset)]


def _pair_type(account: RailAccount, network: str, asset: str) -> str:
    if account.kind == "btcpay":
        return f"btcpay:{asset}"
    if account.kind == "lightning":
        return "lightning"
    return f"chain:{network}:{asset}"


def types_of(org) -> list[str]:
    """Every type `org`'s active accounts offer, in a stable order (plaintext columns only)."""
    found = []
    for currency in BankAccount.objects.filter(org=org, active=True).values_list("currency", flat=True):
        found.append(f"bank:{currency.upper()}")
    for kind in FiatLink.objects.filter(org=org, active=True).values_list("kind", flat=True):
        found.append(f"fiat:{kind}")
    for account in RailAccount.objects.filter(org=org, active=True).order_by("pk"):
        found += [_pair_type(account, net, asset) for net, asset in _rail_pairs(account)]
    return sorted(set(t for t in found if valid(t)), key=_order)


def _order(t: str):
    rank = {"chain": 0, "lightning": 1, "btcpay": 2, "bank": 3, "fiat": 4}
    return rank.get(t.partition(":")[0], 9), label(t)


def resolve(issuer, types, currency: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(offered method keys, offered pairs) of `issuer` for these types and the order's currency. A
    bank account must be in the currency, a payment link must accept it, as `default_methods` asks."""
    wanted = {str(t) for t in types or [] if valid(t)}
    currency = currency.upper()
    methods, pairs = [], []
    if f"bank:{currency}" in wanted:
        methods += [f"bank:{pk}" for pk in BankAccount.objects.filter(
            org=issuer, active=True, currency__iexact=currency).order_by("pk").values_list("pk", flat=True)]
    for link in FiatLink.objects.filter(org=issuer, active=True).order_by("pk").only("pk", "kind", "currencies"):
        if f"fiat:{link.kind}" in wanted and currency in {str(c).upper() for c in link.currencies or []}:
            methods.append(f"fiat:{link.pk}")
    for account in RailAccount.objects.filter(org=issuer, active=True).order_by("pk"):
        chosen = [(net, asset) for net, asset in _rail_pairs(account) if _pair_type(account, net, asset) in wanted]
        if chosen:
            methods.append(account.method_key)
            pairs += [p for p in chosen if p not in pairs]
    return methods, pairs
