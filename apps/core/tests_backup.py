"""backup-db.sh / restore-db.sh against stub pg_dump, pg_restore and systemctl (real bash, openssl, tar)."""
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase

SCRIPTS = Path(settings.BASE_DIR) / "deploy" / "backup"
TOOLS = ("bash", "openssl", "tar", "python3", "cmp")


class ScriptStubs(SimpleTestCase):
    """The stubs, the environment and the helpers; the tests are in the classes below."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        stubs = self.d / "bin"
        stubs.mkdir()
        (stubs / "pg_dump").write_text(
            '#!/bin/sh\necho "dump-argv:$*" >> "$STUB_LOG"\necho "dump-cwd:$(pwd)" >> "$STUB_LOG"\n'
            'echo "pgpassword:${PGPASSWORD:-unset} db:$PGDATABASE" >> "$STUB_LOG"\n'
            'echo "host:${PGHOST-unset} port:${PGPORT-unset} user:${PGUSER-unset} passfile:${PGPASSFILE:+set}" >> "$STUB_LOG"\n'
            '[ -z "${PGPASSFILE:-}" ] || cat "$PGPASSFILE" > "$STUB_LOG.pgpass"\n'
            'printf "PGDMP-fake-dump"\n')
        (stubs / "pg_restore").write_text(
            '#!/bin/sh\necho "restore-argv:$*" >> "$STUB_LOG"\necho "restore-cwd:$(pwd)" >> "$STUB_LOG"\n'
            'cat > "$STUB_RESTORED"\n')
        for f in stubs.iterdir():
            f.chmod(0o755)
        # systemctl in a directory of its own, so one test can take it off the PATH. A unit named in
        # $STUB_ACTIVE is active; every other one is not (exit 3, as systemctl answers for inactive).
        # `show -p LoadState --value` says "loaded" for the units in $STUB_LOADED (all three when unset)
        # and "not-found" for the others, as systemctl does for a unit that is not installed.
        self.systemd = self.d / "systemd-bin"
        self.systemd.mkdir()
        systemctl = self.systemd / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\necho "systemctl:$*" >> "$SYSTEMCTL_LOG"\n'
            'case "$1" in\n'
            '  show) for unit in ${STUB_LOADED-pay-web.socket pay-web pay-worker}; do\n'
            '          [ "$unit" = "$5" ] && { echo loaded; exit 0; }; done; echo not-found; exit 0 ;;\n'
            '  is-active) for unit in $STUB_ACTIVE; do [ "$unit" = "$3" ] && exit 0; done; exit 3 ;;\n'
            'esac\nexit 1\n')
        systemctl.chmod(0o755)
        self.stubs = stubs
        self.passphrase = self.d / "passphrase"
        self.passphrase.write_text("backup passphrase\n")
        self.vault_json = self.d / "vault" / "vault.json"
        self.vault_json.parent.mkdir()
        self.vault_json.write_text('{"salt": "c2FsdA==", "sentinel": "gAAAA-original"}')
        self.log = self.d / "stub.log"
        self.systemctl_log = self.d / "systemctl.log"
        self.key_file = self.d / "run" / "vault.key"
        self.env = {
            "PATH": f"{stubs}:{self.systemd}:{os.environ['PATH']}", "HOME": str(self.d),
            "BACKUP_DIR": str(self.d / "backups"), "BACKUP_PASSPHRASE_FILE": str(self.passphrase),
            "VAULT_CONFIG_PATH": str(self.vault_json), "DATABASE_URL": "postgres://pay:s3cret-pw@db:5432/pay",
            "STUB_LOG": str(self.log), "STUB_RESTORED": str(self.d / "restored.dump"),
            "VAULT_KEY_PATH": str(self.key_file), "STUB_ACTIVE": "", "SYSTEMCTL_LOG": str(self.systemctl_log),
        }

    def run_script(self, name, *args, **env):
        return subprocess.run(["bash", str(SCRIPTS / name), *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def backup(self):
        r = self.run_script("backup-db.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        archives = list((self.d / "backups").glob("pay-*.tar.enc"))
        self.assertEqual(len(archives), 1)
        return archives[0]


@skipUnless(all(shutil.which(t) for t in TOOLS), "needs bash, openssl, tar, python3 and cmp")
class BackupScriptTests(ScriptStubs):
    def test_archive_holds_the_dump_and_the_vault_config_encrypted(self):
        archive = self.backup()
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertNotIn(b"PGDMP", archive.read_bytes())
        plain = self.d / "plain.tar"
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-in", str(archive),
                        "-out", str(plain), "-pass", f"file:{self.passphrase}"], check=True)
        with tarfile.open(plain) as tar:
            self.assertEqual(sorted(tar.getnames()), ["db.dump", "vault.json"])
            self.assertEqual(tar.extractfile("db.dump").read(), b"PGDMP-fake-dump")
            self.assertEqual(tar.extractfile("vault.json").read(), self.vault_json.read_bytes())
        log = self.log.read_text()
        self.assertIn("dump-argv:--format=custom\n", log)
        self.assertNotIn("s3cret-pw", log)  # neither in argv nor in PGPASSWORD
        self.assertIn("pgpassword:unset db:pay", log)

    def test_backup_refuses_without_the_vault_config(self):
        r = self.run_script("backup-db.sh", VAULT_CONFIG_PATH=str(self.d / "missing.json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing.json", r.stderr)
        self.assertFalse(list((self.d / "backups").glob("pay-*")) if (self.d / "backups").exists() else [])

    def test_restore_puts_back_the_database_and_the_vault_config(self):
        archive = self.backup()
        self.vault_json.unlink()  # a new server: no vault.json yet
        r = self.run_script("restore-db.sh", str(archive))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.d / "restored.dump").read_bytes(), b"PGDMP-fake-dump")
        self.assertIn("restore-argv:--clean --if-exists --no-owner --dbname=pay", self.log.read_text())
        self.assertEqual(self.vault_json.read_text(), '{"salt": "c2FsdA==", "sentinel": "gAAAA-original"}')
        self.assertEqual(stat.S_IMODE(self.vault_json.stat().st_mode), 0o600)
        again = self.run_script("restore-db.sh", str(archive))  # an identical vault.json is fine
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_restore_never_overwrites_another_vault_config_without_force(self):
        archive = self.backup()
        self.vault_json.write_text('{"salt": "b3RoZXI=", "sentinel": "gAAAA-other"}')
        self.log.unlink()
        r = self.run_script("restore-db.sh", str(archive))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--force", r.stderr)
        self.assertIn("other", self.vault_json.read_text())
        self.assertFalse(self.log.exists())  # the database was not touched either
        r = self.run_script("restore-db.sh", "--force", str(archive))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("original", self.vault_json.read_text())

    def test_tcp_url_becomes_pg_variables_and_a_private_passfile(self):
        self.backup()
        log = self.log.read_text()
        self.assertIn("host:db port:5432 user:pay passfile:set\n", log)
        self.assertEqual(Path(f"{self.log}.pgpass").read_text(), "db:5432:pay:pay:s3cret-pw\n")

    def test_unix_socket_url_needs_no_password_file(self):
        # Task 11's DATABASE_URL: peer authentication over the socket, no password anywhere.
        me = pwd.getpwuid(os.geteuid()).pw_name
        r = self.run_script("backup-db.sh", DATABASE_URL="postgres:///payinv?host=/var/run/postgresql")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.log.read_text()
        self.assertIn(f"host:/var/run/postgresql port:5432 user:{me} passfile:\n", log)
        self.assertIn("db:payinv", log)
        self.assertNotIn("localhost", log)
        self.assertFalse(Path(f"{self.log}.pgpass").exists())

    def test_socket_url_variants(self):
        me = pwd.getpwuid(os.geteuid()).pw_name
        cases = {
            # no host at all: libpq's default socket directory, so PGHOST stays unset
            "postgres:///payinv": f"host:unset port:5432 user:{me} passfile:\n",
            # the user named in the URL wins over the current one; ?port= names the socket file
            "postgresql://payinv@/payinv?host=/run/postgresql&port=5433": "host:/run/postgresql port:5433 user:payinv passfile:\n",
            # a percent-encoded socket directory in the host part
            "postgres://%2Frun%2Fpostgresql/payinv": f"host:/run/postgresql port:5432 user:{me} passfile:\n",
        }
        for url, expected in cases.items():
            with self.subTest(url):
                if self.log.exists():
                    self.log.unlink()
                r = self.run_script("backup-db.sh", DATABASE_URL=url)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(expected, self.log.read_text())

    def test_socket_url_with_a_password_writes_it_for_that_socket(self):
        r = self.run_script("backup-db.sh", DATABASE_URL="postgres://payinv:pw@/payinv?host=/run/postgresql")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.log.read_text()
        self.assertIn("host:/run/postgresql port:5432 user:payinv passfile:set\n", log)
        self.assertEqual(Path(f"{self.log}.pgpass").read_text(), "/run/postgresql:5432:payinv:payinv:pw\n")
        self.assertNotIn("pw", log.replace("pgpassword", ""))


@skipUnless(all(shutil.which(t) for t in TOOLS), "needs bash, openssl, tar, python3 and cmp")
class RestoreUnderRunningUnitsTests(ScriptStubs):
    """DATA-2: a restore under running units mixed two vault keys. restore-db.sh now refuses while
    pay-web, pay-worker or pay-web.socket is active, and removes the old key after installing
    vault.json."""

    UNITS = ("pay-web.socket", "pay-web", "pay-worker")

    def test_restore_refuses_while_a_unit_is_active(self):
        archive = self.backup()
        self.vault_json.write_text('{"salt": "b3RoZXI=", "sentinel": "gAAAA-other"}')
        self.key_file.parent.mkdir()
        self.key_file.write_text("the running key")
        for unit in self.UNITS:
            with self.subTest(unit):
                self.log.unlink(missing_ok=True)
                r = self.run_script("restore-db.sh", "--force", str(archive), STUB_ACTIVE=unit)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(unit, r.stderr)
                self.assertIn("nothing restored", r.stderr)
                self.assertIn("systemctl stop pay-web.socket pay-web pay-worker", r.stderr)
                self.assertIn(f"systemctl:is-active --quiet {unit}\n", self.systemctl_log.read_text())
                self.assertFalse(self.log.exists())  # the database was not touched
                self.assertIn("other", self.vault_json.read_text())  # nor vault.json
                self.assertEqual(self.key_file.read_text(), "the running key")  # nor the key

    def test_restore_asks_about_every_unit(self):
        archive = self.backup()
        r = self.run_script("restore-db.sh", str(archive))
        self.assertEqual(r.returncode, 0, r.stderr)
        asked = self.systemctl_log.read_text().splitlines()
        self.assertEqual(asked, [f"systemctl:show -p LoadState --value {unit}" for unit in self.UNITS]
                         + [f"systemctl:is-active --quiet {unit}" for unit in self.UNITS])

    def test_restore_refuses_where_none_of_the_units_is_installed(self):
        # Docker on a host that runs systemd: is-active answers "inactive" (exit 3) for units that do not
        # exist, which looked exactly like "stopped". Containers that are running must be stopped by hand.
        archive = self.backup()
        for loaded in ("", "pay-web.socket"):
            with self.subTest(loaded=loaded):
                self.log.unlink(missing_ok=True)
                r = self.run_script("restore-db.sh", str(archive), STUB_LOADED=loaded)
                if loaded:  # one unit installed: this is the systemd layout, and is-active decides
                    self.assertEqual(r.returncode, 0, r.stderr)
                    continue
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("not-found", r.stderr)
                self.assertIn("PAY_RESTORE_SKIP_UNIT_CHECK=1", r.stderr)
                self.assertFalse(self.log.exists())  # nothing restored
        r = self.run_script("restore-db.sh", str(archive), STUB_LOADED="", PAY_RESTORE_SKIP_UNIT_CHECK="1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_key_path_must_not_be_vault_json(self):
        # The key file is removed after vault.json is installed: the same path would remove vault.json.
        archive = self.backup()
        link = self.d / "run" / "vault.key"
        link.parent.mkdir()
        link.symlink_to(self.vault_json)
        for key_path in (str(self.vault_json), str(self.vault_json.parent / "." / "vault.json"), str(link)):
            with self.subTest(key_path):
                self.log.unlink(missing_ok=True)
                r = self.run_script("restore-db.sh", str(archive), VAULT_KEY_PATH=key_path)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("VAULT_KEY_PATH", r.stderr)
                self.assertFalse(self.log.exists())
                self.assertTrue(self.vault_json.exists())

    def test_restore_removes_the_old_key_and_says_what_comes_next(self):
        archive = self.backup()
        self.vault_json.write_text('{"salt": "b3RoZXI=", "sentinel": "gAAAA-other"}')
        self.key_file.parent.mkdir()
        self.key_file.write_text("the key of the vault being replaced")
        r = self.run_script("restore-db.sh", "--force", str(archive))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("original", self.vault_json.read_text())
        self.assertFalse(self.key_file.exists())
        self.assertIn("start the units, then unseal with the backup's password", r.stdout)
        again = self.run_script("restore-db.sh", str(archive))  # no key file: nothing to remove, no error
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_restore_needs_the_key_path(self):
        archive = self.backup()
        self.log.unlink()
        env = {k: v for k, v in self.env.items() if k != "VAULT_KEY_PATH"}
        r = subprocess.run(["bash", str(SCRIPTS / "restore-db.sh"), str(archive)], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("VAULT_KEY_PATH", r.stderr)
        self.assertFalse(self.log.exists())

    def test_the_unit_check_can_be_skipped_for_docker(self):
        archive = self.backup()
        self.log.unlink()
        r = self.run_script("restore-db.sh", str(archive), STUB_ACTIVE=" ".join(self.UNITS),
                            PAY_RESTORE_SKIP_UNIT_CHECK="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.systemctl_log.exists())
        self.assertIn("restore-argv", self.log.read_text())

    def test_without_systemctl_it_refuses_unless_told_to_skip(self):
        archive = self.backup()
        tools = self.d / "tools"
        tools.mkdir()
        for name in ("bash", "openssl", "tar", "python3", "cmp", "mktemp", "mkdir", "rm", "dirname", "install",
                     "mv", "chmod", "cat", "realpath"):
            os.symlink(shutil.which(name), tools / name)
        path = f"{self.stubs}:{tools}"
        self.log.unlink()
        r = self.run_script("restore-db.sh", str(archive), PATH=path)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("PAY_RESTORE_SKIP_UNIT_CHECK=1", r.stderr)
        self.assertFalse(self.log.exists())
        r = self.run_script("restore-db.sh", str(archive), PATH=path, PAY_RESTORE_SKIP_UNIT_CHECK="1")
        self.assertEqual(r.returncode, 0, r.stderr)


@skipUnless(all(shutil.which(t) for t in TOOLS), "needs bash, openssl, tar, python3 and cmp")
@skipUnless(os.geteuid() != 0, "root can read a directory of mode 000")
class UnreadableWorkingDirectoryTests(ScriptStubs):
    """Started from a directory the account cannot read (`sudo -u payinv` from /root), GNU find could
    not return to it and backup-db.sh exited 1 after the archive was written. Both scripts work from /."""

    def run_from_unreadable(self, name, *args):
        locked = self.d / "locked"
        locked.mkdir()
        self.addCleanup(os.chmod, locked, 0o700)
        return subprocess.run(["bash", "-c", 'cd "$1" && chmod 000 . && shift && exec bash "$@"', "-",
                               str(locked), str(SCRIPTS / name), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_backup_from_an_unreadable_directory(self):
        r = self.run_from_unreadable("backup-db.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list((self.d / "backups").glob("pay-*.tar.enc"))), 1)
        self.assertIn("dump-cwd:/\n", self.log.read_text())

    def test_restore_from_an_unreadable_directory(self):
        archive = self.backup()
        r = self.run_from_unreadable("restore-db.sh", str(archive))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restore-cwd:/\n", self.log.read_text())

    def test_a_relative_archive_path_still_works(self):
        archive = self.backup()
        r = subprocess.run(["bash", str(SCRIPTS / "restore-db.sh"), archive.name], cwd=archive.parent,
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.d / "restored.dump").read_bytes(), b"PGDMP-fake-dump")
