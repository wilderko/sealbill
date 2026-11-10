"""Keys arrive three ways: staff and agents upload them and they are active at once; a client
uploads one from the invoice page and it waits, `pending`, until the client answers a letter
encrypted to that very key. Nothing happens on a GET: the page asks, a person presses Confirm.

The confirmation letter is opened the way its reader would open it, with alice_sec.asc in a
throw-away keyring, and the link is taken out of the decrypted text.
"""

import datetime
import email
import hashlib
import os
import re
import tempfile
from email import policy
from shutil import rmtree

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache, caches
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.utils import timezone, translation
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.contacts.models import ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.keys import delivery, services
from apps.keys.models import EncryptionKey
from apps.keys.tests_delivery import header, parsed, parts_of, pgp_decrypt
from apps.keys.tests_validation import ALICE_FPR, failing_gpg, fixture, make_cert, pem
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.staff.models import StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 133700}
ALICE = "alice@example.test"
LANGS = ("en", "sk", "cs", "de", "es", "fr", "pl", "ko", "ru")
LEAKS = ("{%", "{{", "{#")
LINK = re.compile(r"https?://\S+?/k/([A-Za-z0-9_-]+)/")
ALICE_SHOWN = " ".join(ALICE_FPR[i:i + 4] for i in range(0, len(ALICE_FPR), 4))
EXPIRED_FPR = "4C5584E3EE55BCD757CB29267B6041E3FCE0C04C"

# The page and letter texts the tests read, in the languages they are read in.
SK_SENT = "Na vašu adresu sme poslali potvrdzovací e-mail zašifrovaný týmto kľúčom."
SK_SUBJECT = "Potvrďte svoj šifrovací kľúč pre Seller s.r.o."
SK_EXPIRED_KEY = "Platnosť tohto kľúča vypršala."
DE_EXPIRED_KEY = "Dieser Schlüssel ist abgelaufen."
FR_SECRET_KEY = "Ceci contient une clé privée."
SK_TRY_AGAIN = "Kľúč sa teraz nepodarilo overiť."
SK_CONFIRM_BUTTON = "Potvrdiť"
SK_CONFIRMED = "Kľúč je potvrdený"
SK_LINK_EXPIRED = "Platnosť tohto odkazu vypršala."
SK_LINK_USED = "Tento odkaz už bol použitý"
SK_EMAIL_MISMATCH = "Tento kľúč neuvádza e-mailovú adresu, na ktorú vám píšeme."
SK_TOO_MANY = "Cez tento odkaz bolo nahraných priveľa kľúčov."


class FlowBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(rmtree, self.tmp, ignore_errors=True)
        override = override_settings(GPG_TMP_DIR=self.tmp, STAFF_NOTIFY_EMAILS=["ops@example.com"],
                                     SITE_URL="https://pay.example")
        override.enable()
        self.addCleanup(override.disable)
        self.staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.",
                                               email_from="billing@seller.example")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, ALICE, legal_name="Alica Nováková",
                                                 preferred_language="sk")
        self.invoice, self.view, self.pay = self.send(email_client=False)

    def send(self, *, email_client=True):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        return send_invoice(inv, email_client=email_client)

    def upload(self, material, *, lang=None, as_file=False, token=None):
        url = f"/i/{token or self.view}/key/" + (f"?lang={lang}" if lang else "")
        if as_file:
            raw = material.encode() if isinstance(material, str) else material
            return self.client.post(url, {"key_file": SimpleUploadedFile("key.asc", raw)})
        return self.client.post(url, {"key": material})

    def confirmation_letter(self):
        """Flush the outbox and open its confirmation letter as the key's holder would: (outer, inner).
        While the contact has an active key, the replacement notice to that key goes out right after it."""
        notices = OutboxMessage.objects.filter(kind="key_replacement", state="queued").count()
        self.assertEqual(notify.flush_outbox(), 1 + notices)
        outer = parsed(mail.outbox[-1 - notices])
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        inner = email.message_from_bytes(pgp_decrypt(outer.get_payload()[1].get_payload()), policy=policy.default)
        return outer, inner

    def confirmation_token(self) -> str:
        _outer, inner = self.confirmation_letter()
        return LINK.search(parts_of(inner)["text/plain"]).group(1)

    def staff_key(self):
        """An active S/MIME certificate staff uploaded for the contact earlier."""
        return services.add_key(self.contact, pem(make_cert()), source="staff", actor=self.staff)

    def failed_key(self):
        """A key that stopped working while it was the contact's active key."""
        return EncryptionKey.objects.create(contact=self.contact, kind="pgp", material=fixture("expired_pub.asc"),
                                            fingerprint=EXPIRED_FPR, state="failed", failed_while="active",
                                            last_error_kind="expired", source="staff")

    def sign_in_staff(self):
        device = StaffTOTPDevice.objects.create(user=self.staff, name="t", confirmed=True)
        self.client.force_login(self.staff)
        r = self.client.post("/verify/", {"token": token_for(device)})
        self.assertEqual(r.status_code, 302)

    def api(self, org=None, scopes=("read", "invoices:write")):
        _token, plain = ApiToken.issue(org or self.org, "agent", list(scopes))
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return client

    def assertHardened(self, response):
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def assert_no_keyring_left(self):
        self.assertEqual(os.listdir(self.tmp), [])


