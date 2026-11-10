import base64
import os
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import rmtree

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from apps.contacts.models import Contact, ContactEmail
from apps.keys import gpg
from apps.keys.models import EncryptionKey
from apps.keys.validation import KeyInfo, KeyRejected, inspect
from apps.orgs.models import Organization
from apps.vault.testing import UnsealedVaultMixin

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ALICE = ["alice@example.test"]
# Read off the fixture with `gpg --with-colons` when it was generated, not by the code under test.
ALICE_FPR = "587221D5F22B905BCC86BEB28C0DF2BDB123F4B1"
ALICE_EXPIRES = datetime.fromtimestamp(4102398000, tz=UTC)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def dearmor(armored: str) -> bytes:
    """The binary OpenPGP packets inside an armored block (headers and checksum dropped)."""
    lines = armored.strip().splitlines()
    body = lines[lines.index("") + 1:-1]
    return base64.b64decode("".join(line for line in body if not line.startswith("=")))


GPG_WRAPPER = """#!{python} -I
# {doc}
import os, sys
args = sys.argv[1:]
if "--import" in args:
    {action}
os.execv({gpg!r}, ["gpg", *args])
"""
GPG_FAILURES = {
    # The keyring cannot be written (gpg prints ERROR statuses), as on a full or broken tmpfs.
    "blocked": (
        "gpg whose keyring cannot be written while importing",
        'i = args.index("--homedir") + 1\n'
        '    blocked = os.path.join(args[i], "blocked")\n'
        '    open(blocked, "w").close()\n'
        '    args[i] = blocked',
    ),
    # gpg dies before it reports anything.
    "refuses": (
        "gpg that exits 2 on import without a word on its status line",
        'sys.stderr.write("gpg: import failed: General error\\n")\n'
        '    sys.exit(2)',
    ),
}


def failing_gpg(directory: str, mode: str) -> str:
    """The path of a gpg that fails at --import (see GPG_FAILURES) and is plain gpg otherwise."""
    doc, action = GPG_FAILURES[mode]
    path = os.path.join(directory, f"gpg-{mode}")
    with open(path, "w") as fh:
        fh.write(GPG_WRAPPER.format(python=sys.executable, doc=doc, action=action, gpg=shutil.which("gpg")))
    os.chmod(path, 0o700)
    return path


def key_usage(**on) -> x509.KeyUsage:
    flags = dict.fromkeys(
        ("digital_signature", "content_commitment", "key_encipherment", "data_encipherment", "key_agreement",
         "key_cert_sign", "crl_sign", "encipher_only", "decipher_only"), False)
    flags.update(on)
    return x509.KeyUsage(**flags)


_RSA = None


def rsa_key():
    global _RSA
    if _RSA is None:
        _RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _RSA


def make_cert(key=None, *, email="alice@example.test", san=True, subject_email=False, usage=None,
              eku=(ExtendedKeyUsageOID.EMAIL_PROTECTION,), not_before=None, not_after=None):
    """A self-signed certificate. `usage` defaults to what the key type needs to receive mail."""
    key = key or rsa_key()
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, "Test holder")]
    if subject_email:
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    name = x509.Name(attrs)
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(days=1))
        .not_valid_after(not_after or now + timedelta(days=365))
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False)
    if usage is None:
        if isinstance(key, rsa.RSAPrivateKey):
            usage = key_usage(digital_signature=True, key_encipherment=True)
        elif isinstance(key, ec.EllipticCurvePrivateKey):
            usage = key_usage(digital_signature=True, key_agreement=True)
        else:
            usage = key_usage(digital_signature=True)
    if usage is not False:
        builder = builder.add_extension(usage, critical=True)
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    algorithm = None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(key, algorithm)


def pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class PgpValidationTests(SimpleTestCase):
    def assertRejected(self, data, code, emails=ALICE):
        with self.assertRaises(KeyRejected) as cm:
            inspect(data, emails)
        self.assertEqual(cm.exception.code, code)

    def test_valid_pgp_key_accepted_with_fingerprint(self):
        info = inspect(fixture("alice_pub.asc"), ["Alice@Example.TEST"])
        self.assertIsInstance(info, KeyInfo)
        self.assertEqual(info.kind, "pgp")
        self.assertEqual(info.fingerprint, ALICE_FPR)
        self.assertEqual(info.expires_at, ALICE_EXPIRES)
        self.assertTrue(info.material.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"))
        self.assertNotIn("PRIVATE", info.material)
        # The normalized material is itself a key this function accepts, with the same fingerprint.
        self.assertEqual(inspect(info.material, ALICE).fingerprint, ALICE_FPR)

    def test_binary_pgp_key_accepted_and_armored(self):
        info = inspect(dearmor(fixture("alice_pub.asc")), ALICE)
        self.assertEqual((info.kind, info.fingerprint), ("pgp", ALICE_FPR))
        self.assertTrue(info.material.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"))

    def test_expired_pgp_refused(self):
        self.assertRejected(fixture("expired_pub.asc"), "expired")

    def test_pgp_expiry_follows_now(self):
        with self.assertRaises(KeyRejected) as cm:
            inspect(fixture("alice_pub.asc"), ALICE, now=ALICE_EXPIRES + timedelta(seconds=1))
        self.assertEqual(cm.exception.code, "expired")

    def test_revoked_pgp_refused(self):
        self.assertRejected(fixture("revoked_pub.asc"), "revoked")

    def test_subkey_expired_refused(self):
        # The primary is valid and never expires; its only encryption subkey expired in 2021.
        self.assertRejected(fixture("subkey_expired_pub.asc"), "expired")

    def test_subkey_revoked_refused(self):
        # The primary is valid and never expires; its only encryption subkey is revoked.
        self.assertRejected(fixture("subkey_revoked_pub.asc"), "revoked")

    def test_sign_only_pgp_refused_cannot_encrypt(self):
        self.assertRejected(fixture("signonly_pub.asc"), "cannot_encrypt")

    def test_two_keys_refused(self):
        self.assertRejected(fixture("two_keys_pub.asc"), "several_keys")

    def test_pgp_secret_key_refused(self):
        self.assertRejected(fixture("alice_sec.asc"), "secret_key")
        # Binary, too: the first packet is a secret-key packet.
        self.assertRejected(dearmor(fixture("alice_sec.asc")), "secret_key")

    def test_pgp_email_mismatch_refused(self):
        self.assertRejected(fixture("alice_pub.asc"), "email_mismatch", emails=["bob@example.test"])
        self.assertRejected(fixture("alice_pub.asc"), "email_mismatch", emails=[])

    def test_address_inside_a_longer_address_does_not_match(self):
        # "alice@example.test" contains "lice@example.test"; that is not the same mailbox.
        self.assertRejected(fixture("alice_pub.asc"), "email_mismatch", emails=["lice@example.test"])

    def test_user_id_read_as_latin1_is_matched_as_utf8(self):
        # python-gnupg decodes gpg's output as Latin-1; gpg prints user ids in UTF-8.
        from apps.keys import validation

        as_listed = "Žaneta <žaneta@example.test>".encode().decode("latin-1")
        self.assertEqual(validation._uid_address(as_listed), "žaneta@example.test")
        self.assertEqual(validation._uid_address("Ann (work) <ann@example.test>"), "ann@example.test")
        self.assertEqual(validation._uid_address("ann@example.test"), "ann@example.test")
        self.assertIsNone(validation._uid_address("Ann without an address"))

    def test_garbage_is_not_a_key(self):
        for data in ("", "hello", b"\x00\x01\x02",
                     "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n-----END PGP PUBLIC KEY BLOCK-----\n"):
            with self.subTest(data=data):
                self.assertRejected(data, "not_a_key")


class SmimeValidationTests(SimpleTestCase):
    def assertRejected(self, data, code, emails=ALICE):
        with self.assertRaises(KeyRejected) as cm:
            inspect(data, emails)
        self.assertEqual(cm.exception.code, code)

    def test_smime_rsa_cert_accepted(self):
        cert = make_cert()
        info = inspect(pem(cert), ["ALICE@example.test"])
        self.assertEqual(info.kind, "smime")
        self.assertEqual(info.fingerprint, cert.fingerprint(hashes.SHA256()).hex().upper())
        self.assertEqual(info.expires_at, cert.not_valid_after_utc)
        self.assertEqual(info.material, pem(cert))
        # The same certificate in DER is the same key.
        der = inspect(cert.public_bytes(serialization.Encoding.DER), ALICE)
        self.assertEqual((der.fingerprint, der.material), (info.fingerprint, info.material))

    def test_smime_ec_cert_accepted(self):
        # The address only in the subject's emailAddress, no SAN.
        cert = make_cert(ec.generate_private_key(ec.SECP256R1()), san=False, subject_email=True)
        info = inspect(pem(cert), ALICE)
        self.assertEqual(info.kind, "smime")
        self.assertEqual(info.fingerprint, cert.fingerprint(hashes.SHA256()).hex().upper())

    def test_smime_without_key_usage_extension_accepted(self):
        inspect(pem(make_cert(usage=False, eku=None)), ALICE)

    def test_smime_without_key_encipherment_refused(self):
        self.assertRejected(pem(make_cert(usage=key_usage(digital_signature=True))), "cannot_encrypt")
        ec_key = ec.generate_private_key(ec.SECP256R1())
        self.assertRejected(pem(make_cert(ec_key, usage=key_usage(digital_signature=True))), "cannot_encrypt")

    def test_smime_expired_refused(self):
        now = datetime.now(UTC)
        cert = make_cert(not_before=now - timedelta(days=400), not_after=now - timedelta(days=1))
        self.assertRejected(pem(cert), "expired")

    def test_smime_not_yet_valid_refused(self):
        now = datetime.now(UTC)
        cert = make_cert(not_before=now + timedelta(days=1), not_after=now + timedelta(days=400))
        self.assertRejected(pem(cert), "not_yet_valid")

    def test_smime_eku_without_email_protection_refused(self):
        self.assertRejected(pem(make_cert(eku=(ExtendedKeyUsageOID.CLIENT_AUTH,))), "cannot_encrypt")

    def test_smime_any_extended_key_usage_accepted(self):
        # RFC 8550 4.4.4: anyExtendedKeyUsage permits e-mail protection like emailProtection does.
        cert = make_cert(eku=(ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE,))
        self.assertEqual(inspect(pem(cert), ALICE).kind, "smime")
        both = make_cert(eku=(ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE))
        self.assertEqual(inspect(pem(both), ALICE).kind, "smime")

    def test_smime_email_mismatch_refused(self):
        self.assertRejected(pem(make_cert(email="bob@example.test")), "email_mismatch")
        self.assertRejected(pem(make_cert(san=False)), "email_mismatch")

    def test_smime_unsupported_keys_refused(self):
        self.assertRejected(pem(make_cert(ed25519.Ed25519PrivateKey.generate())), "unsupported_key")
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.assertRejected(pem(make_cert(weak)), "unsupported_key")

    def test_several_certificates_refused(self):
        self.assertRejected(pem(make_cert()) + pem(make_cert()), "several_keys")


class InputTests(SimpleTestCase):
    def assertRejected(self, data, code, emails=ALICE):
        with self.assertRaises(KeyRejected) as cm:
            inspect(data, emails)
        self.assertEqual(cm.exception.code, code)

    def test_pkcs12_refused(self):
        key, cert = rsa_key(), make_cert()
        plain = pkcs12.serialize_key_and_certificates(b"alice", key, cert, None, serialization.NoEncryption())
        locked = pkcs12.serialize_key_and_certificates(
            b"alice", key, cert, None, serialization.BestAvailableEncryption(b"a password nobody guesses"))
        certs_only = pkcs12.serialize_key_and_certificates(b"alice", None, cert, None, serialization.NoEncryption())
        for data in (plain, locked, certs_only):
            self.assertRejected(data, "pkcs12_refused")

    def test_private_key_pem_and_der_refused(self):
        key = rsa_key()
        for fmt in (serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL):
            self.assertRejected(
                key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()), "secret_key")
        self.assertRejected(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()), "secret_key")
        self.assertRejected(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.BestAvailableEncryption(b"pw")), "secret_key")
        # A certificate with its private key pasted under it still holds a private key.
        bundle = pem(make_cert()) + key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.assertRejected(bundle, "secret_key")

    def test_oversized_input_refused(self):
        padding = "\n" * (64 * 1024 + 1 - len(fixture("alice_pub.asc")))
        self.assertRejected(fixture("alice_pub.asc") + padding, "too_large")
        self.assertRejected(b"\x00" * (64 * 1024 + 1), "too_large")
        # Exactly 64 KiB is still read.
        at_limit = fixture("alice_pub.asc") + "\n" * (64 * 1024 - len(fixture("alice_pub.asc")))
        self.assertEqual(inspect(at_limit, ALICE).fingerprint, ALICE_FPR)

    def test_truncated_or_random_der_is_not_a_key(self):
        for data in (b"\x30", b"\x30\x03\x02\x01", b"\x30\x05\x02\x01\x03\x30",
                     b"\x30\x84\xff\xff\xff\xff\x02\x01\x00", b"\x30\x80\x02\x01\x03"):
            with self.subTest(data=data):
                self.assertRejected(data, "not_a_key")

    def test_rejection_carries_only_a_known_code(self):
        with self.assertRaises(ValueError):
            KeyRejected("something else")
        self.assertEqual(str(KeyRejected("expired")), "expired")


