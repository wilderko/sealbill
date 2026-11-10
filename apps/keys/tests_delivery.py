"""Encrypted delivery: a contact with an active key never gets a plaintext e-mail.

The ciphertext is opened the way the client would open it: PGP/MIME with alice_sec.asc in a
throw-away keyring, S/MIME with `openssl cms -decrypt` and a key generated here.
"""

import base64
import datetime
import email
import os
import subprocess
import tempfile
from email import policy
from email.header import decode_header, make_header
from pathlib import Path
from shutil import rmtree
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.models import ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.keys import delivery, gpg
from apps.keys.models import EncryptionKey
from apps.keys.tests_validation import ALICE_FPR, GPG_FAILURES, failing_gpg, fixture, make_cert, pem
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.services import record_manual_payment
from apps.staff.models import StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import UnsealedVaultMixin

LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 133700}
ALICE = "alice@example.test"
EXPIRED_FPR = "4C5584E3EE55BCD757CB29267B6041E3FCE0C04C"
SUBKEY_EXPIRED_FPR = "24AB7EC99988BA9CFDCE3EBCD8A5A45485A35A78"
SLOVAK_OUTER_SUBJECT = "Správa od Seller s.r.o."
LONG_SLOVAK_ORG = "Žilinská účtovnícka a poradenská spoločnosť pre malé a stredné podniky, s.r.o., Košice-Juh"


def parsed(message) -> email.message.EmailMessage:
    """What the SMTP backend would put on the wire, read back."""
    return email.message_from_bytes(message.message().as_bytes(linesep="\r\n"), policy=policy.default)


def header(msg, name) -> str:
    return str(make_header(decode_header(msg[name]))) if msg[name] is not None else ""


def pgp_decrypt(armored: str) -> bytes:
    with gpg.throwaway(agent=True) as g:
        g.import_keys(fixture("alice_sec.asc"))
        result = g.decrypt(armored)
        assert result.ok, result.status
        return result.data


def openssl_decrypt(der: bytes, cert, key) -> bytes:
    workdir = tempfile.mkdtemp()
    try:
        cert_path, key_path = Path(workdir, "cert.pem"), Path(workdir, "key.pem")
        cert_path.write_text(pem(cert))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        out = subprocess.run(
            ["openssl", "cms", "-decrypt", "-inform", "DER", "-recip", str(cert_path), "-inkey", str(key_path)],
            input=der, capture_output=True, check=True)
        return out.stdout
    finally:
        rmtree(workdir, ignore_errors=True)


def cms_print(der: bytes) -> str:
    return subprocess.run(["openssl", "cms", "-cmsout", "-print", "-inform", "DER"], input=der,
                          capture_output=True, check=True).stdout.decode()


def parts_of(inner: email.message.EmailMessage) -> dict:
    """{content type: decoded content} of every leaf of the decrypted inner message."""
    found = {}
    for part in inner.walk():
        if part.is_multipart():
            continue
        found[part.get_content_type()] = part.get_content()
    return found


class DeliveryBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(rmtree, self.tmp, ignore_errors=True)
        override = override_settings(GPG_TMP_DIR=self.tmp, STAFF_NOTIFY_EMAILS=["ops@example.com"])
        override.enable()
        self.addCleanup(override.disable)
        self.staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.",
                                               email_from="billing@seller.example")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, ALICE, legal_name="Alica Nováková",
                                                 preferred_language="sk")

    def add_key(self, material, *, kind="pgp", fingerprint=ALICE_FPR, state="active", contact=None):
        """A key as validation stored it when it was uploaded (it may have stopped working since)."""
        return EncryptionKey.objects.create(contact=contact or self.contact, kind=kind, material=material,
                                            fingerprint=fingerprint, state=state, source="staff")

    def add_smime_key(self, private_key, **kw):
        cert = make_cert(private_key)
        key = self.add_key(pem(cert), kind="smime", fingerprint=cert.fingerprint(hashes.SHA256()).hex().upper(), **kw)
        return key, cert

    def send(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        return send_invoice(inv)[0]

    def pay(self, invoice):
        return record_manual_payment(invoice, amount_minor=133700, rail="bank", note="", user=self.staff)

    def assert_no_keyring_left(self):
        self.assertEqual(os.listdir(self.tmp), [])

    def sign_in_staff(self):
        device = StaffTOTPDevice.objects.create(user=self.staff, name="t", confirmed=True)
        self.client.force_login(self.staff)
        r = self.client.post("/verify/", {"token": token_for(device)})
        self.assertEqual(r.status_code, 302)


class EncryptedDeliveryTests(DeliveryBase):
    def test_an_encrypted_letter_gets_half_the_pdf_budget(self):
        # Measured before encryption; PGP armor roughly doubles it against Postfix's 10 MB.
        from apps.invoices import pdf

        invoice = self.send()
        size = len(pdf.render_invoice_pdf(invoice, "sk"))
        letter = OutboxMessage.objects.get(kind="invoice")

        def again():  # the same letter queued once more (a sent row keeps no contents: DATA-4)
            OutboxMessage.objects.filter(pk=letter.pk).update(
                state="queued", sent_at=None, to=letter.to, cc=letter.cc, subject_context=letter.subject_context,
                body_context=letter.body_context)

        with override_settings(EMAIL_ATTACHMENT_BUDGET_BYTES=size + size // 2):
            self.assertEqual(notify.flush_outbox(), 1)  # no key yet: the PDF fits the whole budget
            self.assertIn("application/pdf", parts_of(parsed(mail.outbox[0])))
            self.add_key(fixture("alice_pub.asc"))
            again()
            self.assertEqual(notify.flush_outbox(), 1)
        armored = parsed(mail.outbox[1]).get_payload()[1].get_payload()
        found = parts_of(email.message_from_bytes(pgp_decrypt(armored), policy=policy.default))
        self.assertNotIn("application/pdf", found)  # over half the budget: left out, with the line
        self.assertIn("PDF sa nepodarilo priložiť", found["text/plain"])
        with override_settings(EMAIL_ATTACHMENT_BUDGET_BYTES=size * 3):
            again()
            self.assertEqual(notify.flush_outbox(), 1)
        armored = parsed(mail.outbox[2]).get_payload()[1].get_payload()
        found = parts_of(email.message_from_bytes(pgp_decrypt(armored), policy=policy.default))
        self.assertIn("application/pdf", found)  # within half the budget: attached, encrypted
        self.assert_no_keyring_left()

    def test_receipt_to_contact_with_pgp_key_is_pgp_mime(self):
        self.add_key(fixture("alice_pub.asc"))
        invoice = self.send()
        OutboxMessage.objects.all().delete()  # the invoice letter: only the receipt matters here
        self.pay(invoice)
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, SLOVAK_OUTER_SUBJECT)
        self.assertEqual(sent.body, "")
        raw = sent.message().as_bytes(linesep="\r\n")
        for secret in (invoice.number.encode(), "Nováková".encode(), b"%PDF", b"Consulting"):
            self.assertNotIn(secret, raw)

        outer = parsed(sent)
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        self.assertEqual(outer.get_param("protocol"), "application/pgp-encrypted")
        self.assertEqual(header(outer, "Subject"), SLOVAK_OUTER_SUBJECT)
        self.assertNotIn(invoice.number, header(outer, "Subject"))
        self.assertEqual(header(outer, "From"), "billing@seller.example")
        self.assertEqual(header(outer, "To"), ALICE)
        self.assertTrue(outer["Date"] and outer["Message-ID"])
        version, payload = outer.get_payload()
        self.assertEqual(version.get_content_type(), "application/pgp-encrypted")
        self.assertIn("Version: 1", version.get_payload())
        self.assertEqual(payload.get_content_type(), "application/octet-stream")
        self.assertEqual(payload.get_param("name"), "encrypted.asc")
        armored = payload.get_payload()
        self.assertTrue(armored.startswith("-----BEGIN PGP MESSAGE-----"))

        inner = email.message_from_bytes(pgp_decrypt(armored), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))  # the real subject, inside
        self.assertEqual(inner["Message-ID"], outer["Message-ID"])
        found = parts_of(inner)
        self.assertIn("Alica Nováková", found["text/plain"])
        self.assertIn("<html", found["text/html"].lower())
        self.assertTrue(found["application/pdf"].startswith(b"%PDF"))
        self.assertEqual(OutboxMessage.objects.get().state, "sent")
        self.assert_no_keyring_left()

    def test_invoice_to_contact_with_smime_rsa_is_enveloped(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _key, cert = self.add_smime_key(private)
        invoice = self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        sent = mail.outbox[0]
        outer = parsed(sent)
        self.assertEqual(outer.get_content_type(), "application/pkcs7-mime")
        self.assertEqual(outer.get_param("smime-type"), "enveloped-data")
        self.assertEqual(outer.get_param("name"), "smime.p7m")
        self.assertEqual(outer["Content-Transfer-Encoding"], "base64")
        self.assertEqual(header(outer, "Subject"), SLOVAK_OUTER_SUBJECT)
        self.assertEqual(header(outer, "To"), ALICE)
        der = base64.b64decode(outer.get_payload())
        self.assertIn("aes-256-cbc", cms_print(der))

        inner = email.message_from_bytes(openssl_decrypt(der, cert, private), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))
        found = parts_of(inner)
        self.assertIn("Alica Nováková", found["text/plain"])
        self.assertIn("<html", found["text/html"].lower())
        self.assertTrue(found["application/pdf"].startswith(b"%PDF"))

    def test_smime_ec_uses_openssl_cms(self):
        private = ec.generate_private_key(ec.SECP256R1())
        _key, cert = self.add_smime_key(private)
        invoice = self.send()
        calls = []
        real_run = subprocess.run

        def spy(args, *a, **kw):
            calls.append((list(args), os.path.exists(args[-1]) if args[1:2] == ["cms"] else None))
            return real_run(args, *a, **kw)

        with mock.patch("apps.keys.envelope.subprocess.run", side_effect=spy):
            self.assertEqual(notify.flush_outbox(), 1)
        cms = [(args, existed) for args, existed in calls if args[1:2] == ["cms"]]
        self.assertEqual(len(cms), 1)
        args, existed = cms[0]
        self.assertEqual(os.path.basename(args[0]), "openssl")
        self.assertEqual(args[1:-1], ["cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", "-recip"])
        self.assertTrue(existed)  # the certificate was there while openssl ran...
        self.assertEqual(os.path.dirname(args[-1]), self.tmp)  # on GPG_TMP_DIR
        self.assertFalse(os.path.exists(args[-1]))  # ...and is gone now
        self.assert_no_keyring_left()

        outer = parsed(mail.outbox[0])
        self.assertEqual(outer.get_content_type(), "application/pkcs7-mime")
        der = base64.b64decode(outer.get_payload())
        self.assertIn("aes-256-cbc", cms_print(der))
        inner = email.message_from_bytes(openssl_decrypt(der, cert, private), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))
        self.assertTrue(parts_of(inner)["application/pdf"].startswith(b"%PDF"))

    def test_smtp_backend_puts_only_the_envelope_on_the_wire(self):
        # locmem never flattens the message the way the SMTP backend does (as_bytes(linesep=...)).
        self.add_key(fixture("alice_pub.asc"))
        invoice = self.send()
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend"), \
                mock.patch("django.core.mail.backends.smtp.smtplib.SMTP") as smtp:
            self.assertEqual(notify.flush_outbox(), 1)
        (sender, recipients, data), _kw = smtp.return_value.sendmail.call_args
        self.assertEqual((sender, recipients), ("billing@seller.example", [ALICE]))
        self.assertIn(b"\r\n", data)
        for secret in (invoice.number.encode(), b"%PDF", b"Consulting"):
            self.assertNotIn(secret, data)
        wire = email.message_from_bytes(data, policy=policy.default)
        self.assertEqual(wire.get_content_type(), "multipart/encrypted")
        inner = email.message_from_bytes(pgp_decrypt(wire.get_payload()[1].get_payload()), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))

    def test_long_slovak_organization_name_in_outer_subject_decodes_exactly(self):
        self.assertEqual(len(LONG_SLOVAK_ORG), 90)
        self.org.legal_name = LONG_SLOVAK_ORG
        self.org.save()
        self.add_key(fixture("alice_pub.asc"))
        self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        expected = f"Správa od {LONG_SLOVAK_ORG}"
        self.assertEqual(mail.outbox[0].subject, expected)
        # Read the way a modern mail client reads it: folded words must not leave a leading space.
        self.assertEqual(str(parsed(mail.outbox[0])["Subject"]), expected)

    def test_staff_letters_never_encrypted(self):
        key = self.add_key(fixture("alice_pub.asc"))
        invoice = self.send()
        OutboxMessage.objects.all().delete()
        declared = notify.enqueue(
            "declared_staff", to=["ops@example.com"], invoice=invoice, language="en",
            context={"ref": invoice.number, "name": "Alica", "total": "1 337,00 EUR", "admin_url": "https://x/"})
        # Even naming a key, a letter to staff goes as written.
        OutboxMessage.objects.filter(pk=declared.pk).update(key=key)
        notify.enqueue("vault_sealed_staff", to=["ops@example.com"], language="en",
                       context={"count": 2, "unlock_url": "https://x/unlock/"})
        self.assertEqual(notify.flush_outbox(), 2)
        for sent in mail.outbox:
            self.assertEqual(sent.to, ["ops@example.com"])
            self.assertNotEqual(sent.message().get_content_type(), "multipart/encrypted")
            self.assertTrue(sent.body)
        self.assertEqual(mail.outbox[0].subject, f"Payment declared for {invoice.number}")

    def test_contact_without_key_gets_plaintext(self):
        invoice = self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        sent = mail.outbox[0]
        self.assertIn(invoice.number, sent.subject)
        self.assertIn("Alica Nováková", sent.body)
        self.assertEqual(sent.message().get_content_type(), "multipart/mixed")
        self.assertEqual(sent.attachments[0][2], "application/pdf")

    def test_pending_replaced_or_revoked_key_is_not_used(self):
        # pending: never confirmed; replaced/revoked: a person decided this key is no longer used.
        for state in ("pending", "replaced", "revoked"):
            self.add_key(fixture("alice_pub.asc"), state=state)
        invoice = self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertIn(invoice.number, mail.outbox[0].subject)

    def test_message_key_is_used_even_when_pending(self):
        # The key-confirmation letter (Task 4) is encrypted to the pending key it names.
        pending = self.add_key(fixture("alice_pub.asc"), state="pending")
        self.send()
        OutboxMessage.objects.update(key=pending)
        self.assertEqual(notify.flush_outbox(), 1)
        outer = parsed(mail.outbox[0])
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        self.assertTrue(pgp_decrypt(outer.get_payload()[1].get_payload()))
        self.assertEqual(EncryptionKey.objects.get(pk=pending.pk).state, "pending")