class ClientUploadTests(FlowBase):
    def test_client_upload_is_pending_and_mails_encrypted_confirmation(self):
        r = self.upload(fixture("alice_pub.asc"))
        self.assertRedirects(r, f"/i/{self.view}/key/?sent=1", fetch_redirect_response=False)
        r = self.client.get(r["Location"])
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, 'lang="sk"')
        self.assertContains(r, ALICE_SHOWN)
        self.assertContains(r, SK_SENT)

        key = EncryptionKey.objects.get()
        self.assertEqual((key.state, key.source, key.kind, key.fingerprint), ("pending", "client", "pgp", ALICE_FPR))
        self.assertIsNone(key.created_by)
        self.assertEqual(len(key.confirm_token_hash), 64)
        self.assertIsNotNone(key.confirm_sent_at)
        letter = OutboxMessage.objects.get()
        self.assertEqual((letter.kind, letter.key_id, letter.contact_id, letter.language),
                         ("key_confirm", key.pk, self.contact.pk, "sk"))
        entry = AuditEntry.objects.get(action="key.add")
        self.assertEqual((entry.actor_username, entry.object_pk), ("client", str(key.pk)))
        self.assertEqual((entry.detail["source"], entry.detail["state"]), ("client", "pending"))
        self.assertNotIn("PGP", str(entry.detail))

        outer, inner = self.confirmation_letter()
        self.assertEqual(outer["To"], ALICE)
        self.assertEqual(header(outer, "Subject"), "Správa od Seller s.r.o.")
        # The link is not in the clear. Not `b"/k/" not in wire`: the base64 armour holds those three
        # characters by chance in about one run of two hundred. Every header and every part outside the
        # ciphertext is read instead, and neither the link's host nor its token (below) is on the wire.
        wire = mail.outbox[0].message().as_bytes()
        for name, value in outer.items():
            self.assertNotIn("/k/", str(value), name)
        # Exactly RFC 3156's two parts: the version part, read decoded (a base64 part carrying the link
        # in the clear would show it here), and the ciphertext, which is armour and nothing else.
        self.assertEqual(outer.get_param("protocol"), "application/pgp-encrypted")
        version, ciphertext = outer.get_payload()
        self.assertEqual(version.get_content_type(), "application/pgp-encrypted")
        self.assertEqual(version.get_payload(decode=True).strip(), b"Version: 1")
        self.assertEqual(ciphertext.get_content_type(), "application/octet-stream")
        armour = ciphertext.get_payload(decode=True)
        self.assertTrue(armour.lstrip().startswith(b"-----BEGIN PGP MESSAGE-----"))
        self.assertTrue(armour.rstrip().endswith(b"-----END PGP MESSAGE-----"))
        self.assertNotIn(b"pay.example/k/", wire)  # "." is not in the armour's alphabet
        self.assertNotIn(ALICE_SHOWN.encode(), wire)
        self.assertEqual(header(inner, "Subject"), SK_SUBJECT)
        parts = parts_of(inner)
        found = LINK.search(parts["text/plain"])
        self.assertIsNotNone(found, parts["text/plain"])
        self.assertTrue(found.group(0).startswith("https://pay.example/k/"))
        self.assertIn(found.group(0), parts["text/html"])
        self.assertIn(ALICE_SHOWN, parts["text/plain"])
        token = found.group(1)
        self.assertGreaterEqual(len(token), 43)  # 32 random bytes, URL-safe base64
        self.assertNotIn(token.encode(), wire)
        self.assertEqual(key.confirm_token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(EncryptionKey.objects.values().get()))
        key.refresh_from_db()
        self.assertEqual(key.state, "pending")  # sending the letter confirms nothing
        self.assert_no_keyring_left()

    def test_key_upload_from_a_file(self):
        r = self.upload(fixture("alice_pub.asc"), as_file=True)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(EncryptionKey.objects.get().state, "pending")
        r = self.client.post(f"/i/{self.view}/key/", {})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Vložte kľúč alebo vyberte súbor.", status_code=400)

    def test_pending_key_never_encrypts_invoices(self):
        self.upload(fixture("alice_pub.asc"))
        self.assertEqual(notify.flush_outbox(), 1)  # the confirmation letter
        mail.outbox.clear()
        invoice = self.send()[0]
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertIn(invoice.number, mail.outbox[0].subject)  # plaintext: the pending key is not used
        self.assertNotIn(parsed(mail.outbox[0]).get_content_type(), ("multipart/encrypted", "application/pkcs7-mime"))

        # With an active key and a pending one, invoices go to the active key alone.
        mail.outbox.clear()
        self.staff_key()
        self.send()
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertEqual(parsed(mail.outbox[0]).get_content_type(), "application/pkcs7-mime")
        self.assertEqual(EncryptionKey.objects.filter(state="pending").count(), 1)

    def test_upload_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.upload("not a key").status_code, 400)
        r = self.upload(fixture("alice_pub.asc"))
        self.assertEqual(r.status_code, 429)
        self.assertHardened(r)
        self.assertContains(r, SK_TOO_MANY, status_code=429)
        self.assertEqual(EncryptionKey.objects.count(), 0)
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertEqual(self.client.get(f"/i/{self.view}/key/").status_code, 200)  # looking costs nothing
        # The limit is per link: another invoice's link is not affected.
        other_view = self.send(email_client=False)[1]
        self.assertEqual(self.upload(fixture("alice_pub.asc"), token=other_view).status_code, 302)

    def test_upload_result_follows_post_redirect_get(self):
        r = self.upload(fixture("alice_pub.asc"), lang="de")
        self.assertRedirects(r, f"/i/{self.view}/key/?sent=1&lang=de", fetch_redirect_response=False)
        self.assertHardened(r)
        for _ in range(2):  # refreshing the result page is a GET: nothing is uploaded or sent again
            page = self.client.get(r["Location"])
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'lang="de"')
            self.assertContains(page, ALICE_SHOWN)
            self.assertNotContains(page, f'action="/i/{self.view}/key/')  # no upload form
        self.assertEqual(EncryptionKey.objects.count(), 1)
        self.assertEqual(OutboxMessage.objects.count(), 1)
        # Another browser opening the result address sees the form, never the uploaded key.
        stranger = Client().get(r["Location"])
        self.assertEqual(stranger.status_code, 200)
        self.assertNotContains(stranger, ALICE_SHOWN)
        self.assertContains(stranger, f'action="/i/{self.view}/key/')

    def test_same_key_uploaded_twice_reuses_the_pending_row(self):
        for _ in range(3):
            r = self.upload(fixture("alice_pub.asc"))
            self.assertEqual(r.status_code, 302)
            self.assertContains(self.client.get(r["Location"]), ALICE_SHOWN)
        key = EncryptionKey.objects.get()
        self.assertEqual(key.state, "pending")
        self.assertEqual(OutboxMessage.objects.filter(kind="key_confirm").count(), 1)
        self.assertEqual(AuditEntry.objects.filter(action="key.add").count(), 1)

        # Another key is another row, with its own letter.
        self.upload(pem(make_cert()), as_file=True)
        self.assertEqual(EncryptionKey.objects.filter(state="pending").count(), 2)
        self.assertEqual(OutboxMessage.objects.filter(kind="key_confirm").count(), 2)

        # Once the first link has expired, the same key gets a fresh row and a fresh letter.
        EncryptionKey.objects.filter(pk=key.pk).update(confirm_sent_at=timezone.now() - datetime.timedelta(days=8))
        self.upload(fixture("alice_pub.asc"))
        self.assertEqual(EncryptionKey.objects.filter(kind="pgp").count(), 2)
        self.assertEqual(OutboxMessage.objects.filter(kind="key_confirm").count(), 3)

    def test_refused_key_shows_reason_in_page_language(self):
        r = self.upload(fixture("expired_pub.asc"))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, 'lang="sk"', status_code=400)
        self.assertContains(r, SK_EXPIRED_KEY, status_code=400)
        self.assertNotContains(r, "This key has expired.", status_code=400)
        r = self.upload(fixture("expired_pub.asc"), lang="de")
        self.assertContains(r, 'lang="de"', status_code=400)
        self.assertContains(r, DE_EXPIRED_KEY, status_code=400)
        r = self.upload(fixture("alice_sec.asc"), lang="fr", as_file=True)
        self.assertContains(r, FR_SECRET_KEY, status_code=400)
        self.assertEqual(EncryptionKey.objects.count(), 0)
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertFalse(AuditEntry.objects.filter(action="key.add").exists())

    def test_unknown_link_cannot_upload(self):
        self.assertEqual(self.client.post("/i/nope/key/", {"key": fixture("alice_pub.asc")}).status_code, 404)
        self.assertEqual(self.client.post(f"/i/{self.pay}/key/", {"key": fixture("alice_pub.asc")}).status_code,
                         404)  # a pay token is not a view token
        self.assertEqual(EncryptionKey.objects.count(), 0)

    def test_invoice_page_links_to_key_upload(self):
        r = self.client.get(f"/i/{self.view}/")
        self.assertContains(r, f'href="/i/{self.view}/key/"')
        self.assertContains(r, "Dostávať šifrované e-maily")
        r = self.client.get(f"/i/{self.view}/key/")
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, 'enctype="multipart/form-data"')
        self.assertContains(r, "csrfmiddlewaretoken")


