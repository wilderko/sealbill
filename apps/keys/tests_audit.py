"""Regression tests from the invoicing audit of 2026-11-03 that concern encryption keys (DATA-6).

A client's key arrives through an invoice link, which can be forwarded. Whoever also reads the
client's mailbox could upload their own key, confirm it from the letter encrypted to it, and from
then on every invoice went to them, while the holder of the key being replaced was never told.

Now, when the contact has an active key, the upload also queues a notice encrypted to that key
(never in plaintext: when it cannot be encrypted, it is not sent). It names the new key's
fingerprint and carries a cancel link: a token stored only as its hash, working for 7 days and
once. A GET shows a page; a POST refuses the new key and keeps the old one, or makes it active
again if the new key was confirmed meanwhile. A key staff added since is never overridden.

`KeySwapWithoutTellingTheActiveKeyTest` is ported from `docs/superpowers/audit/2026-11-03-invoicing/
poc/data/poc_data_3.py` in the liberation.services repository.
"""
import base64
import datetime
import email
import re
import shutil
import tempfile
from email import policy
from pathlib import Path
from unittest import mock

from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone, translation

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices import tokens
from apps.invoices.services import create_invoice, send_invoice
from apps.keys import delivery
from apps.keys import services as keys
from apps.keys.models import EncryptionKey
from apps.keys.tests_delivery import header, openssl_decrypt, parsed, parts_of, pgp_decrypt
from apps.keys.tests_validation import ALICE_FPR, fixture, make_cert, pem, rsa_key
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks.models import WebhookEndpoint, WebhookEvent

LINE = {"name": "Service", "quantity": 1, "unit_price_minor": 133700}
LANGS = ("en", "sk", "cs", "de", "es", "fr", "pl", "ko", "ru")
LEAKS = ("{%", "{{", "{#")
CANCEL_LINK = re.compile(r"https?://\S+?/k/([A-Za-z0-9_-]+)/cancel/")
ALICE_SHOWN = keys.format_fingerprint(ALICE_FPR)
EXPIRED_FPR = "4C5584E3EE55BCD757CB29267B6041E3FCE0C04C"
EN_SUBJECT = "New encryption key for your e-mail from Seller s.r.o."
SK_SUBJECT = "Nový šifrovací kľúč pre vaše e-maily od Seller s.r.o."
EN_CANCEL_TITLE = "Cancel the new encryption key"
EN_CANCELLED = "The new key was refused and will not be used."
EN_REFUSED = "The new key could not be cancelled"
EN_NO_LONGER = "This link has expired. If a key you did not upload is in use, write to us and we will remove it."


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Every throw-away keyring of these tests in a directory of their own, as DeliveryBase does: a
        # gpg-agent left over from another test's home must never serve this one (red-team report T1).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        override = override_settings(GPG_TMP_DIR=tmp)
        override.enable()
        self.addCleanup(override.disable)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.",
                                               email_from="billing@seller.example")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "alice@example.test", legal_name="Alice Example")


class KeySwapWithoutTellingTheActiveKeyTest(Base):
    """DATA-6: a forwarded view link replaced the client's active key; the holder of that key was
    never told. The confirmation went to the new key only."""

    def test_active_key_holder_is_told_about_a_replacement(self):
        repo = Path(__import__("apps").__file__).resolve().parent / "keys" / "fixtures"
        active = keys.add_key(self.contact, (repo / "alice_pub.asc").read_text(), source="staff")
        self.assertEqual(active.state, "active")
        before = set(OutboxMessage.objects.values_list("pk", flat=True))
        # Anyone holding the invoice view link uploads their own certificate naming the address.
        pending = keys.add_key(self.contact, pem(make_cert(email="alice@example.test")), source="client")
        letters = list(OutboxMessage.objects.exclude(pk__in=before).order_by("pk"))
        to_active = [m for m in letters if m.key_id == active.pk]
        self.assertTrue(to_active, "the holder of the active key is not told that it is about to be replaced")
        self.assertEqual(pending.state, "pending")


