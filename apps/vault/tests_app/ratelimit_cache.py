"""The test suite's rate-limit cache. Tests reset their limits with `caches["ratelimit"].clear()`;
since the counters moved to `RateCounter` (red-team A1), clearing that cache empties the table
and the per-process fallback as well, so a reset still means what it says."""
from django.core.cache.backends.locmem import LocMemCache


class RateLimitTestCache(LocMemCache):
    def clear(self):
        super().clear()
        from apps.vault import ratelimit
        from apps.vault.models import RateCounter

        with ratelimit._fallback_lock:
            ratelimit._fallback.clear()
        try:
            RateCounter.objects.all().delete()
        except Exception:  # noqa: BLE001 - a SimpleTestCase has no database to clear
            pass