class ConfirmationTests(FlowBase):
    def test_get_on_confirmation_link_changes_nothing(self):
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        audit_before = AuditEntry.objects.count()
        for _ in range(3):
            r = self.client.get(f"/k/{token}/")
            self.assertEqual(r.status_code, 200)
            self.assertHardened(r)
            self.assertContains(r, 'lang="sk"')
            self.assertContains(r, ALICE_SHOWN)
            self.assertContains(r, f'action="/k/{token}/')
            self.assertContains(r, "csrfmiddlewaretoken")
            self.assertContains(r, SK_CONFIRM_BUTTON)
        self.client.head(f"/k/{token}/")
        self.client.options(f"/k/{token}/")
        key = EncryptionKey.objects.get()
        self.assertEqual((key.state, key.confirmed_at), ("pending", None))
        self.assertEqual(AuditEntry.objects.count(), audit_before)
        self.assertIsNone(delivery.key_for_contact(self.contact.pk))

    def test_post_confirms_and_replaces_previous_key(self):
        previous = self.staff_key()
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        r = self.client.post(f"/k/{token}/")
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, SK_CONFIRMED)
        self.assertContains(r, ALICE_SHOWN)

        key = EncryptionKey.objects.get(source="client")
        self.assertEqual(key.state, "active")
        self.assertIsNotNone(key.confirmed_at)
        previous.refresh_from_db()
        self.assertEqual(previous.state, "replaced")
        entry = AuditEntry.objects.get(action="key.confirm")
        self.assertEqual((entry.actor_username, entry.object_pk), ("client", str(key.pk)))
        self.assertEqual(entry.detail["replaced"], [previous.pk])

        # The next letter is encrypted to the confirmed PGP key, not the replaced certificate.
        mail.outbox.clear()
        invoice = self.send()[0]
        self.assertEqual(notify.flush_outbox(), 1)
        outer = parsed(mail.outbox[0])
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        inner = email.message_from_bytes(pgp_decrypt(outer.get_payload()[1].get_payload()), policy=policy.default)
        self.assertIn(invoice.number, header(inner, "Subject"))

    def test_confirmation_link_expires_after_7_days(self):
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        key = EncryptionKey.objects.get()
        sent = key.confirm_sent_at
        with self.assertRaises(KeyError) as caught:
            services.confirm_key(token, now=sent + datetime.timedelta(days=7))
        self.assertEqual(caught.exception.args[0], "expired")

        EncryptionKey.objects.filter(pk=key.pk).update(confirm_sent_at=sent - datetime.timedelta(days=7, seconds=1))
        for method in (self.client.get, self.client.post):
            r = method(f"/k/{token}/")
            self.assertEqual(r.status_code, 410)
            self.assertHardened(r)
            self.assertContains(r, SK_LINK_EXPIRED, status_code=410)
            self.assertNotContains(r, f'action="/k/{token}/', status_code=410)
        key.refresh_from_db()
        self.assertEqual(key.state, "pending")

        EncryptionKey.objects.filter(pk=key.pk).update(
            confirm_sent_at=timezone.now() - datetime.timedelta(days=6, hours=23))
        self.assertEqual(self.client.post(f"/k/{token}/").status_code, 200)
        key.refresh_from_db()
        self.assertEqual(key.state, "active")

    def test_confirmation_token_single_use(self):
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        self.assertEqual(self.client.post(f"/k/{token}/").status_code, 200)
        confirmed_at = EncryptionKey.objects.get().confirmed_at
        services.revoke_key(EncryptionKey.objects.get(), self.staff)
        r = self.client.post(f"/k/{token}/")
        self.assertEqual(r.status_code, 410)
        self.assertContains(r, SK_LINK_USED, status_code=410)
        with self.assertRaises(KeyError) as caught:
            services.confirm_key(token)
        self.assertEqual(caught.exception.args[0], "used")
        with self.assertRaises(KeyError) as caught:
            services.confirm_key("not-a-token")
        self.assertEqual(caught.exception.args[0], "unknown")
        self.assertEqual(self.client.get("/k/not-a-token/").status_code, 404)
        self.assertEqual(self.client.post("/k/not-a-token/").status_code, 404)
        self.assertEqual(EncryptionKey.objects.get().confirmed_at, confirmed_at)
        self.assertEqual(AuditEntry.objects.filter(action="key.confirm").count(), 1)

    def test_repeated_confirm_shows_the_confirmed_page(self):
        # A double click: the second POST finds the key already active through this very link.
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        for _ in range(2):
            r = self.client.post(f"/k/{token}/")
            self.assertEqual(r.status_code, 200)
            self.assertHardened(r)
            self.assertContains(r, SK_CONFIRMED)
            self.assertContains(r, ALICE_SHOWN)
        r = self.client.get(f"/k/{token}/")
        self.assertContains(r, SK_CONFIRMED)
        self.assertNotContains(r, f'action="/k/{token}/')
        self.assertEqual(AuditEntry.objects.filter(action="key.confirm").count(), 1)
        self.assertEqual(EncryptionKey.objects.get().state, "active")

    def test_confirmation_token_never_reaches_the_logs(self):
        # django.request logs every 5xx with its path; production cuts the token out of it.
        import logging

        from apps.core.logfilters import RedactCapabilityTokens

        lines = []

        class Collect(logging.Handler):
            def emit(self, record):
                lines.append(self.format(record))

        handler = Collect(level=logging.DEBUG)
        handler.addFilter(RedactCapabilityTokens())
        logger = logging.getLogger("django.request")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        with override_settings(GPG_BINARY=os.path.join(self.tmp, "no-such-gpg")):
            self.assertEqual(self.client.post(f"/k/{token}/").status_code, 503)  # could not check the key
        vault.seal()
        self.assertEqual(self.client.get(f"/k/{token}/").status_code, 503)
        self.assertEqual(self.client.get(f"/i/{self.view}/key/").status_code, 503)
        self.assertGreaterEqual(len(lines), 3, lines)
        self.assertTrue(any("/k/<token>/" in line for line in lines), lines)
        self.assertTrue(any("/i/<token>/key/" in line for line in lines), lines)
        for line in lines:
            self.assertNotIn(token, line)
            self.assertNotIn(self.view, line)
        self.assertEqual(EncryptionKey.objects.values_list("state", flat=True).get(), "pending")  # sealed: plain only

    def test_sealed_and_wrong_method_answers_are_hardened(self):
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        answers = {
            "PUT /i/": self.client.put(f"/i/{self.view}/"),
            "DELETE /i/key/": self.client.delete(f"/i/{self.view}/key/"),
            "PUT /p/": self.client.put(f"/p/{self.pay}/"),
            "PUT /k/": self.client.put(f"/k/{token}/"),
        }
        for name, r in answers.items():
            self.assertEqual(r.status_code, 405, name)
            self.assertEqual((r["Cache-Control"], r["X-Robots-Tag"]), ("no-store", "noindex"), name)
        vault.seal()
        for path in (f"/i/{self.view}/", f"/p/{self.pay}/", f"/k/{token}/", f"/i/{self.view}/key/"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 503, path)
            self.assertEqual((r["Cache-Control"], r["X-Robots-Tag"]), ("no-store", "noindex"), path)
            self.assertEqual(r["Referrer-Policy"], "same-origin", path)

    def test_confirm_and_upload_need_the_pages_csrf_token(self):
        # The test client skips CSRF; a browser does not. Without the token from the page, a POST is
        # refused before the view runs: nothing is uploaded, nothing confirmed.
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(browser.post(f"/i/{self.view}/key/", {"key": fixture("alice_pub.asc")}).status_code, 403)
        self.assertEqual(EncryptionKey.objects.count(), 0)
        page = browser.get(f"/i/{self.view}/key/")
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        r = browser.post(f"/i/{self.view}/key/", {"key": fixture("alice_pub.asc"), "csrfmiddlewaretoken": csrf})
        self.assertEqual(r.status_code, 302)

        token = self.confirmation_token()
        self.assertEqual(browser.post(f"/k/{token}/").status_code, 403)
        self.assertEqual(EncryptionKey.objects.get().state, "pending")
        page = browser.get(f"/k/{token}/")
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        self.assertEqual(browser.post(f"/k/{token}/", {"csrfmiddlewaretoken": csrf}).status_code, 200)
        self.assertEqual(EncryptionKey.objects.get().state, "active")

    def test_key_that_stopped_working_before_confirmation_is_not_activated(self):
        # The contact's address changed after the upload: the key no longer names it.
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        ContactEmail.objects.filter(contact=self.contact).delete()
        ContactEmail.objects.create(contact=self.contact, address="bob@example.test", primary=True)
        r = self.client.post(f"/k/{token}/")
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, SK_EMAIL_MISMATCH, status_code=409)
        self.assertEqual(EncryptionKey.objects.get().state, "pending")
        self.assertFalse(AuditEntry.objects.filter(action="key.confirm").exists())

    def test_confirmation_link_while_sealed_is_unavailable_not_the_unlock_page(self):
        vault.seal()
        r = self.client.get("/k/whatever/?lang=de")
        self.assertEqual(r.status_code, 503)
        self.assertContains(r, 'lang="de"', status_code=503)