@override_settings(SITE_URL="https://pay.example")
class NoticeBase(Base):
    """The contact's active key is an S/MIME certificate staff added; the client uploads Alice's PGP key.
    So the notice is S/MIME (opened with the certificate's private key) and the confirmation PGP."""

    def setUp(self):
        super().setUp()
        self.cert = make_cert()
        self.active = keys.add_key(self.contact, pem(self.cert), source="staff")

    def upload(self, material=None):
        """A client upload: (the new key, {"confirm": token, "cancel": token}). The tokens are caught
        on their way into the letters, and told apart by the hashes stored on the key."""
        issued = []
        real = tokens.new_token

        def catch():
            plain, digest = real()
            issued.append(plain)
            return plain, digest

        with mock.patch.object(tokens, "new_token", catch):
            key = keys.add_key(self.contact, material or fixture("alice_pub.asc"), source="client")
        found = {}
        for plain in issued:
            if tokens.hash_token(plain) == key.confirm_token_hash:
                found["confirm"] = plain
            if tokens.hash_token(plain) == key.cancel_token_hash:
                found["cancel"] = plain
        return key, found

    def open_letters(self):
        """Flush; {"notice": inner, "confirm": inner, ...}, each opened with the key it was encrypted to."""
        notify.flush_outbox()
        found = {}
        for message in mail.outbox:
            outer = parsed(message)
            if outer.get_content_type() == "application/pkcs7-mime":
                plain = openssl_decrypt(base64.b64decode(outer.get_payload()), self.cert, rsa_key())
                found["notice"] = (message, outer, email.message_from_bytes(plain, policy=policy.default))
            else:
                self.assertEqual(outer.get_content_type(), "multipart/encrypted")
                plain = pgp_decrypt(outer.get_payload()[1].get_payload())
                found["confirm"] = (message, outer, email.message_from_bytes(plain, policy=policy.default))
        return found

    def cancel_url(self, token):
        return f"/k/{token}/cancel/"

    def assertHardened(self, response):
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertEqual(response["Referrer-Policy"], "same-origin")


