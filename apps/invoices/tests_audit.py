"""Invoicing audit 2026-11-03, task 5: contacts and invoices (API-1, API-4, API-5, API-9, DATA-1, CSV).

Ported from the audit's PoCs (docs/superpowers/audit/2026-11-03-invoicing/poc in the
liberation.services repo): poc/api/poc_api_1.py, poc_api_4.py, poc_api_5.py, poc_api_5b.py,
poc_api_9.py, poc/data/poc_data_1.py and poc/poc_e2e_csv.py. Each asserts the secure behaviour, as
the PoC did; their prints are gone. Two PoCs had nothing to assert: poc_api_5b only measured how
long a PDF takes for 200 and 2000 lines, so here it asserts the cap that bounds that time; and
poc_e2e_csv only printed the CSV amount, so here it asserts the asset's own decimals.
"""
import datetime
import io
import itertools
import json
import logging
import hashlib
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import find_contact, find_or_create_contact, update_contact_and_copies
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice, LineItem
from apps.invoices.services import create_invoice, send_invoice, update_draft
from apps.keys.models import EncryptionKey
from apps.keys.tests_delivery import ALICE, fixture
from apps.orgs.models import FiatLink, Organization
from apps.orgs.testing import add_bank
from apps.payments.tests_e2e_btcpay import BTC_DUE, BTCPayBase
from apps.payments.tests_e2e_lightning import LightningBase
from apps.staff.tests_invoicing import InvoicingBase
from apps.vault.fields import blind_index
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import VaultSealed, vault

LINE = {"name": "Consulting", "quantity": "1", "unit_price_minor": 10000}
SERVICE_LINE = {**LINE, "quantity": 1}


def pdf_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def render(invoice, language="en") -> str:
    from apps.invoices.pdf import render_invoice_pdf

    return pdf_text(render_invoice_pdf(invoice, language))


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.a = Organization.objects.create(slug="org-a")
        self.b = Organization.objects.create(slug="org-b")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        for org in (self.a, self.b, self.us):
            add_bank(org)

    def client_for(self, org, scopes=("read", "invoices:write")):
        _, plain = ApiToken.issue(org, f"t-{org.slug}", list(scopes))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c