class KeyringTests(SimpleTestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(rmtree, self.base, ignore_errors=True)
        override = override_settings(GPG_TMP_DIR=self.base)
        override.enable()
        self.addCleanup(override.disable)

    def test_keyring_directory_removed_after_use(self):
        with gpg.throwaway() as g:
            home = g.gnupghome
            self.assertEqual(os.path.dirname(os.path.realpath(home)), os.path.realpath(self.base))
            self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
            self.assertEqual(g.import_keys(fixture("alice_pub.asc")).count, 1)
        self.assertFalse(os.path.exists(home))

        with self.assertRaises(RuntimeError), gpg.throwaway() as g:
            home = g.gnupghome
            raise RuntimeError("boom")
        self.assertFalse(os.path.exists(home))

        inspect(fixture("alice_pub.asc"), ALICE)
        with self.assertRaises(KeyRejected):
            inspect(fixture("revoked_pub.asc"), ALICE)
        self.assertEqual(os.listdir(self.base), [])

    def test_gpg_import_failure_is_not_a_verdict(self):
        # A keyring gpg cannot write says nothing about the key: "could not look", never not_a_key.
        bindir = tempfile.mkdtemp()
        self.addCleanup(rmtree, bindir, ignore_errors=True)
        for mode in GPG_FAILURES:
            with self.subTest(mode=mode), override_settings(GPG_BINARY=failing_gpg(bindir, mode)):
                with self.assertRaises(gpg.GpgFailed):
                    inspect(fixture("alice_pub.asc"), ALICE)
        self.assertEqual(os.listdir(self.base), [])
        # Data gpg cannot read is still a verdict on the data.
        for data in ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n-----END PGP PUBLIC KEY BLOCK-----\n",
                     dearmor(fixture("alice_pub.asc"))[:40]):
            with self.subTest(data=data[:12]), self.assertRaises(KeyRejected) as cm:
                inspect(data, ALICE)
            self.assertEqual(cm.exception.code, "not_a_key")

    def test_agent_started_for_decryption_is_stopped(self):
        with gpg.throwaway(agent=True) as g:
            home = g.gnupghome
            g.import_keys(fixture("alice_sec.asc"))
            encrypted = g.encrypt(b"hello", ALICE_FPR, always_trust=True, armor=True)
            self.assertEqual(str(g.decrypt(str(encrypted))), "hello")
        self.assertFalse(os.path.exists(home))
        running = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if home.encode() in cmdline:
                running.append(pid)
        self.assertEqual(running, [])