class NoticeTest(NoticeBase):
    def test_the_notice_is_encrypted_to_the_active_key(self):
        key, issued = self.upload()
        notice = OutboxMessage.objects.get(kind="key_replacement")
        self.assertEqual((notice.key_id, notice.contact_id), (self.active.pk, self.contact.pk))
        letters = self.open_letters()
        self.assertEqual(sorted(letters), ["confirm", "notice"])
        message, outer, inner = letters["notice"]
        self.assertEqual(message.to, ["alice@example.test"])
        self.assertEqual(header(outer, "Subject"), "Message from Seller s.r.o.")  # the real one is inside
        self.assertEqual(header(inner, "Subject"), EN_SUBJECT)
        parts = parts_of(inner)
        self.assertIn(ALICE_SHOWN, parts["text/plain"])  # the NEW key's fingerprint
        found = CANCEL_LINK.search(parts["text/plain"])
        self.assertIsNotNone(found, parts["text/plain"])
        self.assertTrue(found.group(0).startswith("https://pay.example/k/"))
        self.assertIn(found.group(0), parts["text/html"])
        self.assertEqual(found.group(1), issued["cancel"])
        self.assertIn("7", parts["text/plain"])
        # The token is stored as its hash only, and the link is nowhere in the clear.
        self.assertEqual(key.cancel_token_hash, tokens.hash_token(found.group(1)))
        self.assertNotIn(found.group(1), str(EncryptionKey.objects.values().get(pk=key.pk)))
        wire = message.message().as_bytes()
        self.assertNotIn(found.group(1).encode(), wire)
        self.assertNotIn(b"pay.example/k/", wire)
        self.assertIsNone(OutboxMessage.objects.get(pk=notice.pk).body_context)  # DATA-4: forgotten once sent
        # The confirmation still goes to the new key, and only to it.
        _m, _o, confirm = letters["confirm"]
        self.assertIn(f"/k/{issued['confirm']}/", parts_of(confirm)["text/plain"])
        self.assertNotIn("/cancel/", parts_of(confirm)["text/plain"])

    def test_the_notice_is_in_the_contacts_language(self):
        self.contact.preferred_language = "sk"
        self.contact.save(update_fields=["preferred_language"])
        self.upload()
        self.assertEqual(OutboxMessage.objects.get(kind="key_replacement").language, "sk")
        _m, _o, inner = self.open_letters()["notice"]
        self.assertEqual(header(inner, "Subject"), SK_SUBJECT)
        text = parts_of(inner)["text/plain"]
        self.assertIn("Odtlačok nového kľúča", text)
        for leak in LEAKS:
            self.assertNotIn(leak, text)

    def test_through_the_invoice_page_too(self):
        _inv, view, _pay = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0],
                                        email_client=False)
        r = self.client.post(f"/i/{view}/key/", {"key": fixture("alice_pub.asc")})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([(m.kind, m.key_id) for m in OutboxMessage.objects.order_by("pk")],
                         [("key_confirm", EncryptionKey.objects.get(state="pending").pk),
                          ("key_replacement", self.active.pk)])

    def test_no_notice_without_an_active_key(self):
        keys.revoke_key(self.active, None)
        self.upload()
        self.assertEqual(list(OutboxMessage.objects.values_list("kind", flat=True)), ["key_confirm"])
        self.assertIsNone(EncryptionKey.objects.get(state="pending").cancel_token_hash)

    def test_no_notice_when_staff_or_an_agent_replace_the_key(self):
        # They are trusted to name the right key (and are audited): the ruling covers client uploads.
        keys.add_key(self.contact, fixture("alice_pub.asc"), source="staff")
        self.assertFalse(OutboxMessage.objects.exists())

    def test_the_same_upload_again_sends_no_second_notice(self):
        first, _ = self.upload()
        again, _ = self.upload()
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(OutboxMessage.objects.filter(kind="key_replacement").count(), 1)

    def test_the_notice_goes_to_the_replaced_key_even_after_the_new_one_was_confirmed(self):
        # Whoever reads the mailbox confirms at once; the notice still goes to the key they pushed out.
        key, issued = self.upload()
        keys.confirm_key(issued["confirm"])
        self.active.refresh_from_db()
        self.assertEqual(self.active.state, "replaced")
        _m, _o, inner = self.open_letters()["notice"]
        self.assertIn(issued["cancel"], parts_of(inner)["text/plain"])

    def test_never_in_plaintext_when_the_active_key_cannot_encrypt(self):
        keys.revoke_key(self.active, None)
        expired = EncryptionKey.objects.create(contact=self.contact, kind="pgp", material=fixture("expired_pub.asc"),
                                               fingerprint=EXPIRED_FPR, state="active", source="staff")
        key, issued = self.upload(pem(make_cert(email="alice@example.test")))
        notice = OutboxMessage.objects.get(kind="key_replacement")
        self.assertEqual(notice.key_id, expired.pk)
        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        notice.refresh_from_db()
        self.assertEqual((notice.state, notice.last_error), ("failed", "EncryptionRefused(expired)"))
        # What went out is the confirmation, encrypted to the new certificate; the cancel link never left.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(parsed(mail.outbox[0]).get_content_type(), "application/pkcs7-mime")
        self.assertNotIn(issued["cancel"].encode(), mail.outbox[0].message().as_bytes())

    def test_a_notice_whose_key_is_gone_is_refused(self):
        self.upload()
        notice = OutboxMessage.objects.get(kind="key_replacement")
        OutboxMessage.objects.filter(pk=notice.pk).update(key=None)
        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        notice.refresh_from_db()
        self.assertEqual((notice.state, notice.last_error), ("failed", "EncryptionRefused(key_missing)"))
        self.assertFalse([m for m in mail.outbox if parsed(m).get_content_type() == "application/pkcs7-mime"])