class StaffAndApiTests(FlowBase):
    def test_staff_upload_active_at_once(self):
        previous = self.staff_key()
        self.sign_in_staff()
        url = f"/contacts/{self.contact.pk}/keys/"
        r = self.client.post(url, {"key": fixture("alice_pub.asc")})
        self.assertRedirects(r, url, fetch_redirect_response=False)
        key = EncryptionKey.objects.get(kind="pgp")
        self.assertEqual((key.state, key.source, key.created_by_id), ("active", "staff", self.staff.pk))
        previous.refresh_from_db()
        self.assertEqual(previous.state, "replaced")
        self.assertEqual(OutboxMessage.objects.count(), 0)  # no confirmation for staff
        entry = AuditEntry.objects.filter(action="key.add", object_pk=str(key.pk)).get()
        self.assertEqual(entry.actor_username, "clerk")
        self.assertEqual(entry.detail["replaced"], [previous.pk])

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertHardened(r)
        self.assertContains(r, ALICE_SHOWN)
        self.assertContains(r, "replaced")
        self.assertNotContains(r, "BEGIN PGP")
        self.assertContains(r, f'action="/keys/{key.pk}/revoke/"')

        r = self.client.post(url, {"key_file": SimpleUploadedFile("k.asc", fixture("expired_pub.asc").encode())})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "This key has expired.", status_code=400)
        self.assertEqual(EncryptionKey.objects.count(), 2)

        r = self.client.get(f"/invoices/{self.invoice.pk}/")
        self.assertContains(r, f'href="{url}"')

    def test_api_upload_and_revoke_scoped_to_token_org(self):
        other = Organization.objects.create(slug="other")
        mine, theirs = self.api(), self.api(org=other)
        reader = self.api(scopes=("read",))
        url = f"/api/v1/contacts/{self.contact.pk}/keys"

        self.assertEqual(theirs.get(url).status_code, 404)
        self.assertEqual(theirs.post(url, {"key": fixture("alice_pub.asc")}, format="json").status_code, 404)
        self.assertEqual(reader.post(url, {"key": fixture("alice_pub.asc")}, format="json").status_code, 403)
        self.assertEqual(EncryptionKey.objects.count(), 0)

        r = mine.post(url, {"key": fixture("alice_pub.asc")}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(set(body), {"id", "kind", "fingerprint", "state", "expires_at", "source"})
        self.assertEqual((body["kind"], body["fingerprint"], body["state"], body["source"]),
                         ("pgp", ALICE_FPR, "active", "api"))
        self.assertTrue(body["expires_at"].startswith("2099-12-31"))
        self.assertNotIn("BEGIN", r.content.decode())
        self.assertEqual(OutboxMessage.objects.count(), 0)

        r = reader.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([k["id"] for k in r.json()["results"]], [body["id"]])
        self.assertNotIn("BEGIN", r.content.decode())

        r = mine.post(url, {"key": fixture("expired_pub.asc")}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"error": "key_rejected", "detail": "expired"})

        revoke = f"/api/v1/keys/{body['id']}/revoke"
        self.assertEqual(theirs.post(revoke).status_code, 404)
        self.assertEqual(reader.post(revoke).status_code, 403)
        self.assertEqual(EncryptionKey.objects.get(pk=body["id"]).state, "active")
        r = mine.post(revoke)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "revoked")
        entry = AuditEntry.objects.get(action="key.revoke")
        self.assertEqual((entry.actor_username, entry.object_pk), ("api:agent", str(body["id"])))
        self.assertEqual(mine.post(revoke).json()["state"], "revoked")  # again: harmless
        self.assertEqual(AuditEntry.objects.filter(action="key.revoke").count(), 1)


