"""Which crypto methods the pay page offers, in the client's words."""
from django.utils.translation import gettext as _

from apps.quotes.errors import QuoteError
from apps.quotes.services import GATEWAY_NETWORKS, _enabled, pair_offered
from apps.rails.gateways.btcpay import DECIMALS as GATEWAY_DECIMALS
from apps.rails.models import RailAccount, ton_blocked
from apps.rails.networks import NETWORKS

# Brand and protocol names, kept as they are written everywhere.
GATEWAY_NAMES = {"bitcoin": "Bitcoin", "lightning": "Lightning", "monero": "Monero", "zcash": "Zcash"}
ASSET_ONLY = {"btcpay", "lightning"}  # shown as the network's own name: "Bitcoin", "Monero"


def network_name(key: str) -> str:
    if key in GATEWAY_NAMES:
        return GATEWAY_NAMES[key]
    if key == "tron":
        return "Tron (TRC-20)"
    net = NETWORKS.get(key)
    return net.name if net else key


def _accounts(invoice) -> list[RailAccount]:
    pks = []
    for key in invoice.offered_methods or []:
        kind, _sep, raw = str(key).partition(":")
        if kind == "rail" and raw.isascii() and raw.isdigit():
            pks.append(int(raw))
    found = {a.pk: a for a in RailAccount.objects.filter(pk__in=pks, org_id=invoice.org_id, active=True)}
    return [found[pk] for pk in pks if pk in found]


def account_for(invoice, method: str) -> RailAccount:
    """The active account behind `method` ("rail:<pk>"), or the refusal a made-up one deserves."""
    if method in (invoice.offered_methods or []):
        for account in _accounts(invoice):
            if account.method_key == method:
                return account
    raise QuoteError("method_not_offered")


def decimals_for(network: str, asset: str) -> int | None:
    """Decimal places of `asset` on `network` (a chain), or of a gateway's asset; None when unknown."""
    net = NETWORKS.get(network)
    if net is not None:
        spec = net.assets.get(asset)
        return spec.decimals if spec else None
    return GATEWAY_DECIMALS.get(asset)


def account_pairs(account: RailAccount) -> list[tuple[str, str]]:
    """The (network, asset) pairs `account` can be quoted in: none for TON while it is off."""
    if ton_blocked(account.kind):
        return []
    if account.kind in GATEWAY_NETWORKS:
        return [(net, asset) for asset, net in GATEWAY_NETWORKS[account.kind].items()
                if _enabled(account, net, asset)]
    return [(net, asset) for net in account.networks or [] for asset in (account.assets or [])
            if _enabled(account, net, asset)]


def options(invoice) -> list[dict]:
    """One entry per (account, network, asset) the client may choose."""
    out = []
    for account in _accounts(invoice):
        for net, asset in account_pairs(account):
            if not pair_offered(invoice, net, asset):
                continue
            name = network_name(net)
            label = name if account.kind in ASSET_ONLY else _("%(asset)s on %(network)s") % {
                "asset": asset, "network": name}
            out.append({"method": account.method_key, "network": net, "asset": asset, "label": label})
    return out


def offered_networks(invoice) -> list[tuple[str, str]]:
    seen, out = set(), []
    for o in options(invoice):
        if o["network"] not in seen:
            seen.add(o["network"])
            out.append((o["network"], network_name(o["network"])))
    return out