class RefusedDeliveryTests(DeliveryBase):
    def flush_refusing(self, expected_sent=0):
        with self.assertLogs("apps.notify.services", "ERROR") as logs:
            self.assertEqual(notify.flush_outbox(), expected_sent)
        for line in logs.output:
            self.assertNotIn(ALICE, line)
        return logs

    def test_expired_key_at_send_sends_nothing_and_alerts(self):
        # It was accepted when it was uploaded and has expired since.
        key = self.add_key(fixture("expired_pub.asc"), fingerprint=EXPIRED_FPR)
        self.send()
        logs = self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        msg = OutboxMessage.objects.get()
        self.assertEqual((msg.state, msg.attempts, msg.last_error), ("failed", 1, "EncryptionRefused(expired)"))
        self.assertTrue(any("EncryptionRefused" in line and "expired" in line for line in logs.output))
        key.refresh_from_db()
        self.assertEqual((key.state, key.last_error_kind, key.failed_while), ("failed", "expired", "active"))
        entry = AuditEntry.objects.get(action="key.failed")
        self.assertEqual((entry.model_label, entry.object_pk), ("keys.encryptionkey", str(key.pk)))
        self.assertEqual(entry.system_detail, {"code": "expired", "was": "active", "contact": self.contact.pk,
                                               "outbox": msg.pk})
        self.assertTrue(AuditEntry.objects.filter(action="outbox.failed", object_pk=str(msg.pk)).exists())

        from apps.core import jobs

        jobs.outbox_failed_alert()  # staff are told, by direct SMTP and in plaintext
        self.assertEqual([m.to for m in mail.outbox], [["ops@example.com"]])
        self.assertNotIn(ALICE, mail.outbox[0].body)

        self.sign_in_staff()
        page = self.client.get("/")
        self.assertContains(page, "Encryption keys that stopped working")
        self.assertContains(page, EXPIRED_FPR)
        # The key's own cell and its contact's link: the two pks are equal on SQLite only by chance.
        self.assertContains(page, f"<td>#{key.pk}</td>", html=True)
        self.assertContains(page, f'<td data-label="Contact"><a href="/contacts/{self.contact.pk}/keys/">'
                                  f'#{self.contact.pk}</a></td>', html=True)
        self.assertContains(page, '<td data-label="Why">expired</td>', html=True)
        self.assertContains(page, f"/outbox/{msg.pk}/retry/")
        self.assertContains(page, '<td data-label="Why">EncryptionRefused(expired)</td>', html=True)
        self.assertNotContains(page, "PGP PUBLIC KEY BLOCK")
        self.assertNotContains(page, ALICE)

    def test_retry_after_new_key_sends_encrypted(self):
        self.add_key(fixture("expired_pub.asc"), fingerprint=EXPIRED_FPR)
        invoice = self.send()
        self.flush_refusing()
        self.add_key(fixture("alice_pub.asc"))  # the client sent a renewed key
        notify.retry_now(OutboxMessage.objects.all())
        self.assertEqual(notify.flush_outbox(), 1)
        outer = parsed(mail.outbox[0])
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        inner = email.message_from_bytes(pgp_decrypt(outer.get_payload()[1].get_payload()), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))
        self.assertEqual(OutboxMessage.objects.values_list("state", "last_error").get(), ("sent", ""))

    def test_failed_key_blocks_plaintext_until_a_person_acts(self):
        # The receipt queued behind a refused invoice letter must not go out in plaintext just
        # because the key is now `failed` rather than `active`; nor may a retry.
        key = self.add_key(fixture("expired_pub.asc"), fingerprint=EXPIRED_FPR)
        invoice = self.send()
        self.pay(invoice)
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(sorted(OutboxMessage.objects.values_list("kind", "state")),
                         [("invoice", "failed"), ("receipt", "failed")])
        self.assertEqual(AuditEntry.objects.filter(action="key.failed").count(), 1)

        notify.retry_now(OutboxMessage.objects.all())
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(set(OutboxMessage.objects.values_list("state", flat=True)), {"failed"})

        # A person revokes the key (the contact no longer wants encrypted mail): plaintext again.
        EncryptionKey.objects.filter(pk=key.pk).update(state="revoked")
        notify.retry_now(OutboxMessage.objects.all())
        self.assertEqual(notify.flush_outbox(), 2)
        self.assertIn(invoice.number, mail.outbox[0].subject)

    def test_subkey_expired_since_upload_sends_nothing(self):
        # The primary is still valid; only the encryption subkey has expired since upload.
        key = self.add_key(fixture("subkey_expired_pub.asc"), fingerprint=SUBKEY_EXPIRED_FPR)
        self.send()
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get().state, "failed")
        key.refresh_from_db()
        self.assertEqual((key.state, key.last_error_kind), ("failed", "expired"))

    def test_key_no_longer_naming_the_contacts_address_is_refused(self):
        key = self.add_key(fixture("alice_pub.asc"))
        ContactEmail.objects.filter(contact=self.contact).delete()
        ContactEmail.objects.create(contact=self.contact, address="bob@example.test", primary=True)
        self.send()
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        key.refresh_from_db()
        self.assertEqual((key.state, key.last_error_kind), ("failed", "email_mismatch"))

    def test_revoked_upstream_key_is_refused(self):
        key = self.add_key(fixture("revoked_pub.asc"), fingerprint="A42098BC8F11CAC89930F5B056926D3A3BE74BB1")
        self.send()
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        key.refresh_from_db()
        self.assertEqual((key.state, key.last_error_kind), ("failed", "revoked"))

    def test_stored_fingerprint_must_match_the_material(self):
        key = self.add_key(fixture("alice_pub.asc"), fingerprint="AB" * 20)
        self.send()
        self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        key.refresh_from_db()
        self.assertEqual((key.state, key.last_error_kind), ("failed", "fingerprint_mismatch"))

    def test_unreadable_key_sends_nothing(self):
        key = self.add_key(fixture("alice_pub.asc"))
        with connection.cursor() as cur:  # a value no key opens
            cur.execute("UPDATE keys_encryptionkey SET material = %s WHERE id = %s", ["gAAAAAbogus", key.pk])
        self.send()
        with self.assertLogs("apps.vault.fields", "ERROR"):
            self.flush_refusing()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get().state, "failed")

    def test_openssl_missing_refuses_and_keeps_the_key(self):
        key, _cert = self.add_smime_key(ec.generate_private_key(ec.SECP256R1()))
        self.send()
        with mock.patch("apps.keys.envelope.shutil.which", return_value=None):
            logs = self.flush_refusing()
        self.assertTrue(any("openssl_missing" in line for line in logs.output))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get().state, "failed")
        key.refresh_from_db()
        self.assertEqual(key.state, "active")  # not the key's fault: it stays in use
        self.assertFalse(AuditEntry.objects.filter(action="key.failed").exists())

    def test_gpg_import_failure_at_send_retries_and_keeps_key_active(self):
        # gpg failing on our side (a full tmpfs, a broken keyring) is "could not look": the key
        # the client gave us is not blamed, the letter waits, and nothing goes unencrypted.
        key = self.add_key(fixture("alice_pub.asc"))
        invoice = self.send()
        bindir = tempfile.mkdtemp()
        self.addCleanup(rmtree, bindir, ignore_errors=True)
        for attempt, mode in enumerate(GPG_FAILURES, start=1):
            OutboxMessage.objects.update(next_attempt_at=None)
            with override_settings(GPG_BINARY=failing_gpg(bindir, mode)), \
                    self.assertLogs("apps.notify.services", "ERROR") as logs:
                self.assertEqual(notify.flush_outbox(), 0)
            self.assertFalse(any("EncryptionRefused" in line for line in logs.output), logs.output)
            msg = OutboxMessage.objects.get()
            self.assertEqual((msg.state, msg.attempts), ("queued", attempt))
            key.refresh_from_db()
            self.assertEqual((key.state, key.last_error_kind, key.failed_while), ("active", "", ""))
        self.assertEqual(mail.outbox, [])
        self.assertFalse(AuditEntry.objects.filter(action="key.failed").exists())
        self.assert_no_keyring_left()
        # gpg recovers: the same letter goes, encrypted; the contact was never blocked.
        OutboxMessage.objects.update(next_attempt_at=None)
        self.assertEqual(notify.flush_outbox(), 1)
        outer = parsed(mail.outbox[0])
        inner = email.message_from_bytes(pgp_decrypt(outer.get_payload()[1].get_payload()), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))

    def test_stored_material_that_no_longer_reads_as_one_key_is_not_blamed_on_the_key(self):
        # Material that was accepted once cannot newly become "not a key" or "several keys": if
        # it reads that way at send time, the fault is ours (or the store's), so retry.
        key = self.add_key(fixture("two_keys_pub.asc"))
        self.send()
        for attempt, material in enumerate((fixture("two_keys_pub.asc"), "no key in here"), start=1):
            EncryptionKey.objects.filter(pk=key.pk).update(material=material)
            OutboxMessage.objects.update(next_attempt_at=None)
            with self.assertLogs("apps.notify.services", "ERROR") as logs:
                self.assertEqual(notify.flush_outbox(), 0)
            self.assertTrue(any("EnvelopeError" in line for line in logs.output), logs.output)
            self.assertEqual(OutboxMessage.objects.values_list("state", "attempts").get(), ("queued", attempt))
            self.assertEqual(EncryptionKey.objects.get(pk=key.pk).state, "active")
        self.assertEqual(mail.outbox, [])

    def test_gpg_that_cannot_run_retries_without_sending(self):
        # "Could not look" is not "the key is bad": the message waits, the key stays active.
        key = self.add_key(fixture("alice_pub.asc"))
        self.send()
        with override_settings(GPG_BINARY="/nonexistent/gpg"), self.assertLogs("apps.notify.services", "ERROR"):
            self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(mail.outbox, [])
        msg = OutboxMessage.objects.get()
        self.assertEqual((msg.state, msg.attempts), ("queued", 1))
        self.assertEqual(EncryptionKey.objects.get(pk=key.pk).state, "active")

    def test_openssl_that_fails_retries_without_sending(self):
        key, _cert = self.add_smime_key(ec.generate_private_key(ec.SECP256R1()))
        self.send()
        broken = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")
        with mock.patch("apps.keys.envelope.subprocess.run", return_value=broken), \
                self.assertLogs("apps.notify.services", "ERROR") as logs:
            self.assertEqual(notify.flush_outbox(), 0)
        self.assertTrue(any("EnvelopeError" in line for line in logs.output))
        self.assertEqual(mail.outbox, [])
        msg = OutboxMessage.objects.get()
        self.assertEqual((msg.state, msg.attempts), ("queued", 1))
        self.assertEqual(EncryptionKey.objects.get(pk=key.pk).state, "active")
        self.assert_no_keyring_left()  # the certificate file is removed whatever openssl did
        OutboxMessage.objects.update(next_attempt_at=None)
        self.assertEqual(notify.flush_outbox(), 1)  # openssl works again: it goes, encrypted
        self.assertEqual(parsed(mail.outbox[0]).get_content_type(), "application/pkcs7-mime")

    def test_pending_key_that_fails_does_not_block_plaintext(self):
        # A key someone uploaded and nobody confirmed must not be able to stop the contact's mail.
        pending = self.add_key(fixture("expired_pub.asc"), fingerprint=EXPIRED_FPR, state="pending")
        self.send()
        OutboxMessage.objects.update(key=pending)
        self.flush_refusing()
        pending.refresh_from_db()
        self.assertEqual((pending.state, pending.failed_while), ("failed", "pending"))
        invoice = self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertIn(invoice.number, mail.outbox[0].subject)