class StaffKeysPageTests(FlowBase):
    def test_staff_keys_page_caps_rows_and_collapses_expired_pending_keys(self):
        from unittest import mock

        def key(state, **kw):
            return EncryptionKey.objects.create(contact=self.contact, kind="pgp", material=fixture("alice_pub.asc"),
                                                fingerprint=ALICE_FPR, state=state, source="client", **kw)

        long_ago = timezone.now() - datetime.timedelta(days=8)
        stale = [key("pending", confirm_sent_at=long_ago) for _ in range(3)]
        listed = [key("replaced") for _ in range(3)] + [key("active")]
        fresh = key("pending", confirm_sent_at=timezone.now())
        self.sign_in_staff()
        with mock.patch("apps.staff.views.NEEDS_PERSON_CAP", 2):
            r = self.client.get(f"/contacts/{self.contact.pk}/keys/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn("Showing 2 of 5.", page)
        self.assertIn("<details>", page)
        self.assertIn("3 pending keys whose confirmation link expired", page)
        self.assertIn("Showing 2 of 3.", page)
        main, collapsed = page.split("<details>", 1)
        # Newest first, capped at two: the live pending key and the active one.
        def row(k):
            return f"<td>#{k.pk}</td>"

        self.assertIn(row(fresh), main)
        self.assertIn(row(listed[-1]), main)
        self.assertNotIn(row(listed[0]), main)
        for k in stale:
            self.assertNotIn(row(k), main)
        self.assertIn(row(stale[-1]), collapsed)


class RulingTests(FlowBase):
    def test_gpg_failure_at_upload_says_try_again_and_stores_nothing(self):
        bin_dir = tempfile.mkdtemp()
        self.addCleanup(rmtree, bin_dir, ignore_errors=True)
        self.sign_in_staff()
        agent = self.api()
        binaries = [failing_gpg(bin_dir, "blocked"), failing_gpg(bin_dir, "refuses"),
                    os.path.join(bin_dir, "no-such-gpg")]
        for binary in binaries:
            with self.subTest(gpg=os.path.basename(binary)), override_settings(GPG_BINARY=binary):
                caches["ratelimit"].clear()
                cache.clear()
                r = self.upload(fixture("alice_pub.asc"))
                self.assertEqual(r.status_code, 503)
                self.assertContains(r, 'lang="sk"', status_code=503)
                self.assertContains(r, SK_TRY_AGAIN, status_code=503)
                r = agent.post(f"/api/v1/contacts/{self.contact.pk}/keys", {"key": fixture("alice_pub.asc")},
                               format="json")
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.json()["error"], "key_check_unavailable")
                r = self.client.post(f"/contacts/{self.contact.pk}/keys/", {"key": fixture("alice_pub.asc")})
                self.assertEqual(r.status_code, 503)
                self.assertContains(r, "Could not check the key right now", status_code=503)
                self.assertEqual(EncryptionKey.objects.count(), 0)
                self.assertEqual(OutboxMessage.objects.count(), 0)
                self.assertFalse(AuditEntry.objects.filter(action="key.add").exists())
                self.assert_no_keyring_left()

    def test_activation_clears_earlier_failed_keys(self):
        # staff
        failed = self.failed_key()
        active = services.add_key(self.contact, fixture("alice_pub.asc"), source="staff", actor=self.staff)
        failed.refresh_from_db()
        self.assertEqual(failed.state, "replaced")
        services.revoke_key(active, self.staff)
        self.assertIsNone(delivery.key_for_contact(self.contact.pk))  # the stale failure blocks nothing

        # a client's confirmation
        failed = self.failed_key()
        self.upload(fixture("alice_pub.asc"))
        self.assertEqual(self.client.post(f"/k/{self.confirmation_token()}/").status_code, 200)
        failed.refresh_from_db()
        self.assertEqual(failed.state, "replaced")

        # an agent
        failed = self.failed_key()
        r = self.api().post(f"/api/v1/contacts/{self.contact.pk}/keys", {"key": fixture("alice_pub.asc")},
                            format="json")
        self.assertEqual(r.status_code, 201)
        failed.refresh_from_db()
        self.assertEqual(failed.state, "replaced")
        services.revoke_key(EncryptionKey.objects.get(pk=r.json()["id"]), self.staff)
        self.assertIsNone(delivery.key_for_contact(self.contact.pk))
        self.assertEqual(EncryptionKey.objects.filter(state="active").count(), 0)

    def test_revoking_a_failed_key_unblocks_plaintext(self):
        failed = self.failed_key()
        invoice = self.send()[0]
        with self.assertLogs("apps.notify.services", "ERROR"):
            self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get().last_error, "EncryptionRefused(key_failed)")

        self.sign_in_staff()
        page = f"/contacts/{self.contact.pk}/keys/"
        self.assertContains(self.client.get(page), f'action="/keys/{failed.pk}/revoke/"')
        r = self.client.post(f"/keys/{failed.pk}/revoke/")
        self.assertRedirects(r, page, fetch_redirect_response=False)
        failed.refresh_from_db()
        self.assertEqual(failed.state, "revoked")
        entry = AuditEntry.objects.get(action="key.revoke")
        self.assertEqual((entry.actor_username, entry.detail["was"]), ("clerk", "failed"))

        # The contact has no usable key now, so plaintext is what they asked for.
        notify.retry_now(OutboxMessage.objects.all())
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertIn(invoice.number, mail.outbox[0].subject)

    def test_confirmation_letter_cancelled_when_key_no_longer_pending(self):
        # Revoked before the letter went out.
        self.upload(fixture("alice_pub.asc"))
        services.revoke_key(EncryptionKey.objects.get(), self.staff)
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get().state, "cancelled")
        self.assertTrue(AuditEntry.objects.filter(action="outbox.cancelled").exists())

        # Queued again after its key was confirmed (it is active now, no longer pending).
        caches["ratelimit"].clear()
        self.upload(fixture("alice_pub.asc"))
        letter = OutboxMessage.objects.get(state="queued", kind="key_confirm")
        self.assertEqual(self.client.post(f"/k/{self.confirmation_token()}/").status_code, 200)
        mail.outbox.clear()
        OutboxMessage.objects.filter(pk=letter.pk).update(state="queued")
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(OutboxMessage.objects.get(pk=letter.pk).state, "cancelled")

        # Its link expired while it waited (a vault sealed for a week). The notice to the active key
        # goes with it: the uploaded key can never be confirmed now.
        self.upload(fixture("alice_pub.asc"))
        letter = OutboxMessage.objects.get(state="queued", kind="key_confirm")
        EncryptionKey.objects.filter(pk=letter.key_id).update(
            confirm_sent_at=timezone.now() - datetime.timedelta(days=8))
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(OutboxMessage.objects.get(pk=letter.pk).state, "cancelled")
        self.assertEqual(mail.outbox, [])

    def test_confirmation_letter_without_key_is_never_sent(self):
        self.upload(fixture("alice_pub.asc"))
        OutboxMessage.objects.update(key=None)
        for _ in range(2):
            with self.assertLogs("apps.notify.services", "ERROR"):
                self.assertEqual(notify.flush_outbox(), 0)
            self.assertEqual(mail.outbox, [])
            letter = OutboxMessage.objects.get()
            self.assertEqual((letter.state, letter.last_error), ("failed", "EncryptionRefused(key_missing)"))
            notify.retry_now(OutboxMessage.objects.all())
        from apps.core import jobs

        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        jobs.outbox_failed_alert()
        alert = mail.outbox[0].body
        self.assertIn("key confirmation letter goes to the key it confirms or nowhere", alert)
        self.assertNotIn("encryption key stopped working", alert)  # no client's key failed here
        self.assertNotIn(ALICE, alert)


