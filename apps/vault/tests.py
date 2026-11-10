import os
import stat
import tempfile
from pathlib import Path

from django.core.cache import cache, caches
from django.test import TestCase, override_settings

from apps.vault.vault import Vault, VaultSealed, vault

PW = "correct horse battery"


class VaultTestCase(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.key_path = self.dir / "run" / "key"
        self.cfg_path = self.dir / "cfg" / "vault.json"
        o = override_settings(VAULT_KEY_PATH=str(self.key_path), VAULT_CONFIG_PATH=str(self.cfg_path))
        o.enable()
        self.addCleanup(o.disable)
        vault._key = None
        vault._seen = None
        caches["ratelimit"].clear(); cache.clear()
        self.addCleanup(vault.seal)


class VaultCoreTest(VaultTestCase):
    def test_init_then_unseal_with_right_password(self):
        vault.init(PW)
        self.assertTrue(vault.unseal(PW))
        self.assertTrue(vault.is_unsealed())
        token = vault.fernet().encrypt(b"x")
        self.assertEqual(vault.fernet().decrypt(token), b"x")
        self.assertEqual(len(vault.derive(b"a")), 32)
        self.assertNotEqual(vault.derive(b"a"), vault.derive(b"b"))

    def test_init_refuses_when_config_exists(self):
        vault.init(PW)
        with self.assertRaises(FileExistsError):
            vault.init(PW)

    def test_wrong_password_stays_sealed(self):
        vault.init(PW)
        self.assertFalse(vault.unseal("nope"))
        self.assertFalse(vault.is_unsealed())
        with self.assertRaises(VaultSealed):
            vault.fernet()

    def test_key_file_written_0600_only_at_vault_key_path(self):
        vault.init(PW)
        vault.unseal(PW)
        self.assertEqual(stat.S_IMODE(os.stat(self.key_path).st_mode), 0o600)
        files = {p for p in self.dir.rglob("*") if p.is_file()}
        self.assertEqual(files, {self.key_path, self.cfg_path})

    def test_seal_removes_key_file(self):
        vault.init(PW)
        vault.unseal(PW)
        vault.seal()
        self.assertFalse(self.key_path.exists())
        self.assertFalse(vault.is_unsealed())

    def test_key_file_survives_restart(self):
        vault.init(PW)
        vault.unseal(PW)
        vault._key = None
        vault._seen = None  # simulate a new process
        self.assertTrue(vault.is_unsealed())

    def test_other_worker_unseal_is_picked_up(self):
        vault.init(PW)
        a, b = Vault(), Vault()
        self.assertFalse(a.is_unsealed())
        self.assertFalse(a.is_unsealed())
        self.assertTrue(b.unseal(PW))
        self.assertTrue(a.is_unsealed())

    def test_bad_key_file_stays_sealed(self):
        vault.init(PW)
        self.key_path.parent.mkdir(parents=True)
        self.key_path.write_bytes(b"A" * 43 + b"=")
        with self.assertLogs("apps.vault.vault", "ERROR"):
            self.assertFalse(vault.is_unsealed())


class VaultWebTest(VaultTestCase):
    def test_unlock_rate_limited_after_five_attempts(self):
        vault.init(PW)
        with self.assertLogs("apps.vault.unlock_attempts", "WARNING"):  # each wrong password is logged
            for _ in range(5):
                self.assertEqual(self.client.post("/unlock/", {"password": "bad"}).status_code, 401)
        self.assertEqual(self.client.post("/unlock/", {"password": PW}).status_code, 429)

    def test_unlock_post_when_already_unsealed_skips_the_password_check(self):
        from unittest import mock

        vault.init(PW)
        vault.unseal(PW)
        with mock.patch.object(Vault, "unseal", side_effect=AssertionError("PBKDF2 must not run")):
            r = self.client.post("/unlock/", {"password": "anything"})
        self.assertEqual(r.status_code, 302)

    def test_rate_limit_counters_live_in_their_own_cache(self):
        from django.core.cache import caches

        with self.assertLogs("apps.vault.unlock_attempts", "WARNING"):
            self.client.post("/unlock/", {"password": "x"})
        self.assertIsNotNone(caches["ratelimit"].get("rl:unlock:127.0.0.1"))
        self.assertIsNone(cache.get("rl:unlock:127.0.0.1"))

    def test_rate_limit_uses_last_forwarded_entry(self):
        vault.init(PW)
        with self.assertLogs("apps.vault.unlock_attempts", "WARNING"):
            for _ in range(5):
                self.client.post("/unlock/", {"password": "bad"}, HTTP_X_FORWARDED_FOR="1.1.1.1, 9.9.9.9")
        r = self.client.post("/unlock/", {"password": "bad"}, HTTP_X_FORWARDED_FOR="2.2.2.2, 9.9.9.9")
        self.assertEqual(r.status_code, 429)

    def test_gate_api_503_public_503_staff_redirect_when_sealed(self):
        r = self.client.get("/api/v1/invoices")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "vault_sealed"})
        self.assertEqual(self.client.get("/i/abc").status_code, 503)
        self.assertEqual(self.client.get("/p/abc").status_code, 503)
        r = self.client.get("/hidden-admin-xyz/")
        self.assertRedirects(r, "/unlock/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/unlock/").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_gate_matches_exact_paths_only(self):
        for p in ("/healthzX", "/llms.txt.bak", "/api/v1/schemaFOO", "/static"):
            r = self.client.get(p)
            self.assertIn(r.status_code, (302, 503), p)
        self.assertRedirects(self.client.get("/healthzX"), "/unlock/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/static"), "/unlock/", fetch_redirect_response=False)

    def test_gate_allows_schema_docs_llms_when_sealed(self):
        for p in ("/api/v1/schema", "/api/v1/docs", "/llms.txt"):
            self.assertNotEqual(self.client.get(p).status_code, 503, p)
            self.assertNotEqual(self.client.get(p).status_code, 302, p)

    def test_unlock_lands_on_a_page_that_exists(self):
        # It used to redirect to "/", which no view serves: the person who had just typed the
        # password correctly was shown "Not Found" (2026-11-03, the first unlock in production).
        vault.init(PW)
        r = self.client.post("/unlock/", {"password": PW})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertNotEqual(self.client.get(r["Location"]).status_code, 404)
        r = self.client.post("/unlock/", {"password": "anything"})  # already unsealed
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_healthz_reports_unsealed_after_unlock(self):
        vault.init(PW)
        self.assertEqual(self.client.get("/healthz").json()["vault"], "sealed")
        r = self.client.post("/unlock/", {"password": PW})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/healthz").json()["vault"], "unsealed")
        self.assertEqual(self.client.get("/api/v1/invoices").status_code, 401)
