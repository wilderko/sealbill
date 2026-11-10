"""Delete expired cache entries.

Every backend in use drops an expired entry only when that key is read again or the store
is culled, and most rate-limit keys (one per address and invoice) are never read again. The
worker runs `purge_expired()` hourly so they cannot pile up, whichever backend is configured.
"""
import time

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.db import DatabaseCache
from django.core.cache.backends.filebased import FileBasedCache
from django.core.cache.backends.locmem import LocMemCache
from django.db import connections, router, transaction
from django.utils import timezone


def _purge_database(backend: DatabaseCache) -> int:
    db = router.db_for_write(backend.cache_model_class)
    connection = connections[db]
    table = connection.ops.quote_name(backend._table)
    expires = connection.ops.quote_name("expires")
    now = timezone.now().replace(microsecond=0)  # the precision DatabaseCache stores
    with transaction.atomic(using=db), connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE {expires} < %s", [connection.ops.adapt_datetimefield_value(now)]
        )
        return max(cursor.rowcount, 0)


def _purge_files(backend: FileBasedCache) -> int:
    removed = 0
    for name in backend._list_cache_files():
        try:
            with open(name, "rb") as fh:
                removed += backend._is_expired(fh)  # deletes the file when expired
        except FileNotFoundError:
            pass
    return removed


def _purge_locmem(backend: LocMemCache) -> int:
    now = time.time()
    with backend._lock:
        stale = [k for k, exp in backend._expire_info.items() if exp is not None and exp <= now]
        for key in stale:
            backend._delete(key)
    return len(stale)


def purge_expired() -> int:
    """Delete the expired entries of every configured cache; returns how many went."""
    removed = 0
    for alias in settings.CACHES:
        backend = caches[alias]
        if isinstance(backend, DatabaseCache):
            removed += _purge_database(backend)
        elif isinstance(backend, FileBasedCache):
            removed += _purge_files(backend)
        elif isinstance(backend, LocMemCache):
            removed += _purge_locmem(backend)
    from apps.vault import ratelimit

    return removed + ratelimit.purge_expired()  # the rate counters live in their own table