class KeyPageLanguageTests(FlowBase):
    def assertClean(self, text, what):
        for token in LEAKS:
            self.assertNotIn(token, text, f"{what} leaks {token!r}")

    def test_reworded_translations(self):
        with translation.override("sk"):
            self.assertIn("bol zrušený", render_to_string("keys/expired.html", {"reason": "used"}))
        with translation.override("cs"):
            self.assertIn("zrušen", render_to_string("keys/expired.html", {"reason": "used"}))
        for lang in ("sk", "cs"):
            with translation.override(lang):
                text = render_to_string("keys/expired.html", {"reason": "used"})
            self.assertNotIn("stiahnut", text)
            self.assertNotIn("stažen", text)
        letter = OutboxMessage(kind="key_confirm", language="ko", subject_context={"org": "Seller"})
        with translation.override("ko"):
            self.assertEqual(notify._subject(letter), "Seller용 암호화 키를 확인하십시오")

    def test_key_pages_are_translated_and_leak_no_template_syntax(self):
        self.upload(fixture("alice_pub.asc"))
        token = self.confirmation_token()
        english = {}
        for lang in LANGS:
            caches["ratelimit"].clear()
            cache.clear()
            pages = {
                "upload": self.client.get(f"/i/{self.view}/key/?lang={lang}"),
                "refused": self.upload("not a key", lang=lang),
                "confirm": self.client.get(f"/k/{token}/?lang={lang}"),
                "unknown": self.client.get(f"/k/nope/?lang={lang}"),
            }
            with translation.override(lang):
                rendered = {
                    "sent": render_to_string("keys/upload.html", {"uploaded": True, "fingerprint": ALICE_SHOWN,
                                                                   "token": self.view}),
                    "confirmed": render_to_string("keys/confirmed.html", {"fingerprint": ALICE_SHOWN}),
                    "expired": render_to_string("keys/expired.html", {"reason": "expired"}),
                    "used": render_to_string("keys/expired.html", {"reason": "used"}),
                    "letter.txt": render_to_string("email/key_confirm.txt", {
                        "org": "Seller", "name": "Ján", "fingerprint": ALICE_SHOWN,
                        "confirm_url": "https://pay.example/k/t/"}),
                    "letter.html": render_to_string("email/key_confirm.html", {
                        "org": "Seller", "name": "Ján", "fingerprint": ALICE_SHOWN,
                        "confirm_url": "https://pay.example/k/t/", "language": lang}),
                }
            texts = {name: r.content.decode() for name, r in pages.items()} | rendered
            for name, r in pages.items():
                self.assertContains(r, f'lang="{lang}"', status_code=r.status_code)
            for name, text in texts.items():
                self.assertClean(text, f"{name}/{lang}")
                body = text.split("<main>")[1] if "<main>" in text else text
                if lang == "en":
                    english[name] = body
                else:
                    self.assertNotEqual(body, english[name], f"{name}/{lang} is the English text")
