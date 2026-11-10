"""Regression tests from the invoicing audit of 2026-11-03 that concern the vault (DATA-2, DATA-5, WEB-7).

Every encrypted column and the blind-index key hang on the vault password, and `vault.json` is in
every backup: `init_vault` refuses a password under 16 characters. `/unlock/` faces the internet
while sealed: IPv6 attempts are counted per /64, a wrong password is logged (no address, no
password), and the tenth failure of an episode sends one `staff.alert`.

A process that holds the key keeps checking that `vault.json` is still the one the key came from
(its inode and mtime, one `stat` per use). When a restore puts another installation's `vault.json`
in place and the key no longer opens its sentinel, the process seals instead of writing rows no
one could ever read again (DATA-2).

`VaultPasswordStrengthTest` is ported from `docs/superpowers/audit/2026-11-03-invoicing/poc/data/
poc_data_3.py` and `RestoreUnderRunningProcessTest` from `poc_data_2.py`, in the liberation.services
repository.
"""
import io
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache, caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase, override_settings

from apps.contacts.models import Contact
from apps.notify import services as notify
from apps.orgs.models import Organization
from apps.vault.fields import UndecryptableValue
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import Vault, VaultSealed, vault
from apps.webhooks.models import WebhookEndpoint, WebhookEvent

PW = "correct horse battery"


