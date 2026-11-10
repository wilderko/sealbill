"""Address validation and normalisation, with no third-party dependency."""

import base64
import binascii
import hashlib
import re

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TON_RAW = re.compile(r"^(-?\d+):([0-9a-f]{64})$")


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return _B58[0] * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip(_B58[0]))) + body


def _check(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def _tron(address: str) -> str:
    try:
        raw = _b58decode(address)
    except ValueError:
        raise ValueError("invalid Tron address") from None
    if len(raw) != 25 or raw[0] != 0x41 or raw[21:] != _check(raw[:21]):
        raise ValueError("invalid Tron address")
    return address


def tron_hex_to_base58(hex_addr: str) -> str:
    h = hex_addr.lower().removeprefix("0x")
    if len(h) == 40:
        h = "41" + h
    raw = bytes.fromhex(h)
    if len(raw) != 21 or raw[0] != 0x41:
        raise ValueError("invalid Tron hex address")
    return _b58encode(raw + _check(raw))


def _solana(address: str) -> str:
    try:
        ok = len(_b58decode(address)) == 32
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("invalid Solana address")
    return address


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _ton(address: str) -> str:
    m = _TON_RAW.match(address)
    if m:
        return f"{int(m.group(1))}:{m.group(2)}"
    if len(address) != 48:
        raise ValueError("invalid TON address")
    try:
        raw = base64.b64decode(address.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid TON address") from None
    if len(raw) != 36 or int.from_bytes(raw[34:], "big") != _crc16_xmodem(raw[:34]):
        raise ValueError("invalid TON address")
    wc = int.from_bytes(raw[1:2], "big", signed=True)
    return f"{wc}:{raw[2:34].hex()}"


def ton_user_friendly(address: str, *, bounceable: bool = False) -> str:
    """The mainnet user-friendly (base64url) form of a TON address, the form wallets read in a
    transfer link. Non-bounceable (`UQ…`) by default, which is how wallets show a wallet address."""
    wc, _, hex_hash = _ton(address).partition(":")
    body = bytes([0x11 if bounceable else 0x51]) + int(wc).to_bytes(1, "big", signed=True) + bytes.fromhex(hex_hash)
    raw = body + _crc16_xmodem(body).to_bytes(2, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def normalize(family: str, address: str) -> str:
    address = (address or "").strip()
    if family == "evm":
        if not _EVM.match(address):
            raise ValueError("invalid EVM address")
        return address.lower()
    if family == "tron":
        return _tron(address)
    if family == "solana":
        return _solana(address)
    if family == "ton":
        return _ton(address)
    raise ValueError(f"unknown address family: {family}")