class CancelTest(NoticeBase):
    def setUp(self):
        super().setUp()
        self.key, self.issued = self.upload()

    def states(self):
        return {k.pk: k.state for k in EncryptionKey.objects.all()}

    def test_a_get_shows_the_key_and_changes_nothing(self):
        r = self.client.get(self.cancel_url(self.issued["cancel"]) + "?lang=en")
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, EN_CANCEL_TITLE)
        self.assertContains(r, ALICE_SHOWN)
        self.assertContains(r, 'method="post"')
        self.assertContains(r, "Cancel the new key</button>")
        self.assertEqual(self.states(), {self.active.pk: "active", self.key.pk: "pending"})
        self.assertFalse(AuditEntry.objects.filter(action="key.cancel").exists())

    def test_a_post_refuses_the_new_key_and_keeps_the_old_one(self):
        r = self.client.post(self.cancel_url(self.issued["cancel"]) + "?lang=en")
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, EN_CANCELLED)
        self.assertContains(r, "Our e-mails to you stay encrypted to your current key.")
        self.assertEqual(self.states(), {self.active.pk: "active", self.key.pk: "revoked"})
        entry = AuditEntry.objects.get(action="key.cancel")
        self.assertEqual((entry.actor_username, entry.object_pk), ("client", str(self.key.pk)))
        self.assertEqual(entry.detail, {"contact": self.contact.pk, "was": "pending", "restored": None})
        # The new key's confirmation link is dead, and its letter is never sent.
        self.assertEqual(self.client.get(f"/k/{self.issued['confirm']}/").status_code, 410)
        with self.assertRaises(KeyError):
            keys.confirm_key(self.issued["confirm"])
        notify.flush_outbox()
        self.assertEqual(OutboxMessage.objects.get(kind="key_confirm").state, "cancelled")

    def test_the_link_works_once(self):
        self.assertEqual(self.client.post(self.cancel_url(self.issued["cancel"])).status_code, 200)
        for method in (self.client.get, self.client.post):
            r = method(self.cancel_url(self.issued["cancel"]) + "?lang=en")
            self.assertEqual(r.status_code, 410)
            self.assertContains(r, "This link has already been used: the new key was cancelled.", status_code=410)
        self.assertEqual(AuditEntry.objects.filter(action="key.cancel").count(), 1)

    def test_the_link_expires_after_seven_days(self):
        EncryptionKey.objects.filter(pk=self.key.pk).update(
            confirm_sent_at=timezone.now() - datetime.timedelta(days=7, seconds=1))
        for method in (self.client.get, self.client.post):
            r = method(self.cancel_url(self.issued["cancel"]) + "?lang=en")
            self.assertEqual(r.status_code, 410)
            self.assertContains(r, "This link has expired.", status_code=410)
        self.assertEqual(self.states(), {self.active.pk: "active", self.key.pk: "pending"})

    def test_an_unknown_link(self):
        r = self.client.get(self.cancel_url("not-a-token") + "?lang=en")
        self.assertEqual(r.status_code, 404)
        self.assertHardened(r)
        self.assertContains(r, "This link is not valid.", status_code=404)
        # The confirmation token is no cancel token, nor the other way round.
        self.assertEqual(self.client.post(self.cancel_url(self.issued["confirm"])).status_code, 404)
        self.assertEqual(self.client.get(f"/k/{self.issued['cancel']}/").status_code, 404)
        self.assertEqual(self.states(), {self.active.pk: "active", self.key.pk: "pending"})

    def test_after_the_new_key_was_confirmed_the_old_one_is_active_again(self):
        keys.confirm_key(self.issued["confirm"])
        self.assertEqual(self.states(), {self.active.pk: "replaced", self.key.pk: "active"})
        r = self.client.post(self.cancel_url(self.issued["cancel"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.states(), {self.active.pk: "active", self.key.pk: "revoked"})
        entry = AuditEntry.objects.get(action="key.cancel")
        self.assertEqual(entry.detail, {"contact": self.contact.pk, "was": "active", "restored": self.active.pk})
        # The next invoice goes to the old key again.
        mail.outbox.clear()
        send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])
        notify.flush_outbox()
        invoice_letter = mail.outbox[-1]
        self.assertEqual(parsed(invoice_letter).get_content_type(), "application/pkcs7-mime")

    def test_a_key_staff_added_since_is_never_overridden(self):
        keys.confirm_key(self.issued["confirm"])
        staff_key = keys.add_key(self.contact, pem(make_cert()), source="staff")
        r = self.client.post(self.cancel_url(self.issued["cancel"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.states(), {self.active.pk: "replaced", self.key.pk: "revoked", staff_key.pk: "active"})
        self.assertEqual(AuditEntry.objects.get(action="key.cancel").detail["restored"], None)

    def test_a_cancel_never_leaves_the_contact_without_a_key(self):
        # Fix round 1 (fail closed): staff withdrew the old key meanwhile, so it cannot come back. The
        # link no longer works, and the new key stays: revoking it would send every letter in plaintext.
        keys.confirm_key(self.issued["confirm"])
        keys.revoke_key(self.active, None)
        for method in (self.client.get, self.client.post):
            r = method(self.cancel_url(self.issued["cancel"]) + "?lang=en")
            self.assertEqual(r.status_code, 410)
            self.assertContains(r, EN_NO_LONGER, status_code=410)
        self.assertEqual(self.states(), {self.active.pk: "revoked", self.key.pk: "active"})
        self.assertEqual(delivery.key_for_contact(self.contact.pk).pk, self.key.pk)
        self.assertIsNone(EncryptionKey.objects.get(pk=self.key.pk).cancelled_at)

    def test_the_page_promises_encryption_only_while_a_key_remains(self):
        # Staff put another key in place, then withdrew it: the contact had no key in use before the
        # cancel, and the pending key it refuses was never used either.
        staff_key = keys.add_key(self.contact, pem(make_cert()), source="staff")
        keys.revoke_key(staff_key, None)
        r = self.client.post(self.cancel_url(self.issued["cancel"]) + "?lang=en")
        self.assertContains(r, EN_CANCELLED)
        self.assertNotContains(r, "stay encrypted")
        self.assertEqual(self.states(), {self.active.pk: "replaced", self.key.pk: "revoked", staff_key.pk: "revoked"})

    def test_the_audit_names_no_key_material(self):
        self.client.post(self.cancel_url(self.issued["cancel"]))
        detail = str(AuditEntry.objects.get(action="key.cancel").detail)
        for leak in ("PGP", ALICE_FPR, ALICE_SHOWN, self.issued["cancel"]):
            self.assertNotIn(leak, detail)


class WithdrawnKeyTest(NoticeBase):
    """Fix round 1: the key a notice names, or that a cancel would bring back, may have been withdrawn
    or may have failed. The notice is then never sent, the cancel link no longer works, a cancel never
    leaves the contact without a key, and staff hear of every cancel and every refusal."""

    def setUp(self):
        super().setUp()
        WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s", events=["staff.alert"])

    def alerts(self):
        return [e.data for e in WebhookEvent.objects.filter(type="staff.alert").order_by("pk")
                if e.data.get("kind") == "key_cancel"]

    def break_key(self, key):
        """What delivery.mark_failed leaves behind when a key stops working at send time."""
        EncryptionKey.objects.filter(pk=key.pk).update(state="failed", failed_while="active", last_error_kind="expired")

    def test_probe_a_no_notice_to_a_revoked_key(self):
        self.upload()
        keys.revoke_key(self.active, None)
        notify.flush_outbox()
        notice = OutboxMessage.objects.get(kind="key_replacement")
        self.assertEqual(notice.state, "cancelled")
        # Only the confirmation went out, to the new (PGP) key; nothing was encrypted to the revoked one.
        self.assertEqual([parsed(m).get_content_type() for m in mail.outbox], ["multipart/encrypted"])
        entry = AuditEntry.objects.get(action="outbox.cancelled", object_pk=str(notice.pk))
        self.assertEqual(entry.detail["kind"], "key_replacement")

    def test_no_notice_to_a_failed_key(self):
        self.upload()
        self.break_key(self.active)
        notify.flush_outbox()
        self.assertEqual(OutboxMessage.objects.get(kind="key_replacement").state, "cancelled")
        self.assertEqual([parsed(m).get_content_type() for m in mail.outbox], ["multipart/encrypted"])

    def test_the_encryption_path_refuses_a_withdrawn_key_as_well(self):
        # Defence in depth: should a notice to a withdrawn key ever get past the outbox's check, it is
        # still never encrypted to that key (nor sent any other way).
        self.upload()
        keys.revoke_key(self.active, None)
        with mock.patch.object(notify, "_obsolete", return_value=None), \
                self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        notice = OutboxMessage.objects.get(kind="key_replacement")
        self.assertEqual((notice.state, notice.last_error), ("failed", "EncryptionRefused(key_withdrawn)"))
        self.assertEqual([parsed(m).get_content_type() for m in mail.outbox], ["multipart/encrypted"])
        self.assertEqual(EncryptionKey.objects.get(pk=self.active.pk).state, "revoked")  # not marked failed
        from apps.core.jobs import _failure_causes

        self.assertIn("never encrypted to a key that was revoked or failed", _failure_causes([notice.last_error])[0])

    def test_probe_b_cancel_after_the_old_key_was_revoked(self):
        key, issued = self.upload()
        keys.confirm_key(issued["confirm"])
        keys.revoke_key(self.active, None)
        self.assertEqual(self.client.get(self.cancel_url(issued["cancel"]) + "?lang=en").status_code, 410)
        self.assertEqual(self.alerts(), [])  # looking is not cancelling
        r = self.client.post(self.cancel_url(issued["cancel"]) + "?lang=en")
        self.assertEqual(r.status_code, 410)
        self.assertContains(r, EN_NO_LONGER, status_code=410)
        self.assertEqual(EncryptionKey.objects.get(pk=key.pk).state, "active")
        self.assertEqual(delivery.key_for_contact(self.contact.pk).pk, key.pk)
        self.assertEqual(self.alerts(), [{"kind": "key_cancel", "key_id": key.pk, "reason": "replaced_key_withdrawn"}])
        # Every later letter is still encrypted, to the key that is in use.
        mail.outbox.clear()
        send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])
        notify.flush_outbox()
        self.assertEqual([parsed(m).get_content_type() for m in mail.outbox], ["multipart/encrypted"])

    def test_a_failed_key_is_never_brought_back(self):
        # The old key failed at send time, then the new one was confirmed and superseded it: it is
        # `replaced` now, but carries its failure. Bringing it back would make a key active that does
        # not work, so the cancel is refused and the new key stays.
        key, issued = self.upload()
        self.break_key(self.active)
        keys.confirm_key(issued["confirm"])
        old = EncryptionKey.objects.get(pk=self.active.pk)
        self.assertEqual((old.state, old.failed_while), ("replaced", "active"))
        r = self.client.post(self.cancel_url(issued["cancel"]) + "?lang=en")
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, EN_REFUSED, status_code=409)
        self.assertEqual({k.pk: k.state for k in EncryptionKey.objects.all()}, {old.pk: "replaced", key.pk: "active"})
        self.assertIsNone(EncryptionKey.objects.get(pk=key.pk).cancelled_at)  # staff may yet make it possible
        self.assertEqual(self.alerts(), [{"kind": "key_cancel", "key_id": key.pk, "reason": "cannot_restore"}])
        entry = AuditEntry.objects.get(action="key.cancel_refused")
        self.assertEqual(entry.detail, {"contact": self.contact.pk, "reason": "cannot_restore"})
        self.assertFalse(AuditEntry.objects.filter(action="key.cancel").exists())

    def test_a_cancel_tells_staff_once(self):
        key, issued = self.upload()
        self.assertEqual(self.client.post(self.cancel_url(issued["cancel"])).status_code, 200)
        self.assertEqual(self.client.post(self.cancel_url(issued["cancel"])).status_code, 410)  # used: no news
        self.assertEqual(self.alerts(), [{"kind": "key_cancel", "key_id": key.pk, "reason": "cancelled"}])

    def test_a_cancel_after_confirmation_tells_staff_too(self):
        key, issued = self.upload()
        keys.confirm_key(issued["confirm"])
        self.assertEqual(self.client.post(self.cancel_url(issued["cancel"])).status_code, 200)
        self.assertEqual(EncryptionKey.objects.get(pk=self.active.pk).state, "active")
        self.assertEqual(self.alerts(), [{"kind": "key_cancel", "key_id": key.pk, "reason": "cancelled"}])