# ----------------------------------------------------------------------------------- API-1
class UsIssuerOwnContactTests(Base):
    """poc_api_1: the US-resident issuer has its own client (not a copy of anything)."""

    def setUp(self):
        super().setUp()
        self.victim, _ = find_or_create_contact(
            self.us, "victim@example.com", legal_name="Victoria Secretname",
            address={"line1": "77 Hidden Lane", "city": "Reno", "zip": "89501", "country": "US"},
            tax_ids="SSN 123-45-6789", phone="+1 775 555 0199", notes="VIP, internal note of the US issuer",
            us_tax_resident="yes")

    def test_org_a_cannot_read_the_us_issuers_contact_through_a_draft_pdf(self):
        a = self.client_for(self.a)
        # the attacker knows only the address; every other field it sends is its own
        r = a.post("/api/v1/invoices", {"contact": {"email": "victim@example.com", "legal_name": "?",
                                                    "us_tax_resident": "yes"},
                                        "currency": "EUR", "lines": [LINE]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        inv = Invoice.objects.get(pk=r.json()["id"])
        self.assertEqual((inv.org, inv.requested_by_org), (self.us, self.a))
        self.assertNotEqual(inv.contact_id, self.victim.pk)
        pdf = a.get(f"/api/v1/invoices/{inv.pk}/pdf")
        self.assertEqual(pdf.status_code, 200)
        text = pdf_text(pdf.content)
        leaked = [s for s in ("Victoria Secretname", "77 Hidden Lane", "SSN 123-45-6789") if s in text]
        self.assertEqual(leaked, [], "org A read the US issuer's contact record")

    def test_org_a_cannot_rewrite_the_us_issuers_contact(self):
        a = self.client_for(self.a)
        r = a.post("/api/v1/invoices", {"contact": {"email": "victim@example.com", "us_tax_resident": "yes"},
                                        "currency": "EUR", "lines": [LINE]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        mine = a.get("/api/v1/contacts?email=victim@example.com").json()["results"][0]["id"]
        p = a.patch(f"/api/v1/contacts/{mine}", {"legal_name": "Attacker Rewrote This",
                                                 "address": {"line1": "1 Attacker Rd"},
                                                 "tax_ids": "", "notes": "overwritten by org A",
                                                 "phone": "+1 000"}, format="json")
        self.assertEqual(p.status_code, 200)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.legal_name, "Victoria Secretname", "org A rewrote the US issuer's contact")
        self.assertEqual((self.victim.tax_ids, self.victim.notes, self.victim.phone, self.victim.address["line1"]),
                         ("SSN 123-45-6789", "VIP, internal note of the US issuer", "+1 775 555 0199",
                          "77 Hidden Lane"))
        # Org A's PATCH reached its own copy, and only that.
        copy = Invoice.objects.get(pk=r.json()["id"]).contact
        self.assertEqual((copy.legal_name, copy.copy_for_org_id), ("Attacker Rewrote This", self.a.pk))

    def test_org_a_cannot_lower_the_us_answer_of_the_us_issuers_contact(self):
        a = self.client_for(self.a)
        a.post("/api/v1/invoices", {"contact": {"email": "victim@example.com", "us_tax_resident": "yes"},
                                    "currency": "EUR", "lines": [LINE]}, format="json")
        mine = a.get("/api/v1/contacts?email=victim@example.com").json()["results"][0]["id"]
        self.assertEqual(a.patch(f"/api/v1/contacts/{mine}", {"us_tax_resident": "no"}, format="json").status_code,
                         200)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.us_tax_resident, "yes")

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_a_key_org_a_adds_never_reaches_the_us_issuers_contact(self):
        import tempfile
        from shutil import rmtree

        tmp = tempfile.mkdtemp()
        self.addCleanup(rmtree, tmp, ignore_errors=True)
        self.enterContext(override_settings(GPG_TMP_DIR=tmp))
        victim, _ = find_or_create_contact(self.us, ALICE, legal_name="Alice of the US issuer", us_tax_resident="yes")
        a = self.client_for(self.a)
        r = a.post("/api/v1/invoices", {"contact": {"email": ALICE, "us_tax_resident": "yes"},
                                        "currency": "EUR", "lines": [LINE]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        mine = a.get(f"/api/v1/contacts?email={ALICE}").json()["results"][0]["id"]
        k = a.post(f"/api/v1/contacts/{mine}/keys", {"key": fixture("alice_pub.asc")}, format="json")
        self.assertEqual(k.status_code, 201, k.content)
        self.assertFalse(EncryptionKey.objects.filter(contact=victim).exists(),
                         "org A's key became a key of the US issuer's own client")
        copy = Invoice.objects.get(pk=r.json()["id"]).contact
        self.assertEqual(copy.encryption_keys.get(state="active").source, "mirror")


class SharedCopyTests(Base):
    """poc_api_1: org B invoiced a US tax resident; org A later names the same address."""

    def test_org_a_cannot_read_what_org_b_stored_about_its_client(self):
        b = self.client_for(self.b)
        r = b.post("/api/v1/invoices", {"contact": {"email": "client@example.com", "legal_name": "Bob Privateman",
                                                    "tax_ids": "EIN 98-7654321",
                                                    "address": {"line1": "5 B Street", "city": "Austin"},
                                                    "us_tax_resident": "yes"},
                                        "currency": "EUR", "lines": [LINE]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        a = self.client_for(self.a)
        r = a.post("/api/v1/invoices", {"contact": {"email": "client@example.com", "us_tax_resident": "yes"},
                                        "currency": "EUR", "lines": [LINE]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        text = pdf_text(a.get(f"/api/v1/invoices/{r.json()['id']}/pdf").content)
        leaked = [s for s in ("Bob Privateman", "EIN 98-7654321", "5 B Street") if s in text]
        self.assertEqual(leaked, [], "org A read what org B stored about its client")

    def test_a_patch_reaches_only_the_copy_made_for_the_patching_org(self):
        b_contact, _ = find_or_create_contact(self.b, "client@example.com", legal_name="Bob Privateman",
                                              us_tax_resident="yes")
        a_contact, _ = find_or_create_contact(self.a, "client@example.com", legal_name="Bob of A",
                                              us_tax_resident="yes")
        b_copy = create_invoice(self.b, b_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        a_copy = create_invoice(self.a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        self.assertNotEqual(a_copy.pk, b_copy.pk)
        self.assertEqual({a_copy.org_id, b_copy.org_id}, {self.us.pk})
        self.assertEqual((a_copy.copy_for_org_id, b_copy.copy_for_org_id), (self.a.pk, self.b.pk))
        update_contact_and_copies(a_contact, {"legal_name": "Renamed by A"})
        a_copy.refresh_from_db()
        b_copy.refresh_from_db()
        self.assertEqual((a_copy.legal_name, b_copy.legal_name), ("Renamed by A", "Bob Privateman"))
        # Org A invoicing again reuses its own copy, never org B's.
        again = create_invoice(self.a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.assertEqual(again.contact_id, a_copy.pk)


class CopyLinkTests(Base):
    """The link between an original and the US issuer's copy is `copy_for_org`, nothing else."""

    def test_copies_of_and_sources_of_follow_only_the_link(self):
        from apps.keys.mirroring import copies_of, sources_of

        native_us, _ = find_or_create_contact(self.us, "x@example.com", us_tax_resident="yes")
        a_contact, _ = find_or_create_contact(self.a, "x@example.com", us_tax_resident="yes")
        b_contact, _ = find_or_create_contact(self.b, "x@example.com", us_tax_resident="yes")
        a_copy = create_invoice(self.a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        b_copy = create_invoice(self.b, b_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        # The US issuer invoicing its own client never touches a copy.
        own = create_invoice(self.us, native_us, currency="EUR", lines=[SERVICE_LINE])[0]
        self.assertEqual(own.contact_id, native_us.pk)
        self.assertEqual(copies_of(a_contact.pk), {a_copy.pk})
        self.assertEqual(copies_of(b_contact.pk), {b_copy.pk})
        self.assertEqual(copies_of(native_us.pk), set())
        self.assertEqual(sources_of(a_copy.pk), {a_contact.pk})
        self.assertEqual(sources_of(b_copy.pk), {b_contact.pk})
        self.assertEqual(sources_of(native_us.pk), set())

    def test_find_contact_tells_a_native_contact_from_a_copy(self):
        a_contact, _ = find_or_create_contact(self.a, "x@example.com", us_tax_resident="yes")
        a_copy = create_invoice(self.a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        self.assertIsNone(find_contact(self.us, "x@example.com"))  # the US issuer has no client of its own here
        self.assertEqual(find_contact(self.us, "x@example.com", copy_for=self.a), a_copy)
        self.assertIsNone(find_contact(self.us, "x@example.com", copy_for=self.b))
        native, created = find_or_create_contact(self.us, "x@example.com")
        self.assertTrue(created)
        self.assertNotEqual(native.pk, a_copy.pk)
        self.assertEqual(find_contact(self.us, "x@example.com"), native)

    def test_one_native_contact_and_one_copy_per_requester_for_an_address(self):
        a_contact, _ = find_or_create_contact(self.a, "x@example.com", us_tax_resident="yes")
        a_copy = create_invoice(self.a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0].contact
        with self.assertRaises(IntegrityError), transaction.atomic():
            twin = Contact.objects.create(org=self.us, copy_for_org=self.a)
            ContactEmail.objects.create(contact=twin, address="X@example.com")
        find_or_create_contact(self.us, "x@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContactEmail.objects.create(contact=Contact.objects.create(org=self.us), address="x@example.com")
        self.assertEqual(ContactEmail.objects.get(contact=a_copy).copy_for_org_id, self.a.pk)


# ----------------------------------------------------------------------------------- API-4
class SentInvoiceImmutabilityTests(UnsealedVaultMixin, TestCase):
    """poc_api_4: the PDF of a SENT invoice was rendered from today's contact row."""

    def setUp(self):
        super().setUp()
        self.a = Organization.objects.create(slug="org-a")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True, legal_name="US Issuer Ltd")
        for org in (self.a, self.us):
            add_bank(org)
        _, plain = ApiToken.issue(self.a, "a", ["read", "invoices:write"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")

    def test_patching_a_contact_does_not_change_the_us_issuers_sent_invoice(self):
        # The US issuer's own client and its own sent invoice (not org A's).
        victim, _ = find_or_create_contact(self.us, "victim@example.com", legal_name="Victoria Realname",
                                           address={"line1": "77 Hidden Lane", "city": "Reno"},
                                           us_tax_resident="yes")
        theirs, _ = create_invoice(self.us, victim, currency="EUR", lines=[{**LINE, "quantity": 1}])
        theirs, view_plain, _ = send_invoice(theirs, email_client=False)
        before = render(theirs)
        # Org A: one draft naming the address, then a PATCH of its own contact.
        self.c.post("/api/v1/invoices", {"contact": {"email": "victim@example.com", "us_tax_resident": "yes"},
                                         "currency": "EUR", "lines": [LINE]}, format="json")
        mine = self.c.get("/api/v1/contacts?email=victim@example.com").json()["results"][0]["id"]
        self.c.patch(f"/api/v1/contacts/{mine}", {"legal_name": "Mallory Changed-It",
                                                  "address": {"line1": "1 Attacker Rd"}}, format="json")
        theirs.refresh_from_db()
        after = render(theirs)
        self.assertEqual(before, after, "a sent invoice's document changed after it was sent")

    def test_patching_a_contact_does_not_change_its_own_sent_invoice(self):
        r = self.c.post("/api/v1/invoices", {"contact": {"email": "c@example.com", "legal_name": "Carol Original"},
                                             "currency": "EUR", "lines": [LINE]}, format="json")
        iid, cid = r.json()["id"], r.json()["contact_id"]
        self.assertEqual(self.c.post(f"/api/v1/invoices/{iid}/send", {"email_client": False},
                                     format="json").status_code, 200)
        before = pdf_text(self.c.get(f"/api/v1/invoices/{iid}/pdf").content)
        self.c.patch(f"/api/v1/contacts/{cid}", {"legal_name": "Someone Else"}, format="json")
        after = pdf_text(self.c.get(f"/api/v1/invoices/{iid}/pdf").content)
        self.assertIn("Carol Original", before)
        self.assertEqual(before, after, "a sent invoice's document changed after it was sent")


class SnapshotTests(UnsealedVaultMixin, TestCase):
    """The seller, the buyer and the payment details of a sent invoice are frozen at send."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1, Bratislava",
                                               tax_ids="IČO 12345678", footer_note="Registered in Bratislava")
        self.bank = add_bank(self.org, beneficiary_name="Seller s.r.o.", account_number="SK3112000000198742637541",
                             bank_name="First Bank", bank_swift="FIRSSKBA",
                             instructions={"en": "Quote the reference.", "sk": "Uveďte referenciu."})
        self.link = FiatLink.objects.create(org=self.org, kind="wise", label="Wise", currencies=["EUR"],
                                            url="https://wise.example/pay/seller",
                                            instructions={"en": "Pay by card."})
        self.contact, _ = find_or_create_contact(self.org, "c@example.com", legal_name="Carol Original",
                                                 address={"line1": "1 Old Street", "city": "Brno"},
                                                 tax_ids="DIČ 111")
        draft = create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.invoice, self.view, self.pay = send_invoice(draft, email_client=False)

    def change_everything(self):
        self.contact.legal_name, self.contact.tax_ids = "Mallory Changed-It", "DIČ 999"
        self.contact.address = {"line1": "1 Attacker Rd", "city": "Reno"}
        self.contact.save()
        self.org.legal_name, self.org.address, self.org.tax_ids = "Other Seller", "Elsewhere 9", "IČO 99999999"
        self.org.footer_note = "Changed footer"
        self.org.save()
        self.bank.account_number, self.bank.bank_swift = "GB00ATTACKER0000000000", "ATTKGB22"
        self.bank.beneficiary_name, self.bank.instructions = "Mallory", {"en": "Pay Mallory."}
        self.bank.save()
        self.link.url, self.link.instructions = "https://evil.example/pay", {"en": "Pay here instead."}
        self.link.save()

    def test_bank_contact_and_organization_edits_do_not_change_a_sent_invoice(self):
        from apps.invoices.pdf import render_receipt_pdf

        before_pdf = render(self.invoice)
        before_receipt = pdf_text(render_receipt_pdf(self.invoice, "en"))
        before_page = self.client.get(f"/p/{self.pay}/?lang=en").content.decode()
        self.assertIn("SK3112000000198742637541", before_pdf)
        self.assertIn("SK3112000000198742637541", before_page)
        self.change_everything()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertEqual(render(invoice), before_pdf)
        self.assertEqual(pdf_text(render_receipt_pdf(invoice, "en")), before_receipt)
        page = self.client.get(f"/p/{self.pay}/?lang=en").content.decode()
        view = self.client.get(f"/i/{self.view}/?lang=en").content.decode()
        for text in (page, view):
            for changed in ("Mallory", "1 Attacker Rd", "Other Seller", "Elsewhere 9", "IČO 99999999",
                            "GB00ATTACKER0000000000", "evil.example", "Pay here instead."):
                self.assertNotIn(changed, text)
            self.assertIn("Carol Original", text)
            self.assertIn("Seller s.r.o.", text)
        self.assertIn("SK3112000000198742637541", page)
        self.assertIn("https://wise.example/pay/seller", page)

    def test_the_snapshot_keeps_every_language_of_the_instructions(self):
        self.change_everything()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.assertIn("Uveďte referenciu.", render(invoice, "sk"))
        self.assertIn("Quote the reference.", render(invoice, "en"))

    def test_a_draft_renders_the_live_rows(self):
        draft = create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.change_everything()
        draft = Invoice.objects.get(pk=draft.pk)
        self.assertIsNone(draft.snapshot)
        text = render(draft)
        self.assertIn("Mallory Changed-It", text)
        self.assertIn("GB00ATTACKER0000000000", text)

    def test_the_snapshot_is_encrypted_at_rest(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT snapshot FROM invoices_invoice WHERE id = %s", [self.invoice.pk])
            (raw,) = cursor.fetchone()
        self.assertTrue(raw.startswith("gAAAA"), raw[:20])
        self.assertNotIn("Carol", raw)
        self.assertNotIn("SK3112", raw)

    def test_the_snapshot_of_a_sent_invoice_cannot_be_rewritten(self):
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.snapshot = {**invoice.snapshot, "buyer": {"name": "Mallory", "address_lines": [], "tax_ids": ""}}
        with self.assertRaisesMessage(InvoiceError, "sent invoices cannot be edited"):
            invoice.save(update_fields=["snapshot"])

    def test_a_sent_invoice_without_a_snapshot_is_frozen_the_first_time_it_is_shown(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(snapshot=None)  # as left by a migration that ran sealed
        first = render(Invoice.objects.get(pk=self.invoice.pk))
        self.assertIsNotNone(Invoice.objects.get(pk=self.invoice.pk).snapshot)
        self.change_everything()
        self.assertEqual(render(Invoice.objects.get(pk=self.invoice.pk)), first)


class SealedSnapshotTests(UnsealedVaultMixin, TestCase):
    """Review Focus 3: a sealed vault on the new paths is "temporarily unavailable", never a 500."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        contact, _ = find_or_create_contact(self.org, "c@example.com", legal_name="Carol")
        draft = create_invoice(self.org, contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.invoice, self.view, self.pay = send_invoice(draft, email_client=False)
        _, plain = ApiToken.issue(self.org, "a", ["read", "invoices:write"])
        self.api = APIClient(raise_request_exception=False)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        self.client.raise_request_exception = False

    def check_unavailable(self):
        for path in (f"/i/{self.view}/", f"/i/{self.view}/pdf", f"/p/{self.pay}/"):
            r = self.client.get(path + "?lang=en")
            self.assertEqual(r.status_code, 503, path)
            self.assertIn("Temporarily unavailable", r.content.decode(), path)
        r = self.api.get(f"/api/v1/invoices/{self.invoice.pk}/pdf")
        self.assertEqual((r.status_code, r.json()["error"]), (503, "vault_sealed"))
        r = self.api.post("/api/v1/invoices", {"contact": {"email": "d@example.com"}, "currency": "EUR",
                                               "lines": [LINE]}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (503, "vault_sealed"))

    def test_sealed_vault_is_temporarily_unavailable(self):
        vault.seal()
        self.check_unavailable()

    def test_sealed_after_the_gate_let_the_request_through(self):
        """The vault sealed between the gate and the read: the same answer, never a 500."""
        vault.seal()
        gate = mock.Mock(is_unsealed=mock.Mock(return_value=True))
        with mock.patch("apps.vault.middleware.vault", gate):
            self.check_unavailable()

    def test_the_keyed_fingerprint_needs_the_vault_and_says_so_with_a_503(self):
        from apps.api import views as api_views

        bare = Organization.objects.create(slug="bare")  # nothing encrypted: the token itself loads sealed
        add_bank(bare)
        _, plain = ApiToken.issue(bare, "b", ["read", "invoices:write"])
        api = APIClient(raise_request_exception=False)
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        vault.seal()
        gate = mock.Mock(is_unsealed=mock.Mock(return_value=True))
        with mock.patch("apps.vault.middleware.vault", gate), \
                mock.patch.object(api_views, "request_fingerprint", wraps=api_views.request_fingerprint) as fingerprint, \
                mock.patch.object(api_views, "find_or_create_contact") as contact_lookup:
            r = api.post("/api/v1/invoices", {"contact": {"email": "d@example.com"}, "currency": "EUR",
                                              "lines": [LINE]}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (503, "vault_sealed"))
        self.assertEqual(fingerprint.call_count, 1)  # it was the fingerprint that needed the vault,
        self.assertEqual(contact_lookup.call_count, 0)  # before anything else did
        self.assertEqual(Invoice.objects.filter(org=bare).count(), 0)

    def test_reading_the_snapshot_while_sealed_raises_vault_sealed(self):
        from apps.invoices import snapshot

        plain = Invoice.objects.only("pk", "status").get(pk=self.invoice.pk)
        vault.seal()
        with self.assertRaises(VaultSealed):
            snapshot.for_render(plain)


# ----------------------------------------------------------------------------------- API-5
class LineCapTests(UnsealedVaultMixin, TestCase):
    """poc_api_5: one request within Apache's 2 MiB made a 20 000-line draft."""

    N = 20000

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        add_bank(self.org)
        _, plain = ApiToken.issue(self.org, "w", ["read", "invoices:write"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")

    def post(self, **over):
        body = {"contact": {"email": "x@example.com"}, "currency": "EUR",
                "lines": [{"name": "a", "unit_price_minor": 1}], **over}
        return self.c.post("/api/v1/invoices", json.dumps(body, separators=(",", ":")),
                           content_type="application/json")

    def test_line_count_is_capped(self):
        r = self.post(lines=[{"name": "a", "unit_price_minor": 1}] * self.N)
        self.assertNotEqual(r.status_code, 201, "an invoice with 20 000 lines was accepted")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(LineItem.objects.count(), 0)

    def test_two_hundred_lines_and_no_more(self):
        self.assertEqual(self.post(lines=[{"name": "a", "unit_price_minor": 1}] * 201).status_code, 400)
        r = self.post(lines=[{"name": "a", "unit_price_minor": 1}] * 200, external_reference="ok")
        self.assertEqual(r.status_code, 201, r.content[:300])
        self.assertEqual(LineItem.objects.count(), 200)

    def test_text_lengths_are_capped(self):
        long_line = {"name": "a", "unit_price_minor": 1, "description": "d" * 2001}
        for over in ({"title": "t" * 2001}, {"notes": "n" * 5001}, {"lines": [long_line]}):
            r = self.post(**over)
            self.assertEqual((r.status_code, r.json()["error"]), (400, "validation_error"), over.keys())
        at_cap = {"name": "a", "unit_price_minor": 1, "description": "d" * 2000}
        r = self.post(title="t" * 2000, notes="n" * 5000, lines=[at_cap], external_reference="cap")
        self.assertEqual(r.status_code, 201, r.content[:300])

    def test_the_schema_states_the_caps(self):
        from django.core.management import call_command

        out = io.StringIO()
        call_command("spectacular", "--format", "openapi-json", stdout=out)
        schema = json.loads(out.getvalue())["components"]["schemas"]
        create = schema["InvoiceCreateRequest"]["properties"]
        self.assertEqual(create["lines"]["maxItems"], 200)
        self.assertEqual((create["title"]["maxLength"], create["notes"]["maxLength"]), (2000, 5000))
        self.assertEqual(schema["LineInRequest"]["properties"]["description"]["maxLength"], 2000)


class ServiceCapTests(UnsealedVaultMixin, TestCase):
    """The same caps in `create_invoice` and `update_draft`, which the staff form goes through."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "x@example.com")

    def test_pdf_size_is_bounded_by_the_line_cap(self):
        """poc_api_5b measured PDF time growing with the line count (1.4 s for 200 lines, 14.8 s for
        2000). The 2000-line invoice is refused now; 200 lines, the most there can be, still render."""
        from apps.invoices.pdf import render_invoice_pdf

        with self.assertRaisesMessage(InvoiceError, "at most 200 lines"):
            create_invoice(self.org, self.contact, currency="EUR", lines=[{"name": "a", "unit_price_minor": 1}] * 2000,
                           external_reference="r2000")
        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                lines=[{"name": "a", "unit_price_minor": 1}] * 200, external_reference="r200")
        self.assertTrue(render_invoice_pdf(inv, "en").startswith(b"%PDF"))
        self.assertFalse(Invoice.objects.filter(external_reference="r2000").exists())

    def test_create_invoice_refuses_what_the_serializer_refuses(self):
        ok = {"name": "a", "unit_price_minor": 1}
        cases = [
            ({"lines": [ok] * 201}, "at most 200 lines"),
            ({"lines": [ok], "title": "t" * 2001}, "title"),
            ({"lines": [ok], "notes": "n" * 5001}, "notes"),
            ({"lines": [{**ok, "description": "d" * 2001}]}, "description"),
            ({"lines": [{**ok, "name": "n" * 501}]}, "name"),
        ]
        for kw, message in cases:
            with self.subTest(message), self.assertRaisesMessage(InvoiceError, message):
                create_invoice(self.org, self.contact, currency="EUR", **kw)
        self.assertFalse(Invoice.objects.exists())

    def test_update_draft_refuses_them_too(self):
        draft = create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0]
        base = dict(kind="invoice", currency="EUR", issue_date=datetime.date.today(), due_days=None, title="",
                    notes="", lines=[SERVICE_LINE])
        for over, message in (({"lines": [SERVICE_LINE] * 201}, "at most 200 lines"),
                              ({"title": "t" * 2001}, "title"), ({"notes": "n" * 5001}, "notes"),
                              ({"lines": [{**SERVICE_LINE, "description": "d" * 2001}]}, "description")):
            with self.subTest(message), self.assertRaisesMessage(InvoiceError, message):
                update_draft(draft, **{**base, **over})
        self.assertEqual(draft.lines.count(), 1)


class StaffFormCapTests(InvoicingBase):
    """The staff form states the same caps (its own line cap is lower: see `limits.STAFF_FORM_LINES`)."""

    def test_title_up_to_the_cap_is_accepted_and_beyond_refused(self):
        r = self.client.post("/invoices/new/", self.payload(title="t" * 2000))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertEqual(len(self.newest().title), 2000)
        r = self.client.post("/invoices/new/", self.payload(title="t" * 2001))
        self.assertEqual(r.status_code, 400)

    def test_notes_and_description_beyond_the_cap_are_refused(self):
        for over in ({"notes": "n" * 5001}, {"lines-0-description": "d" * 2001}):
            r = self.client.post("/invoices/new/", self.payload(**over))
            self.assertEqual(r.status_code, 400, over.keys())

    def test_more_lines_than_the_form_holds_are_refused(self):
        from apps.invoices import limits

        data = self.payload(**{"lines-TOTAL_FORMS": str(limits.STAFF_FORM_LINES + 1)})
        for i in range(limits.STAFF_FORM_LINES + 1):
            data.update({f"lines-{i}-name": f"L{i}", f"lines-{i}-description": "", f"lines-{i}-quantity": "1",
                         f"lines-{i}-unit_price": "1", f"lines-{i}-tax_percent": "", f"lines-{i}-discount_percent": ""})
        r = self.client.post("/invoices/new/", data)
        self.assertEqual(r.status_code, 400)
        self.assertLessEqual(limits.STAFF_FORM_LINES, limits.MAX_LINES)


# ----------------------------------------------------------------------------------- API-9
class DateBoundTests(UnsealedVaultMixin, TestCase):
    """poc_api_9: issue_date was unbounded; a far-future date plus due_days was an unhandled 500."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        add_bank(self.org)
        _, plain = ApiToken.issue(self.org, "w", ["read", "invoices:write"])
        self.c = APIClient(raise_request_exception=False)
        self.c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        self.body = {"contact": {"email": "x@example.com"}, "currency": "EUR",
                     "lines": [{"name": "a", "unit_price_minor": 100}], "due_days": 30}

    def test_far_dates_are_a_400(self):
        body = {**self.body, "issue_date": "9999-12-31"}
        r = self.c.post("/api/v1/invoices", body, format="json")
        r2 = self.c.post("/api/v1/invoices", {**body, "issue_date": "0001-01-01", "due_days": None,
                                              "external_reference": "old"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r.json()["error"], "validation_error")

    def test_the_bounds(self):
        today = timezone.localdate()
        for day, code in ((datetime.date(1999, 12, 31), 400), (datetime.date(2000, 1, 1), 201),
                          (today + datetime.timedelta(days=365), 201), (today + datetime.timedelta(days=366), 400)):
            r = self.c.post("/api/v1/invoices", {**self.body, "issue_date": day.isoformat(),
                                                 "external_reference": day.isoformat()}, format="json")
            self.assertEqual(r.status_code, code, (day, r.content[:200]))

    def test_the_service_holds_the_same_bounds_and_never_overflows(self):
        contact, _ = find_or_create_contact(self.org, "x@example.com")
        for day in (datetime.date(1, 1, 1), datetime.date(9999, 12, 31)):
            with self.subTest(day), self.assertRaisesMessage(InvoiceError, "issue date"):
                create_invoice(self.org, contact, currency="EUR", lines=[SERVICE_LINE], issue_date=day)
        with self.assertRaisesMessage(InvoiceError, "due date"):
            create_invoice(self.org, contact, currency="EUR", lines=[SERVICE_LINE], due_days=10**9)
        draft = create_invoice(self.org, contact, currency="EUR", lines=[SERVICE_LINE])[0]
        with self.assertRaisesMessage(InvoiceError, "issue date"):
            update_draft(draft, kind="invoice", currency="EUR", issue_date=datetime.date(9999, 12, 31), due_days=30,
                         title="", notes="", lines=[SERVICE_LINE])
        with self.assertRaisesMessage(InvoiceError, "due date"):
            update_draft(draft, kind="invoice", currency="EUR", issue_date=datetime.date.today(), due_days=10**9,
                         title="", notes="", lines=[SERVICE_LINE])

    def test_an_overflow_anywhere_in_the_api_is_a_400(self):
        with mock.patch("apps.invoices.services.create_invoice", side_effect=OverflowError("date value out of range")):
            r = self.c.post("/api/v1/invoices", self.body, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid_request"))


# ----------------------------------------------------------------------------------- DATA-1
CONTACT_FIELDS = ("legal_name", "display_name", "address", "tax_ids", "phone", "notes", "preferred_language",
                  "us_tax_resident")

# What an attacker would try: a leaked customer list, and the seller's public product names.
CANDIDATE_PEOPLE = [
    ("jan.novak@example.com", "Jan Novak"),
    ("maria.svobodova@example.com", "Maria Svobodova"),
    ("peter.kral@example.org", "Peter Kral"),
    ("john.doe@example.net", "John Doe"),
]
CATALOGUE = ["TBC Bank Account Opening - Georgia", "Aiyl Bank Account Opening - Kyrgyzstan",
             "ABA Bank Account Opening - Cambodia", "Paraguay residency"]


def guess_fingerprint(email, name, inv, lines, *, issue_given, due_given, methods_given):
    """The canonical form apps.api.views.request_fingerprint hashed, rebuilt from plaintext."""
    contact = {"email": email.strip().lower(), **{f: None for f in CONTACT_FIELDS}}
    contact["legal_name"] = name
    canonical = {
        "contact": contact, "currency": inv["currency"].upper(), "kind": inv["kind"],
        "lines": [[ln["name"], "", str(Decimal(ln["quantity"]).quantize(Decimal("0.01"))), ln["unit_price_minor"],
                   ln["tax_rate_bps"], ln["discount_rate_bps"], ln["discount_amount_minor"]] for ln in lines],
        "due_days": ((inv["due_date"] - inv["issue_date"]).days if inv["due_date"] else None) if due_given else None,
        "issue_date": str(inv["issue_date"]) if issue_given else None,
        "offered_methods": list(inv["offered_methods"]) if methods_given else None,
        "title": "", "notes": "", "external_reference": inv["external_reference"],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class RequestFingerprintOracleTest(UnsealedVaultMixin, TestCase):
    """poc_data_1: Invoice.request_fingerprint was an unkeyed SHA-256 over the client's identity."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        add_bank(self.org)
        _token, plain = ApiToken.issue(self.org, "shop", ["read", "invoices:write"])
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")

    def create(self, **over):
        return self.api.post("/api/v1/invoices", {
            "contact": {"email": "Maria.Svobodova@example.com", "legal_name": "Maria Svobodova"},
            "currency": "EUR", "external_reference": "order-1042",
            "lines": [{"name": "Aiyl Bank Account Opening - Kyrgyzstan", "unit_price_minor": 133700}], **over,
        }, format="json")

    def test_database_alone_must_not_confirm_who_the_client_is(self):
        # liberation.services, the first consumer, creates the invoice for a customer's order.
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)

        # From here on the attacker has the database only: no vault key.
        vault.seal()
        self.assertFalse(vault.is_unsealed())
        inv = Invoice.objects.values("pk", "currency", "kind", "issue_date", "due_date", "offered_methods",
                                     "external_reference", "request_fingerprint", "contact_id").get()
        lines = list(LineItem.objects.filter(invoice_id=inv["pk"]).order_by("position")
                     .values("quantity", "unit_price_minor", "tax_rate_bps", "discount_rate_bps",
                             "discount_amount_minor"))
        recovered = None
        for (email, name), names in itertools.product(CANDIDATE_PEOPLE,
                                                      itertools.product(CATALOGUE, repeat=len(lines))):
            for issue_given, due_given, methods_given in itertools.product((False, True), repeat=3):
                guessed = [{**ln, "name": n} for ln, n in zip(lines, names)]
                stored = inv["request_fingerprint"]
                if guess_fingerprint(email, name, inv, guessed, issue_given=issue_given, due_given=due_given,
                                     methods_given=methods_given) in (stored, stored.removeprefix("v2:")):
                    recovered = (email, name, names)
        self.assertIsNone(recovered, "an unkeyed hash in a plaintext column confirms the client's identity")

    def test_the_fingerprint_is_versioned_keyed_and_still_catches_a_changed_replay(self):
        self.assertEqual(self.create().status_code, 201)
        stored = Invoice.objects.get().request_fingerprint
        self.assertRegex(stored, r"^v2:[0-9a-f]{64}$")
        self.assertEqual(self.create().status_code, 200)  # the same request again: the same invoice
        r = self.create(currency="CZK")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "idempotency_mismatch"))
        # Another vault (key) gives another fingerprint for the same request.
        from apps.api.views import request_fingerprint
        from apps.api import serializers as s

        ser = s.InvoiceCreateSerializer(data={"contact": {"email": "a@example.com"}, "currency": "EUR",
                                              "lines": [{"name": "x", "unit_price_minor": 1}]})
        ser.is_valid(raise_exception=True)
        first = request_fingerprint(ser.validated_data)
        with mock.patch.object(vault, "derive", return_value=b"\x01" * 32):
            self.assertNotEqual(request_fingerprint(ser.validated_data), first)


# ----------------------------------------------------------------------------------- CSV
class CsvGatewayAmount(BTCPayBase):
    """poc_e2e_csv: a BTCPay amount printed with 18 decimals (the column's), not BTC's 8."""

    def test_csv_amount(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE, status="Settled")
        self.poll()
        (row,) = [r for r in self.csv_rows() if r["rail"] == "btcpay"]
        self.assertEqual(row["amount"], "0.00166667")
        self.assertEqual(row["amount_invoice_currency"], "100.00")


class CsvLnAmount(LightningBase):
    """poc_e2e_csv: a Lightning amount (msat) printed with 18 decimals, not as BTC with 8."""

    def test_csv_amount(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id)
        self.poll()
        (row,) = [r for r in self.csv_rows() if r["rail"] == "lightning"]
        self.assertEqual(row["amount"], "0.00166667")


class CsvGatewayDecimalsTests(SimpleTestCase):
    def amount(self, rail, network, asset, received):
        from apps.exports.csv import _native_amount

        return _native_amount(SimpleNamespace(rail=rail, network=network, asset=asset, amount_base_units="",
                                              received_amount=Decimal(received), invoice_id=None))

    def test_each_gateway_asset_has_its_own_decimals(self):
        self.assertEqual(self.amount("btcpay", "monero", "XMR", "0.123456789012000000"), "0.123456789012")
        self.assertEqual(self.amount("btcpay", "zcash", "ZEC", "1.234567890000000000"), "1.23456789")
        self.assertEqual(self.amount("btcpay", "bitcoin", "BTC", "0.500000000000000000"), "0.50")
        # Lightning reports millisatoshis; sub-satoshi dust is not counted (nor refunded), so not shown.
        self.assertEqual(self.amount("lightning", "lightning", "BTC", "0.000001669990000000"), "0.00000166")


# ----------------------------------------------------------------------------------- migrations
CONTACTS_BEFORE = ("contacts", "0001_initial")
INVOICES_BEFORE = ("invoices", "0005_unique_token_hashes")


class MigrationBase(UnsealedVaultMixin, TransactionTestCase):
    """Review Focus 2: each change migrates a database that already holds the rows it touches."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)

    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


class ContactCopyMigrationTests(MigrationBase):
    """API-1: existing copies cannot be told from the US issuer's own clients. They stay native
    (`copy_for_org` NULL), are counted in the log, and no requesting organization reaches them again."""

    def test_existing_contacts_become_native_and_requesters_get_their_own_copy(self):
        a = Organization.objects.create(slug="org-a")
        us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        other = Organization.objects.create(slug="other")
        for org in (a, us, other):
            add_bank(org)
        old = self.migrate_to(CONTACTS_BEFORE)
        OldContact, OldEmail = old.get_model("contacts", "Contact"), old.get_model("contacts", "ContactEmail")

        def legacy(org, address, **fields):
            contact = OldContact.objects.create(org_id=org.pk, **fields)
            OldEmail.objects.create(contact_id=contact.pk, org_id=org.pk, address=address,
                                    address_index=blind_index(address), primary=True)
            return contact.pk

        shared = legacy(us, "victim@example.com", legal_name="Victoria Legacy", us_tax_resident="yes")  # a copy, or not
        own = legacy(us, "own@example.com", legal_name="Only The US Issuer's", us_tax_resident="yes")
        a_pk = legacy(a, "victim@example.com", legal_name="Victoria of A", us_tax_resident="yes")
        legacy(other, "victim@example.com", legal_name="Not counted")

        with self.assertLogs("apps.contacts.migrations", logging.WARNING) as logs:
            self.migrate_to_latest()

        self.assertEqual(len(logs.records), 1)
        self.assertIn("2 contact(s)", logs.output[0])
        for secret in ("victim@", "own@", "Victoria", "Only The"):
            self.assertNotIn(secret, logs.output[0])
        self.assertFalse(Contact.objects.filter(copy_for_org__isnull=False).exists())
        self.assertFalse(ContactEmail.objects.filter(copy_for_org__isnull=False).exists())
        self.assertEqual(find_contact(us, "victim@example.com").pk, shared)
        self.assertEqual(find_contact(us, "own@example.com").pk, own)

        # The old one-address-per-organization rule still holds for native contacts.
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContactEmail.objects.create(contact=Contact.objects.create(org=us), address="victim@example.com")

        # Org A's next invoice makes org A a copy of its own; the legacy row is not reached.
        a_contact = Contact.objects.get(pk=a_pk)
        invoice = create_invoice(a, a_contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.assertEqual(invoice.org, us)
        self.assertNotIn(invoice.contact_id, (shared, own))
        self.assertEqual(invoice.contact.copy_for_org_id, a.pk)
        update_contact_and_copies(a_contact, {"legal_name": "Renamed by A"})
        self.assertEqual(Contact.objects.get(pk=shared).legal_name, "Victoria Legacy")
        self.assertEqual(Contact.objects.get(pk=invoice.contact_id).legal_name, "Renamed by A")


class InvoiceSnapshotMigrationTests(MigrationBase):
    """API-4: invoices already sent are frozen from their current rows when the snapshot arrives."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.bank = add_bank(self.org, account_number="SK3112000000198742637541")
        self.contact, _ = find_or_create_contact(self.org, "c@example.com", legal_name="Carol Original",
                                                 address={"line1": "1 Old Street"})
        draft = create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.sent = send_invoice(draft, email_client=False)[0]
        voided = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0],
                              email_client=False)[0]
        from apps.invoices.services import void_invoice

        self.voided = void_invoice(voided, "mistake", None)
        self.draft = create_invoice(self.org, self.contact, currency="EUR", lines=[SERVICE_LINE])[0]
        self.before = render(Invoice.objects.get(pk=self.sent.pk))

    def change_rows(self):
        self.contact.legal_name = "Mallory Changed-It"
        self.contact.save()
        self.bank.account_number = "GB00ATTACKER0000000000"
        self.bank.save()

    def test_sent_invoices_are_backfilled_from_their_current_rows(self):
        from apps.invoices import snapshot

        self.migrate_to(INVOICES_BEFORE)  # the column did not exist: every sent invoice is "legacy"
        self.migrate_to_latest()
        sent = Invoice.objects.get(pk=self.sent.pk)
        self.assertEqual(sent.snapshot, snapshot.build(sent))  # the migration's copy builds the same thing
        self.assertIsNotNone(Invoice.objects.get(pk=self.voided.pk).snapshot)
        self.assertIsNone(Invoice.objects.get(pk=self.draft.pk).snapshot)
        self.assertEqual(render(sent), self.before)
        self.change_rows()
        self.assertEqual(render(Invoice.objects.get(pk=self.sent.pk)), self.before)
        self.assertIn("Mallory Changed-It", render(Invoice.objects.get(pk=self.draft.pk)))

    def test_a_migration_run_sealed_defers_the_snapshot_to_the_first_rendering(self):
        self.migrate_to(INVOICES_BEFORE)
        vault.seal()
        with self.assertLogs("apps.invoices.migrations", logging.WARNING) as logs:
            self.migrate_to_latest()
        self.assertIn("2 sent invoice(s)", logs.output[0])
        self.assertNotIn("Carol", logs.output[0])
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertFalse(Invoice.objects.filter(snapshot__isnull=False).exists())
        self.assertEqual(render(Invoice.objects.get(pk=self.sent.pk)), self.before)  # frozen now
        self.change_rows()
        self.assertEqual(render(Invoice.objects.get(pk=self.sent.pk)), self.before)


class FingerprintMigrationTests(MigrationBase):
    """DATA-1: the unkeyed fingerprints already stored are blanked."""

    def test_legacy_fingerprints_are_blanked(self):
        org = Organization.objects.create(slug="eu")
        add_bank(org)
        contact, _ = find_or_create_contact(org, "c@example.com")
        inv = create_invoice(org, contact, currency="EUR", lines=[SERVICE_LINE])[0]
        sent = send_invoice(create_invoice(org, contact, currency="EUR", lines=[SERVICE_LINE])[0],
                            email_client=False)[0]
        untouched = create_invoice(org, contact, currency="EUR", lines=[SERVICE_LINE])[0]
        old = self.migrate_to(INVOICES_BEFORE)
        OldInvoice = old.get_model("invoices", "Invoice")
        OldInvoice.objects.filter(pk__in=[inv.pk, sent.pk]).update(request_fingerprint="ab" * 32)
        self.migrate_to_latest()
        self.assertEqual(set(Invoice.objects.values_list("request_fingerprint", flat=True)), {""})
        self.assertEqual(Invoice.objects.get(pk=sent.pk).status, "sent")
        self.assertEqual(Invoice.objects.get(pk=untouched.pk).request_fingerprint, "")
        field = Invoice._meta.get_field("request_fingerprint")
        self.assertGreaterEqual(field.max_length, len("v2:") + 64)