class EncryptionRefusedTests(TestCase):
    def test_refusal_carries_only_a_known_code(self):
        with self.assertRaises(ValueError):
            delivery.EncryptionRefused("something else")
        self.assertEqual(delivery.EncryptionRefused("expired").code, "expired")
        self.assertTrue(delivery.EncryptionRefused("expired").blames_key)
        self.assertFalse(delivery.EncryptionRefused("openssl_missing").blames_key)


class KeysOnNeedsPersonTests(DeliveryBase):
    def test_keys_expiring_within_30_days_are_listed(self):
        now = timezone.now()
        soon = self.add_key("x", fingerprint="11" * 20)
        EncryptionKey.objects.filter(pk=soon.pk).update(expires_at=now + datetime.timedelta(days=10))
        other, _ = find_or_create_contact(self.org, "other@example.test", legal_name="O")
        later = self.add_key("x", fingerprint="22" * 20, contact=other)
        EncryptionKey.objects.filter(pk=later.pk).update(expires_at=now + datetime.timedelta(days=60))
        third, _ = find_or_create_contact(self.org, "third@example.test", legal_name="T")
        self.add_key("x", fingerprint="33" * 20, contact=third)  # never expires
        fourth, _ = find_or_create_contact(self.org, "fourth@example.test", legal_name="F")
        waiting = self.add_key("x", fingerprint="44" * 20, contact=fourth, state="pending")
        EncryptionKey.objects.filter(pk=waiting.pk).update(expires_at=now + datetime.timedelta(days=5))
        self.sign_in_staff()
        page = self.client.get("/")
        self.assertContains(page, "Encryption keys expiring within 30 days")
        self.assertContains(page, "11" * 20)
        for absent in ("22" * 20, "33" * 20, "44" * 20):
            self.assertNotContains(page, absent)

    def test_failed_key_of_a_contact_with_a_new_active_key_is_no_longer_listed(self):
        self.add_key("x", fingerprint="55" * 20, state="failed")
        self.sign_in_staff()
        self.assertContains(self.client.get("/"), "55" * 20)
        self.add_key("x", fingerprint="66" * 20)
        self.assertNotContains(self.client.get("/"), "55" * 20)

    def test_an_undecryptable_fingerprint_does_not_take_the_page_down(self):
        key = self.add_key("x", state="failed")
        with connection.cursor() as cur:
            cur.execute("UPDATE keys_encryptionkey SET fingerprint = %s WHERE id = %s", ["gAAAAAbogus", key.pk])
        self.sign_in_staff()
        with self.assertLogs("apps.vault.fields", "ERROR"):
            page = self.client.get("/")
        self.assertContains(page, "(cannot be decrypted)")
