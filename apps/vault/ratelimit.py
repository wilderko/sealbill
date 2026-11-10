import ipaddress
import logging
import threading
import time

from django.core.cache import caches

logger = logging.getLogger(__name__)

# Used only while the shared store fails: one count per process, so the limit still holds
# (per worker) and a broken cache can never stop the vault from being unlocked.
_fallback: dict[str, tuple[int, float]] = {}
_fallback_lock = threading.Lock()
_FALLBACK_MAX = 10000


def client_ip(request) -> str:
    """Last X-Forwarded-For entry (the proxy appends the real address), else REMOTE_ADDR."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff.strip():
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def address_bucket(ip: str) -> str:
    """What an address is counted as: an IPv6 address by its /64, which one subscriber is handed
    whole and can rotate through at will; an IPv4 address (an IPv4-mapped one too) as itself.
    Anything that is not an address is counted as the text it is."""
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip
    if address.version == 4:
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(ipaddress.IPv6Network((int(address) >> 64 << 64, 64)))


def _next(entry, now, window_s):
    if not entry or entry[1] <= now:
        entry = (0, now + window_s)
    return entry[0] + 1, entry[1]


def _count_locally(key, now, window_s) -> int:
    with _fallback_lock:
        if len(_fallback) >= _FALLBACK_MAX:
            for k in [k for k, (_c, end) in _fallback.items() if end <= now]:
                del _fallback[k]
            if len(_fallback) >= _FALLBACK_MAX:
                _fallback.clear()
        count, end = _next(_fallback.get(key), now, window_s)
        _fallback[key] = (count, end)
    return count


def count_attempt(bucket: str, ident: str, window_s: int) -> int:
    """Count this attempt and return how many fall in the current window.

    The window is fixed from the first hit: the value carries its start time and every
    write passes the remaining time as the timeout (file/db caches reset TTL on set).
    If the shared store fails, the attempt is counted in this process instead."""
    key = f"rl:{bucket}:{ident}"
    now = time.time()
    try:
        store = caches["ratelimit"]
        count, end = _next(store.get(key), now, window_s)
        store.set(key, (count, end), max(1, int(end - now) + 1))
    except Exception as exc:  # noqa: BLE001 - an outage of the store must not become a 500
        logger.error("rate-limit store unavailable (%s); counting in this process", type(exc).__name__)
        return _count_locally(key, now, window_s)
    return count


def attempts_so_far(bucket: str, ident: str) -> int:
    """How many attempts the current window of this bucket holds, counting nothing. For a bucket that
    counts only what succeeded (`count_attempt` after the fact): ask first, count once it is done."""
    key = f"rl:{bucket}:{ident}"
    now = time.time()
    try:
        entry = caches["ratelimit"].get(key)
    except Exception as exc:  # noqa: BLE001 - an outage of the store must not become a 500
        logger.error("rate-limit store unavailable (%s); counting in this process", type(exc).__name__)
        with _fallback_lock:
            entry = _fallback.get(key)
    if not entry or entry[1] <= now:
        return 0
    return entry[0]


def rate_limited(bucket: str, ident: str, limit: int, window_s: int) -> bool:
    """Count this attempt; True once more than `limit` attempts fall in the window."""
    return count_attempt(bucket, ident, window_s) > limit
