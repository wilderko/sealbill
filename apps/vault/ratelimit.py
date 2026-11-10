import hashlib
import ipaddress
import logging
import threading
import time

from django.db import IntegrityError, transaction
from django.db.models import F

logger = logging.getLogger(__name__)

# Used only while the shared store fails: one count per process, so the limit still holds
# (per worker) and a broken database can never stop the vault from being unlocked.
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
                # Drop the tenth that closes soonest, never every count at once: a flood of new
                # keys must not reset the one being guessed at (red-team A1).
                for k, _entry in sorted(_fallback.items(), key=lambda kv: kv[1][1])[:_FALLBACK_MAX // 10]:
                    del _fallback[k]
        count, end = _next(_fallback.get(key), now, window_s)
        _fallback[key] = (count, end)
    return count


def _key(bucket: str, ident: str) -> str:
    key = f"rl:{bucket}:{ident}"
    if len(key) <= 200:
        return key
    return f"rl:{bucket}:sha256:{hashlib.sha256(ident.encode('utf-8', 'surrogatepass')).hexdigest()}"


def _count_shared(key: str, now: float, window_s: int) -> int:
    """Count one attempt in the database, atomically.

    A live window is incremented in place (one UPDATE, so two attempts never read the same
    count); a closed one is reopened at 1 by an UPDATE that only matches while it is still closed;
    a missing one is created, and losing that race to another attempt means trying again. What is
    read back after an increment may include attempts made at the same moment: it only errs high."""
    from .models import RateCounter

    end = now + window_s
    for _ in range(5):
        if RateCounter.objects.filter(key=key, window_end__gt=now).update(count=F("count") + 1):
            return RateCounter.objects.filter(key=key).values_list("count", flat=True).first() or 1
        if RateCounter.objects.filter(key=key, window_end__lte=now).update(count=1, window_end=end):
            return 1
        try:
            with transaction.atomic():
                RateCounter.objects.create(key=key, count=1, window_end=end)
            return 1
        except IntegrityError:
            continue  # another attempt created it a moment ago: count against that one
    raise RuntimeError(f"the rate counter {key!r} did not settle")


def count_attempt(bucket: str, ident: str, window_s: int) -> int:
    """Count this attempt and return how many fall in the current window.

    The window is fixed from the first hit. The count lives in the database (`RateCounter`) and
    is incremented there in one statement. If the database fails, the attempt is counted in this
    process instead."""
    key = _key(bucket, ident)
    now = time.time()
    try:
        return _count_shared(key, now, window_s)
    except Exception as exc:  # noqa: BLE001 - an outage of the store must not become a 500
        logger.error("rate-limit store unavailable (%s); counting in this process", type(exc).__name__)
        return _count_locally(key, now, window_s)


def attempts_so_far(bucket: str, ident: str) -> int:
    """How many attempts the current window of this bucket holds, counting nothing. For a bucket that
    counts only what succeeded (`count_attempt` after the fact): ask first, count once it is done."""
    from .models import RateCounter

    key = _key(bucket, ident)
    now = time.time()
    try:
        row = RateCounter.objects.filter(key=key, window_end__gt=now).values_list("count", flat=True).first()
        return row or 0
    except Exception as exc:  # noqa: BLE001 - an outage of the store must not become a 500
        logger.error("rate-limit store unavailable (%s); counting in this process", type(exc).__name__)
        with _fallback_lock:
            entry = _fallback.get(key)
    if not entry or entry[1] <= now:
        return 0
    return entry[0]


def reset(bucket: str, ident: str) -> None:
    """Close the current window of this bucket: the next attempt counts from one."""
    from .models import RateCounter

    key = _key(bucket, ident)
    with _fallback_lock:
        _fallback.pop(key, None)
    RateCounter.objects.filter(key=key).delete()


def purge_expired() -> int:
    """Delete the windows that have closed; returns how many went."""
    from .models import RateCounter

    return RateCounter.objects.filter(window_end__lte=time.time()).delete()[0]


def rate_limited(bucket: str, ident: str, limit: int, window_s: int) -> bool:
    """Count this attempt; True once more than `limit` attempts fall in the window."""
    return count_attempt(bucket, ident, window_s) > limit
