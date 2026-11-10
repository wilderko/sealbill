"""Payment URIs a wallet can open (or read from a QR code) for a chain quote.

EIP-681 on EVM chains, Solana Pay on Solana, the `ton://transfer` link on TON. Tron has no
standard, so it gets none and the pay page shows the address and the amount instead.
"""

from urllib.parse import quote as urlquote

from apps.rails import addresses
from apps.rails.networks import NETWORKS


def decimal_amount(amount_base_units: int, decimals: int) -> str:
    """`amount_base_units` in whole units, exactly: every significant digit, and no trailing
    zeros beyond two decimal places (108270000 at 6 decimals is "108.27", 10**18 at 18 "1.00")."""
    if amount_base_units < 0 or decimals < 0:
        raise ValueError("amounts and decimals are never negative")
    whole, frac = divmod(amount_base_units, 10**decimals)
    digits = str(frac).rjust(decimals, "0").rstrip("0") if decimals else ""
    return f"{whole}.{digits.ljust(2, '0')}"


def payment_uri(network: str, asset: str, address: str, amount_base_units: int, reference: str) -> str:
    """The URI that asks a wallet to send exactly `amount_base_units` of `asset` on `network` to
    `address`; "" where no standard exists. ValueError for an unknown pair or a bad address."""
    net = NETWORKS.get(network)
    spec = net.assets.get(asset) if net else None
    if spec is None:
        raise ValueError(f"{asset!r} on {network!r} is not on the allow-list")
    to = addresses.normalize(net.family, address)
    amount = int(amount_base_units)
    if net.family == "evm":
        if spec.contract:
            contract = addresses.normalize("evm", spec.contract)
            return f"ethereum:{contract}@{net.chain_id}/transfer?address={to}&uint256={amount}"
        return f"ethereum:{to}@{net.chain_id}?value={amount}"
    if net.family == "solana":
        return f"solana:{to}?amount={decimal_amount(amount, spec.decimals)}&spl-token={spec.contract}"
    if net.family == "ton":
        return (f"ton://transfer/{addresses.ton_user_friendly(to)}?jetton={spec.contract}"
                f"&amount={amount}&text={urlquote(reference, safe='')}")
    return ""