class VaultPasswordStrengthTest(TestCase):
    """DATA-5: the one secret every encrypted column hangs on has no strength check."""

    def test_init_vault_refuses_a_trivial_password(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with self.settings(VAULT_CONFIG_PATH=str(d / "vault.json"), VAULT_KEY_PATH=str(d / "key")):
            v = Vault()
            try:
                v.init("a")
                created = (d / "vault.json").exists()
            except ValueError:
                created = False
        self.assertFalse(created, "a one-character vault password is accepted")

    def test_sixteen_characters_is_the_floor(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with self.settings(VAULT_CONFIG_PATH=str(d / "vault.json"), VAULT_KEY_PATH=str(d / "key")):
            with self.assertRaises(ValueError):
                Vault().init("x" * 15)
            self.assertFalse((d / "vault.json").exists())
            Vault().init("x" * 16)
            self.assertTrue((d / "vault.json").exists())

    def test_the_command_says_why(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with self.settings(VAULT_CONFIG_PATH=str(d / "vault.json"), VAULT_KEY_PATH=str(d / "key")):
            with mock.patch("sys.stdin", io.StringIO("short one\n")), self.assertRaises(CommandError) as caught:
                call_command("init_vault", stdout=io.StringIO())
            self.assertIn("16 characters", str(caught.exception))
            self.assertFalse((d / "vault.json").exists())
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO("a long generated passphrase\n")):
                call_command("init_vault", stdout=out)
            self.assertIn("Vault initialised.", out.getvalue())


class UnlockBase(TestCase):
    """A sealed, initialised vault on temp paths; PBKDF2 cut down so twenty wrong passwords are quick."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        o = override_settings(VAULT_KEY_PATH=str(self.dir / "run" / "key"),
                              VAULT_CONFIG_PATH=str(self.dir / "cfg" / "vault.json"))
        o.enable()
        self.addCleanup(o.disable)
        self.enterContext(mock.patch("apps.vault.vault.ITERATIONS", 1000))
        vault._key = None
        vault._seen = None
        caches["ratelimit"].clear(); cache.clear()
        self.addCleanup(vault.seal)
        vault.init(PW)

    def wrong(self, ip):
        return self.client.post("/unlock/", {"password": "not the password"}, HTTP_X_FORWARDED_FOR=ip)


class UnlockIPv6Test(UnlockBase):
    def setUp(self):
        super().setUp()
        self.enterContext(self.assertLogs("apps.vault", "WARNING"))  # every wrong password is logged

    def test_attempts_from_one_ipv6_slash_64_share_a_bucket(self):
        for i in range(5):
            self.assertEqual(self.wrong(f"2001:db8:1:2::{i + 1:x}").status_code, 401)
        # Another address of the same /64: one more host does not buy five more guesses.
        self.assertEqual(self.wrong("2001:db8:1:2:ffff:ffff:ffff:fffe").status_code, 429)
        # The next /64 is somebody else's.
        self.assertEqual(self.wrong("2001:db8:1:3::1").status_code, 401)

    def test_ipv4_keeps_its_own_address_and_a_mapped_ipv4_is_that_address(self):
        for _ in range(5):
            self.assertEqual(self.wrong("198.51.100.7").status_code, 401)
        self.assertEqual(self.wrong("::ffff:198.51.100.7").status_code, 429)
        self.assertEqual(self.wrong("198.51.100.8").status_code, 401)

    def test_an_unparsable_address_is_its_own_bucket_and_no_500(self):
        for _ in range(5):
            self.assertEqual(self.wrong("not-an-address").status_code, 401)
        self.assertEqual(self.wrong("not-an-address").status_code, 429)

    def test_the_bucket_names(self):
        from apps.vault.ratelimit import address_bucket

        self.assertEqual(address_bucket("2001:DB8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(address_bucket("fe80::1%eth0"), "fe80::/64")
        self.assertEqual(address_bucket("198.51.100.7"), "198.51.100.7")
        self.assertEqual(address_bucket("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(address_bucket(""), "")
        self.wrong("2001:db8::1")  # the context above wants one log line


class UnlockFailureLogTest(UnlockBase):
    def test_a_wrong_password_is_logged_without_address_or_password(self):
        with self.assertLogs("apps.vault", "WARNING") as logs:
            self.assertEqual(self.wrong("2001:db8:1:2::7").status_code, 401)
        self.assertEqual([r.getMessage() for r in logs.records], ["vault unlock: wrong password"])
        for record in logs.records:
            self.assertNotIn("2001:db8", record.getMessage())
            self.assertNotIn("not the password", record.getMessage())
            self.assertEqual(record.args, ())

    def test_the_right_password_logs_no_warning(self):
        with self.assertNoLogs("apps.vault", "WARNING"):
            self.assertEqual(self.client.post("/unlock/", {"password": PW}).status_code, 302)


class UnlockFailureAlertTest(UnlockBase):
    def setUp(self):
        super().setUp()
        vault.unseal(PW)  # the endpoint's URL and secret are encrypted
        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        WebhookEndpoint.objects.create(org=org, url="https://hook.example/x", secret="s", events=["staff.alert"])
        vault.seal()

    def alerts(self):
        return list(WebhookEvent.objects.filter(type="staff.alert", data__kind="vault_unlock_failures"))

    def test_the_tenth_failure_of_an_episode_sends_one_staff_alert(self):
        with self.assertLogs("apps.vault", "WARNING"):
            for i in range(9):
                self.wrong(f"198.51.100.{i}")
            self.assertEqual(self.alerts(), [])
            self.wrong("198.51.100.9")
            self.assertEqual(len(self.alerts()), 1)
            self.assertEqual(self.alerts()[0].data, {"kind": "vault_unlock_failures", "count": 10})
            for i in range(10, 25):
                self.wrong(f"198.51.100.{i}")
        self.assertEqual(len(self.alerts()), 1)

    def test_refused_requests_are_not_failures(self):
        # The rate limit answers 429 before any password is tried: nothing was guessed.
        with self.assertLogs("apps.vault", "WARNING"):
            for _ in range(15):
                self.wrong("198.51.100.1")
        self.assertEqual(self.alerts(), [])

    def test_unsealing_ends_the_episode(self):
        with self.assertLogs("apps.vault", "WARNING"):
            for i in range(10):
                self.wrong(f"198.51.100.{i}")
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.client.post("/unlock/", {"password": PW}, HTTP_X_FORWARDED_FOR="192.0.2.1").status_code, 302)
        vault.seal()
        with self.assertLogs("apps.vault", "WARNING"):
            for i in range(9):
                self.wrong(f"198.51.101.{i}")
            self.assertEqual(len(self.alerts()), 1)  # a new episode: nine so far
            self.wrong("198.51.101.9")
        self.assertEqual(len(self.alerts()), 2)

    def test_unseal_vault_on_the_command_line_ends_the_episode_too(self):
        with self.assertLogs("apps.vault", "WARNING"):
            for i in range(9):
                self.wrong(f"198.51.100.{i}")
        with mock.patch("sys.stdin", io.StringIO(PW + "\n")):
            call_command("unseal_vault", stdout=io.StringIO())
        vault.seal()
        with self.assertLogs("apps.vault", "WARNING"):
            self.wrong("198.51.100.20")
        self.assertEqual(self.alerts(), [])


BACKUP_PASSWORD = "the backup's vault password"


class RestoreUnderRunningProcessTest(UnsealedVaultMixin, TestCase):
    """DATA-2 (poc_data_2): `restore-db.sh --force` replaced the database and vault.json under running
    processes, which never looked at vault.json again once unsealed. They kept the previous key in
    RAM, failed every restored letter for good, and encrypted every row they wrote afterwards with a
    key the restored vault cannot derive. This test plays that sequence inside one process.

    Adapted: the running process now seals itself, so the contact written "meanwhile" is refused
    (VaultSealed, a 503 to whoever sent it) instead of being written under the old key, and is
    written once the operator has unsealed with the backup's password."""

    def test_restored_vault_json_is_noticed_by_a_running_process(self):
        org = Organization.objects.create(slug="eu")
        # The host was set up per README step 7 (init_vault + unseal_vault, key "A", running now).
        cfg_a = Path(settings.VAULT_CONFIG_PATH)

        # The backup was made under another vault ("B"): its vault.json and rows encrypted with B.
        cfg_b = Path(self.vault_dir) / "backup" / "vault.json"
        cfg_b.parent.mkdir(parents=True)
        with self.settings(VAULT_CONFIG_PATH=str(cfg_b), VAULT_KEY_PATH=str(Path(self.vault_dir) / "b.key")):
            other = Vault()
            other.init(BACKUP_PASSWORD)
            self.assertTrue(other.unseal(BACKUP_PASSWORD))
            key_b = other._key
        key_a = vault._key
        vault._key = key_b  # write the "restored" rows as the backup's installation wrote them
        restored = notify.enqueue("declared_staff", to=["ops@example.org"], language="en",
                                  context={"ref": "2026-0001", "name": "Client", "total": "1 EUR",
                                           "admin_url": "https://pay.example/"})
        vault._key = key_a

        # restore-db.sh --force: pg_restore (the rows above) then `install` + `mv` of vault.json.
        shutil.copyfile(cfg_b, cfg_a)

        # The running worker flushes the outbox: the restored letter must not be failed.
        with self.assertLogs("apps.vault", "ERROR"):
            notify.flush_outbox()
        restored.refresh_from_db(fields=["state", "last_error"])
        sealed_by_the_restore = not vault.is_unsealed()

        # The running web process takes a new contact (an API call, a staff form) meanwhile.
        try:
            with transaction.atomic():  # the request's own transaction
                new = Contact.objects.create(org=org, legal_name="Written after the restore")
        except VaultSealed:
            new = None  # refused: nothing was written under a key the restored vault cannot derive

        # Operator restarts and unseals with the restored vault's password, as the README says.
        vault.seal()
        self.assertTrue(vault.unseal(BACKUP_PASSWORD))
        if new is None:
            new = Contact.objects.create(org=org, legal_name="Written after the restore")  # sent again
        try:
            name = Contact.objects.get(pk=new.pk).legal_name
        except UndecryptableValue:
            name = None
        self.assertEqual(restored.state, "queued", "a restored letter was failed by a process holding the old key")
        self.assertEqual(name, "Written after the restore", "a row written after the restore is lost for good")
        self.assertTrue(sealed_by_the_restore)
        # And the restored letter goes out under the restored vault.
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertEqual([m.to for m in mail.outbox], [["ops@example.org"]])


OTHER_PASSWORD = "another installation's password"


class ConfigIdentityTest(TestCase):
    """The key a process holds is checked against vault.json whenever vault.json is replaced."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cfg = self.dir / "cfg" / "vault.json"
        self.key_path = self.dir / "run" / "key"
        o = override_settings(VAULT_KEY_PATH=str(self.key_path), VAULT_CONFIG_PATH=str(self.cfg))
        o.enable()
        self.addCleanup(o.disable)
        self.enterContext(mock.patch("apps.vault.vault.ITERATIONS", 1000))
        vault._key = None
        vault._seen = None
        self.addCleanup(vault.seal)
        vault.init(PW)
        self.assertTrue(vault.unseal(PW))

    def other_config(self) -> Path:
        """A vault.json another installation made (another salt, another password)."""
        d = Path(tempfile.mkdtemp(dir=self.dir))
        with self.settings(VAULT_CONFIG_PATH=str(d / "vault.json"), VAULT_KEY_PATH=str(d / "key")):
            Vault().init(OTHER_PASSWORD)
        return d / "vault.json"

    def restore(self, source: Path) -> None:
        """What restore-db.sh does: `install` beside vault.json, then `mv -f` over it (a new inode)."""
        staged = self.cfg.with_name("vault.json.restore")
        shutil.copyfile(source, staged)
        os.replace(staged, self.cfg)

    def test_another_vault_json_seals_a_running_process(self):
        self.restore(self.other_config())
        with self.assertLogs("apps.vault.vault", "ERROR") as logs:
            self.assertFalse(vault.is_unsealed())
        self.assertIn(f"Vault config {self.cfg} was replaced", logs.output[0])
        with self.assertRaises(VaultSealed):
            vault.fernet()
        with self.assertRaises(VaultSealed):
            vault.derive(b"blind-index")
        # The key file is never removed from here: another worker may have just unsealed under the new
        # vault.json and written its key there. A stale one is simply never loaded.
        self.assertTrue(self.key_path.exists())
        self.assertTrue(vault.unseal(OTHER_PASSWORD))
        self.assertTrue(vault.is_unsealed())

    def test_a_restore_of_the_same_vault_json_keeps_the_key(self):
        copy = self.dir / "copy.json"
        shutil.copyfile(self.cfg, copy)
        self.restore(copy)  # a new inode, the same salt and sentinel: this installation's own backup
        with self.assertNoLogs("apps.vault.vault", "ERROR"):
            self.assertTrue(vault.is_unsealed())
            token = vault.fernet().encrypt(b"x")
        self.assertEqual(vault.fernet().decrypt(token), b"x")

    def test_touching_vault_json_keeps_the_key(self):
        st = self.cfg.stat()
        os.utime(self.cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        with self.assertNoLogs("apps.vault.vault", "ERROR"):
            self.assertTrue(vault.is_unsealed())

    def test_an_unchanged_vault_json_costs_one_stat_and_no_read(self):
        self.assertTrue(vault.is_unsealed())
        with mock.patch.object(Vault, "_read_config", side_effect=AssertionError("vault.json read on every use")):
            for _ in range(50):
                self.assertTrue(vault.is_unsealed())
                vault.fernet()
                vault.derive(b"blind-index")

    def test_a_process_that_loaded_the_key_file_checks_too(self):
        worker = Vault()  # another gunicorn worker: it reads the key file the unseal wrote
        self.assertTrue(worker.is_unsealed())
        self.restore(self.other_config())
        with self.assertLogs("apps.vault.vault", "ERROR"):
            self.assertFalse(worker.is_unsealed())

    def test_a_worker_unsealing_under_the_restored_vault_is_picked_up(self):
        self.restore(self.other_config())
        other = Vault()
        self.assertTrue(other.unseal(OTHER_PASSWORD))  # writes the new key to the key file
        with self.assertLogs("apps.vault.vault", "ERROR"):
            self.assertTrue(vault.is_unsealed())  # drops the old key, takes the new one from the file
        self.assertEqual(vault._key, other._key)

    def test_a_missing_vault_json_is_no_verdict(self):
        # "Could not look" is not "another vault": the key stays, and the file that comes back is checked.
        self.cfg.unlink()
        with self.assertLogs("apps.vault.vault", "WARNING") as logs:
            self.assertTrue(vault.is_unsealed())
        self.assertIn("cannot be read", logs.output[0])
        with self.assertNoLogs("apps.vault.vault", "WARNING"):  # said once
            self.assertTrue(vault.is_unsealed())
        shutil.copyfile(self.other_config(), self.cfg)
        with self.assertLogs("apps.vault.vault", "ERROR"):
            self.assertFalse(vault.is_unsealed())

    def test_a_vault_json_that_does_not_parse_is_no_verdict_either(self):
        # Caught half-written, say: the key stays, the same broken file is not read on every use, and
        # the finished file is checked.
        self.cfg.write_text('{"salt": "c2Fs')
        with self.assertLogs("apps.vault.vault", "WARNING"):
            self.assertTrue(vault.is_unsealed())
        with mock.patch.object(Vault, "_read_config", side_effect=AssertionError("read again")):
            self.assertTrue(vault.is_unsealed())
        self.restore(self.other_config())
        with self.assertLogs("apps.vault.vault", "ERROR"):
            self.assertFalse(vault.is_unsealed())