class NineLanguagesTest(NoticeBase):
    CONTEXT = {"org": "Seller s.r.o.", "name": "Alice", "fingerprint": ALICE_SHOWN,
               "cancel_url": "https://pay.example/k/token/cancel/"}

    def render(self, template, lang, **extra):
        with translation.override(lang):
            return render_to_string(template, {**self.CONTEXT, "language": lang, **extra})

    @staticmethod
    def words(page: str) -> str:
        """What a reader sees: the body, without the lang attribute or ?lang= that differ anyway."""
        if "<main>" in page:
            body = page.split("<main>")[1]
        elif "<body" in page:
            body = page.split("<body", 1)[1].split(">", 1)[1]
        else:
            body = page
        return re.sub(r"\?lang=\w+", "", body)

    def test_the_notice_in_every_language(self):
        english = {ext: self.words(self.render(f"email/key_replacement.{ext}", "en")) for ext in ("txt", "html")}
        for lang in LANGS:
            for ext in ("txt", "html"):
                text = self.render(f"email/key_replacement.{ext}", lang)
                for leak in LEAKS:
                    self.assertNotIn(leak, text, (lang, ext))
                self.assertIn(ALICE_SHOWN, text, (lang, ext))
                self.assertIn(self.CONTEXT["cancel_url"], text, (lang, ext))
                if lang != "en":
                    # Every sentence translated: none of the English ones is left.
                    for line in english[ext].replace("<p>", "\n").replace("</p>", "\n").splitlines():
                        line = re.sub(r"<[^>]+>", "", line).strip()
                        if len(line) > 25 and "pay.example" not in line:
                            self.assertNotIn(line, text, (lang, ext))
            msg = OutboxMessage(kind="key_replacement", language=lang, subject_context={"org": "Seller s.r.o."})
            with translation.override(lang):
                subject = notify._subject(msg)
            if lang != "en":
                self.assertNotEqual(subject, "New encryption key for your e-mail from Seller s.r.o.", lang)

    def test_the_pages_in_every_language(self):
        _key, issued = self.key_and_tokens()
        english = self.client.get(self.cancel_url(issued["cancel"]) + "?lang=en").content.decode()
        for lang in LANGS:
            r = self.client.get(self.cancel_url(issued["cancel"]) + f"?lang={lang}")
            self.assertEqual(r.status_code, 200, lang)
            page = r.content.decode()
            self.assertIn(f'lang="{lang}"', page)
            for leak in LEAKS:
                self.assertNotIn(leak, page, lang)
            if lang != "en":
                for tag in ("h1", "button"):
                    shown = page.split(f"<{tag}")[1].split(f"</{tag}>")[0]
                    self.assertNotEqual(shown, english.split(f"<{tag}")[1].split(f"</{tag}>")[0], (lang, tag))
                self.assertNotEqual(self.words(page), self.words(english), lang)
        for template, extra in (("keys/cancel_refused.html", {}),
                                ("keys/cancelled.html", {"still_encrypted": True}),
                                ("keys/cancelled.html", {"still_encrypted": False}),
                                ("keys/cancel_unusable.html", {"reason": "expired"}),
                                ("keys/cancel_unusable.html", {"reason": "used"}),
                                ("keys/cancel_unusable.html", {"reason": "withdrawn"})):
            reference = self.words(self.render(template, "en", **extra))
            for lang in LANGS:
                page = self.render(template, lang, **extra)
                for leak in LEAKS:
                    self.assertNotIn(leak, page, (template, lang))
                if lang != "en":
                    for line in re.sub(r"<[^>]+>", "\n", reference).splitlines():
                        if len(line.strip()) > 20:
                            self.assertNotIn(line.strip(), page, (template, lang, extra))

    def key_and_tokens(self):
        return self.upload()


