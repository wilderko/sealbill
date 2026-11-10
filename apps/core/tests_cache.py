"""The production cache configuration: database tables, never the vault-key tmpfs."""
import errno
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache, caches
from django.core.cache.backends.db import DatabaseCache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings

from apps.core import jobs, worker
from apps.vault.models import RateCounter
from apps.vault.ratelimit import rate_limited
from apps.vault.vault import vault
from config.settings import base as base_settings

PW = "correct horse battery"


def rows(table):
    with connection.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(table)}")
        return cur.fetchone()[0]


@override_settings(CACHES=base_settings.CACHES)
class ProductionCacheTests(TestCase):
    def setUp(self):
        call_command("createcachetable", verbosity=0)

    def test_every_cache_is_a_database_table_and_none_sits_on_the_key_tmpfs(self):
        key_dir = str(Path(base_settings.VAULT_KEY_PATH).parent)
        for alias, conf in base_settings.CACHES.items():
            self.assertIsInstance(caches[alias], DatabaseCache, alias)
            self.assertFalse(str(conf.get("LOCATION", "")).startswith(key_dir), alias)
        self.assertFalse(hasattr(base_settings, "CACHE_DIR"))

    def test_rate_limit_counts_in_the_database(self):
        for _ in range(3):
            self.assertFalse(rate_limited("portal", "1.2.3.4:abcd", 3, 60))
        self.assertTrue(rate_limited("portal", "1.2.3.4:abcd", 3, 60))
        self.assertEqual(rows("pay_rate_counter"), 1)  # one row, counted in place (red-team A1)
        self.assertEqual((rows("pay_ratelimit"), rows("pay_cache")), (0, 0))

    def test_expired_rate_limit_entries_are_purged_and_live_ones_kept(self):
        for i in range(5):
            rate_limited("portal", f"10.0.0.{i}:abcd", 60, 60)
        cache.set(jobs.SEALED_KEY, True, timeout=None)  # never expires
        self.assertEqual(rows("pay_rate_counter"), 5)
        rate_limited("portal", "10.0.0.99:abcd", 60, 60)  # still inside its window
        with connection.cursor() as cur:  # the first five windows ended long ago
            cur.execute("UPDATE pay_rate_counter SET window_end = 0 WHERE key NOT LIKE %s", ["%10.0.0.99%"])
        jobs.cache_purge()
        self.assertEqual(rows("pay_rate_counter"), 1)
        self.assertTrue(cache.get(jobs.SEALED_KEY))

    def test_cache_purge_is_an_hourly_worker_job(self):
        worker.reset()
        self.addCleanup(worker.reset)
        jobs.register_all()
        self.assertEqual(worker._jobs["cache_purge"]["every_s"], 3600)


class OtherBackendPurgeTests(TestCase):
    def test_file_and_locmem_backends_are_purged_too(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        conf = {
            "default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": d},
            "ratelimit": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "purge-test"},
        }
        with override_settings(CACHES=conf):
            for alias in ("default", "ratelimit"):
                caches[alias].set("old", 1, 1)
                caches[alias].set("new", 1, 600)
            later = __import__("time").time() + 5
            with mock.patch("time.time", return_value=later):
                from apps.core.cache_purge import purge_expired

                self.assertEqual(purge_expired(), 2)
            self.assertEqual(len(list(Path(d).iterdir())), 1)
            self.assertEqual(caches["ratelimit"].get("new"), 1)


class UnsealWithoutCacheTests(TestCase):
    """A broken cache must never keep the vault sealed."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        o = override_settings(VAULT_KEY_PATH=str(self.dir / "run" / "key"),
                              VAULT_CONFIG_PATH=str(self.dir / "cfg" / "vault.json"))
        o.enable()
        self.addCleanup(o.disable)
        vault._key = vault._seen = None
        caches["ratelimit"].clear()
        from apps.vault import ratelimit

        ratelimit._fallback.clear()
        self.addCleanup(vault.seal)
        vault.init(PW)

    def test_unlock_works_when_the_rate_limit_store_fails(self):
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(RateCounter.objects, "filter", side_effect=full), \
                self.assertLogs("apps.vault.ratelimit", "ERROR") as logs:
            r = self.client.post("/unlock/", {"password": PW})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(vault.is_unsealed())
        self.assertIn("OSError", logs.output[0])

    def test_the_fallback_still_limits_guessing(self):
        with mock.patch.object(RateCounter.objects, "filter", side_effect=OSError("down")), \
                self.assertLogs("apps.vault.ratelimit", "ERROR"), self.assertLogs("apps.vault.unlock_attempts", "WARNING"):
            codes = [self.client.post("/unlock/", {"password": "bad"}).status_code for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])


class SealedAlertWithoutCacheTests(TestCase):
    """The sealed-vault alert must not depend on the cache either."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        o = override_settings(VAULT_KEY_PATH=str(self.dir / "run" / "key"),
                              VAULT_CONFIG_PATH=str(self.dir / "cfg" / "vault.json"),
                              STAFF_NOTIFY_EMAILS=["ops@example.com"])
        o.enable()
        self.addCleanup(o.disable)
        vault._key = vault._seen = None
        jobs._sealed_announced_locally = False
        self.addCleanup(setattr, jobs, "_sealed_announced_locally", False)

    def test_alert_sent_once_when_the_cache_fails(self):
        from django.core import mail

        broken = OSError("cache down")
        with mock.patch.object(cache, "add", side_effect=broken), mock.patch.object(cache, "delete", side_effect=broken), \
                self.assertLogs("apps.core.jobs", "WARNING") as logs:
            jobs.sealed_alert()
            jobs.sealed_alert()
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(any("OSError" in line for line in logs.output))