def make_contact(org, email):
    contact = Contact.objects.create(org=org)
    ContactEmail.objects.create(contact=contact, address=email, primary=True)
    return contact


class EncryptionKeyModelTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.")
        self.contact = make_contact(self.org, "alice@example.test")

    def make_key(self, contact, state, **kw):
        return EncryptionKey.objects.create(
            contact=contact, kind="pgp", material="-----BEGIN PGP PUBLIC KEY BLOCK-----", fingerprint="AB" * 20,
            state=state, source="staff", **kw)

    def test_material_and_fingerprint_encrypted_at_rest(self):
        info = inspect(fixture("alice_pub.asc"), ALICE)
        key = EncryptionKey.objects.create(
            contact=self.contact, kind=info.kind, material=info.material, fingerprint=info.fingerprint,
            expires_at=info.expires_at, state="active", source="staff")
        with connection.cursor() as cur:
            cur.execute("SELECT material, fingerprint, expires_at FROM keys_encryptionkey WHERE id = %s", [key.pk])
            material, fingerprint, expires_at = cur.fetchone()
        for stored in (material, fingerprint):
            self.assertTrue(stored.startswith("gAAAAA"))
        self.assertNotIn("PGP", material)
        self.assertNotIn(ALICE_FPR, fingerprint)
        self.assertNotIn(ALICE_FPR[-16:], fingerprint)
        self.assertIsNotNone(expires_at)  # plaintext: the expiry is what the watch lists sort on
        reloaded = EncryptionKey.objects.get(pk=key.pk)
        self.assertEqual((reloaded.material, reloaded.fingerprint), (info.material, ALICE_FPR))
        self.assertEqual(reloaded.expires_at, ALICE_EXPIRES)

    def test_only_one_active_key_per_contact(self):
        other = make_contact(self.org, "bob@example.test")
        self.make_key(self.contact, "active")
        for state in ("pending", "replaced", "revoked", "failed"):
            self.make_key(self.contact, state)
        self.make_key(other, "active")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_key(self.contact, "active")
        self.assertEqual(EncryptionKey.objects.filter(contact=self.contact, state="active").count(), 1)

    def test_new_key_is_pending_by_default(self):
        key = EncryptionKey.objects.create(contact=self.contact, kind="smime", material="x", fingerprint="AB",
                                           source="client")
        self.assertEqual(key.state, "pending")

    def test_confirmation_token_hash_is_unique(self):
        self.make_key(self.contact, "pending", confirm_token_hash="a" * 64)
        self.make_key(self.contact, "pending")  # NULL is not a duplicate
        self.make_key(self.contact, "pending")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_key(self.contact, "pending", confirm_token_hash="a" * 64)


class EncryptionKeyAdminTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from apps.staff.models import StaffTOTPDevice

        admin = get_user_model().objects.create_superuser("root", password="pw-root-1")
        device = StaffTOTPDevice.objects.create(user=admin, name="t", confirmed=True)
        self.client.force_login(admin)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session.save()
        org = Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.")
        info = inspect(fixture("alice_pub.asc"), ALICE)
        self.key = EncryptionKey.objects.create(
            contact=make_contact(org, "alice@example.test"), kind=info.kind, material=info.material,
            fingerprint=info.fingerprint, expires_at=info.expires_at, state="active", source="staff")

    def test_list_and_detail_show_state_and_fingerprint_never_material(self):
        r = self.client.get("/hidden-admin-xyz/keys/encryptionkey/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "active")
        self.assertNotContains(r, ALICE_FPR)  # the list loads plaintext columns only
        r = self.client.get(f"/hidden-admin-xyz/keys/encryptionkey/{self.key.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, ALICE_FPR)
        self.assertNotContains(r, "PGP PUBLIC KEY BLOCK")
        self.assertEqual(self.client.get("/hidden-admin-xyz/keys/encryptionkey/add/").status_code, 403)
