"""Static allow-list of networks and token contracts.

Anything not listed here is never accepted: bridged USDC.e, Binance-Peg tokens
on other chains and the legacy bridged USDT on Optimism are deliberately absent.

Contracts were checked on 2026-11-02 against the issuers' own pages:
  Tether  https://tether.to/en/supported-protocols/
  USDT0   https://docs.usdt0.to/technical-documentation/deployments
  Circle  https://developers.circle.com/stablecoins/usdc-contract-addresses
A line marked "unverified" is one the issuer does not publish.
"""

from dataclasses import dataclass, field

FAMILIES = ("evm", "tron", "solana", "ton")
FINALITY = ("finalized", "l2", "confirmed", "age")


@dataclass(frozen=True)
class Asset:
    symbol: str
    contract: str  # "" for the native coin
    decimals: int
    step: int  # base-unit increment of a unique amount


@dataclass(frozen=True)
class Network:
    key: str
    family: str
    name: str
    chain_id: int | None
    finality: str
    assets: dict[str, Asset] = field(default_factory=dict)
    explorer_tx: str = ""


def _eth() -> Asset:
    return Asset("ETH", "", 18, 10**12)


def _stable(symbol: str, contract: str, decimals: int = 6) -> Asset:
    return Asset(symbol, contract, decimals, 10 ** (decimals - 2))


def _net(key, family, name, chain_id, finality, assets, explorer_tx) -> Network:
    return Network(key, family, name, chain_id, finality, {a.symbol: a for a in assets}, explorer_tx)


_LIST = [
    # USDT: tether.to supported-protocols. USDC: Circle's list.
    _net("ethereum", "evm", "Ethereum", 1, "finalized", [
        _eth(),
        _stable("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        _stable("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    ], "https://etherscan.io/tx/{tx}"),
    # USDC: Circle. USDT0: docs.usdt0.to deployments.
    _net("arbitrum", "evm", "Arbitrum One", 42161, "l2", [
        _eth(),
        _stable("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
        _stable("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
    ], "https://arbiscan.io/tx/{tx}"),
    # USDC: Circle. USDT0: docs.usdt0.to deployments (legacy bridged USDT 0x94b0...8e58 is not listed).
    _net("optimism", "evm", "Optimism", 10, "l2", [
        _eth(),
        _stable("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
        _stable("USDT", "0x01bFF41798a0BcF287b996046Ca68b395DbC1071"),
    ], "https://optimistic.etherscan.io/tx/{tx}"),
    # USDC: Circle.
    _net("base", "evm", "Base", 8453, "l2", [
        _eth(),
        _stable("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    ], "https://basescan.org/tx/{tx}"),
    # USDC: Circle. USDT: unverified, tether.to no longer lists Polygon.
    _net("polygon", "evm", "Polygon PoS", 137, "finalized", [
        _stable("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
        _stable("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
    ], "https://polygonscan.com/tx/{tx}"),
    # unverified: neither Tether nor Circle publishes a USDT/USDC contract on BNB Smart Chain
    # (these are Binance-Peg tokens); 18 decimals, not 6.
    _net("bsc", "evm", "BNB Smart Chain", 56, "finalized", [
        _stable("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
        _stable("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
    ], "https://bscscan.com/tx/{tx}"),
    # USDC: Circle. USDT: tether.to (Avalanche).
    _net("avalanche", "evm", "Avalanche C-Chain", 43114, "finalized", [
        _stable("USDC", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E"),
        _stable("USDT", "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7"),
    ], "https://snowtrace.io/tx/{tx}"),
    # USDT: tether.to (Tron).
    _net("tron", "tron", "Tron", None, "confirmed", [
        _stable("USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),
    ], "https://tronscan.org/#/transaction/{tx}"),
    # USDC: Circle. USDT: tether.to (Solana).
    _net("solana", "solana", "Solana", None, "finalized", [
        _stable("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
        _stable("USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),
    ], "https://solscan.io/tx/{tx}"),
    # USDT: tether.to (Ton jetton).
    _net("ton", "ton", "TON", None, "age", [
        _stable("USDT", "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"),
    ], "https://tonviewer.com/transaction/{tx}"),
]

NETWORKS: dict[str, Network] = {n.key: n for n in _LIST}


def asset_by_contract(network_key: str, contract: str) -> Asset | None:
    net = NETWORKS.get(network_key)
    if net is None:
        return None
    for a in net.assets.values():
        if not a.contract:
            continue
        if net.family == "evm":
            if a.contract.lower() == contract.lower():
                return a
        elif a.contract == contract:
            return a
    return None


def explorer_tx_url(network_key: str, tx: str) -> str:
    return NETWORKS[network_key].explorer_tx.format(tx=tx)