class ReplacementNoticeMigrationTest(UnsealedVaultMixin, TransactionTestCase):
    """keys.0005 adds three empty columns to a table that already holds keys (production has some)."""

    BEFORE = [("keys", "0004_encryptionkey_source_mirror")]
    AFTER = [("keys", "0005_encryptionkey_replacement_notice")]

    def migrate(self, targets):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_keys_migrate_untouched(self):
        old = self.migrate(self.BEFORE)
        org = old.get_model("orgs", "Organization").objects.create(slug="eu")
        contact = old.get_model("contacts", "Contact").objects.create(org=org)
        keys_before = old.get_model("keys", "EncryptionKey")
        common = {"contact": contact, "kind": "pgp", "material": "m", "fingerprint": ALICE_FPR, "source": "staff"}
        active = keys_before.objects.create(state="active", **common).pk
        pending = keys_before.objects.create(state="pending", confirm_token_hash="a" * 64, **common).pk
        self.migrate(self.AFTER)
        for pk, state in ((active, "active"), (pending, "pending")):
            key = EncryptionKey.objects.get(pk=pk)
            self.assertEqual((key.state, key.fingerprint), (state, ALICE_FPR))
            self.assertEqual((key.replaces_id, key.cancel_token_hash, key.cancelled_at), (None, None, None))
