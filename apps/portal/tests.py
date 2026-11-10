import re

from django.core.cache import cache, caches
from django.test import TestCase, override_settings

from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.reference import is_valid, payment_reference
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.payments.models import Declaration
from apps.vault.testing import UnsealedVaultMixin


class PortalBase(UnsealedVaultMixin, TestCase):
    language = ""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.contact, _ = find_or_create_contact(
            self.org, "a@example.com", legal_name="Ján Žilinský", preferred_language=self.language
        )
        self.bank = BankAccount.objects.create(
            org=self.org, currency="EUR", label="Main EUR", beneficiary_name="Seller s.r.o.",
            account_number="SK0000000000001", bank_name="Bank A", bank_swift="BANKSKBX",
            intermediary_bank_name="Inter Bank", intermediary_swift="INTERXXX9", intermediary_account="C-1",
        )
        self.link = FiatLink.objects.create(
            org=self.org, kind="revolut", label="Revolut", currencies=["EUR"], url="https://revolut.me/seller",
            instructions={"en": "Pay with the app", "sk": "Zaplaťte v aplikácii"},
        )
        inv, _ = create_invoice(
            self.org, self.contact, currency="EUR",
            offered_methods=[f"bank:{self.bank.pk}", f"fiat:{self.link.pk}"],
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 133700}],
        )
        self.invoice, self.view, self.pay = send_invoice(inv)


class ReferenceTests(TestCase):
    def test_reference_is_valid_iso11649(self):
        inv = Invoice(kind="invoice", number="2026-0001")
        ref = payment_reference(inv)
        self.assertTrue(is_valid(ref))
        self.assertTrue(ref.startswith("RF"))
        bad = ref[:-1] + ("2" if ref[-1] != "2" else "3")
        self.assertFalse(is_valid(bad))


class PortalTests(PortalBase):
    language = "sk"

    def test_unknown_token_404(self):
        self.assertEqual(self.client.get("/i/nope/").status_code, 404)
        self.assertEqual(self.client.get("/p/nope/").status_code, 404)
        self.assertEqual(self.client.get(f"/i/{self.pay}/").status_code, 404)  # wrong kind of token
        self.assertEqual(self.client.get(f"/p/{self.view}/").status_code, 404)
        self.assertEqual(self.client.post("/p/nope/declare").status_code, 404)

    def test_invoice_page_in_contact_language(self):
        r = self.client.get(f"/i/{self.view}/")
        self.assertContains(r, 'lang="sk"')
        self.assertContains(r, "Faktúra")
        self.assertContains(r, self.invoice.number)
        self.assertNotContains(r, "SK0000000000001")  # no payment details on the view page
        r = self.client.get(f"/i/{self.view}/?lang=de")
        self.assertContains(r, 'lang="de"')
        r = self.client.get(f"/i/{self.view}/?lang=zz")
        self.assertContains(r, 'lang="sk"')

    def test_pay_page_shows_beneficiary_and_bank_addresses(self):
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertNotContains(r, "Beneficiary address")  # not set: no empty label
        self.bank.beneficiary_address = "Hlavná 1, 811 01 Bratislava"
        self.bank.bank_address = "Bankova 9, Tbilisi"
        self.bank.save()
        # A sent invoice keeps the details it was sent with (audit API-4); the next one has them.
        self.assertNotContains(self.client.get(f"/p/{self.pay}/?lang=en"), "Hlavná 1")
        inv, _ = create_invoice(self.org, self.contact, currency="EUR", offered_methods=[f"bank:{self.bank.pk}"],
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 133700}])
        _inv, _view, pay = send_invoice(inv)
        r = self.client.get(f"/p/{pay}/?lang=en")
        for needle in ("Beneficiary address", "Hlavná 1, 811 01 Bratislava", "Bank address", "Bankova 9, Tbilisi"):
            self.assertContains(r, needle)
        self.assertContains(self.client.get(f"/p/{pay}/"), "Adresa príjemcu")  # contact language sk

    def test_pay_page_shows_intermediary_and_reference_and_our_fees(self):
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        for needle in ("Seller s.r.o.", "SK0000000000001", "Bank A", "BANKSKBX", "Inter Bank", "INTERXXX9", "C-1",
                       "1,337.00 EUR", payment_reference(self.invoice), "OUR", "https://revolut.me/seller",
                       "Pay with the app"):
            self.assertContains(r, needle)
        r = self.client.get(f"/p/{self.pay}/")  # contact language sk
        self.assertContains(r, "Zaplaťte v aplikácii")

    def test_pdf_download_headers(self):
        try:
            from weasyprint import HTML  # noqa: F401
        except Exception:
            self.skipTest("WeasyPrint system libraries are missing")
        r = self.client.get(f"/i/{self.view}/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["Content-Disposition"], f'attachment; filename="invoice-{self.invoice.number}.pdf"')
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_privacy_headers_on_every_portal_response(self):
        for url in (f"/i/{self.view}/", f"/p/{self.pay}/", f"/p/{self.pay}/status"):
            r = self.client.get(url)
            self.assertIn("no-store", r["Cache-Control"], url)
            self.assertEqual(r["Referrer-Policy"], "same-origin")
            self.assertEqual(r["X-Robots-Tag"], "noindex")

    @override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"])
    def test_declare_moves_invoice_to_declared_and_notifies_staff_outbox(self):
        r = self.client.post(f"/p/{self.pay}/declare", {"network": "other", "tx_hash": " abc123 ", "note": "paid today"})
        self.assertEqual(r.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "declared")
        d = Declaration.objects.get()
        self.assertEqual((d.tx_hash, d.network, d.note, d.state), ("abc123", "other", "paid today", "open"))
        msg = OutboxMessage.objects.get(kind="declared_staff")
        self.assertEqual(msg.to, ["staff@example.com"])
        self.assertEqual(msg.language, "en")
        # The invoice number and the staff link only: no client name, no total (audit DATA-7).
        self.assertEqual(sorted(msg.body_context), ["admin_url", "ref"])
        self.assertEqual(msg.body_context["ref"], self.invoice.number)
        self.assertIn(f"/invoices/{self.invoice.pk}/", msg.body_context["admin_url"])
        # the page now says so, and a second declaration is recorded without a second transition
        self.assertContains(self.client.get(f"/p/{self.pay}/?lang=en"), "We have been told")
        self.assertEqual(self.client.post(f"/p/{self.pay}/declare", {}).status_code, 302)
        self.assertEqual(Declaration.objects.count(), 2)
        self.assertEqual(OutboxMessage.objects.filter(kind="declared_staff").count(), 1)  # no repeat mail

    def test_declare_without_staff_addresses_still_works(self):
        self.assertEqual(self.client.post(f"/p/{self.pay}/declare", {}).status_code, 302)
        self.assertFalse(OutboxMessage.objects.filter(kind="declared_staff").exists())

    def test_declare_rejects_overlong_input(self):
        r = self.client.post(f"/p/{self.pay}/declare", {"tx_hash": "x" * 129})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Declaration.objects.exists())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "sent")

    def test_declare_requires_csrf_and_post(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        self.assertEqual(c.post(f"/p/{self.pay}/declare", {}).status_code, 403)
        self.assertEqual(self.client.get(f"/p/{self.pay}/declare").status_code, 405)

    def test_declare_on_paid_invoice_409(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="paid")
        self.assertEqual(self.client.post(f"/p/{self.pay}/declare", {}).status_code, 409)

    def test_declare_on_void_invoice_409(self):
        void_invoice(self.invoice, "mistake", None)
        self.assertEqual(self.client.post(f"/p/{self.pay}/declare", {}).status_code, 409)
        self.assertFalse(Declaration.objects.exists())

    def test_void_invoice_page_says_void(self):
        void_invoice(self.invoice, "mistake", None)
        for url in (f"/i/{self.view}/?lang=en", f"/p/{self.pay}/?lang=en"):
            r = self.client.get(url)
            self.assertContains(r, "This invoice has been voided")
            self.assertNotContains(r, "SK0000000000001")
            self.assertNotContains(r, "mistake")
        self.assertContains(self.client.get(f"/p/{self.pay}/status?lang=en"), "voided")

    def test_status_partial(self):
        r = self.client.get(f"/p/{self.pay}/status?lang=en")
        self.assertContains(r, "Awaiting payment")
        self.assertNotContains(r, "<html")

    def test_rate_limit_429(self):
        caches["ratelimit"].clear(); cache.clear()
        for _ in range(60):
            self.assertEqual(self.client.get(f"/p/{self.pay}/status").status_code, 200)
        r = self.client.get(f"/p/{self.pay}/status?lang=en")
        self.assertContains(r, "Too many requests", status_code=429)
        # another token from the same address has its own allowance
        self.assertEqual(self.client.get(f"/i/{self.view}/").status_code, 200)

    def test_csp_nonce_and_no_inline_handlers(self):
        for url in (f"/i/{self.view}/", f"/p/{self.pay}/"):
            r = self.client.get(url)
            csp = r["Content-Security-Policy"]
            m = re.search(r"script-src [^;]*'nonce-([A-Za-z0-9_\-]+)'", csp)
            self.assertTrue(m, csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertNotRegex(r.content.decode(), r"\son[a-z]+\s*=")
            self.assertNotIn(" style=", r.content.decode())
            for tag in re.findall(r"<script\b[^>]*>", r.content.decode()):
                self.assertTrue("src=" in tag or f'nonce="{m.group(1)}"' in tag, tag)
        a = self.client.get(f"/p/{self.pay}/")["Content-Security-Policy"]
        b = self.client.get(f"/p/{self.pay}/")["Content-Security-Policy"]
        self.assertNotEqual(a, b)

    def test_htmx_is_served_locally(self):
        html = self.client.get(f"/p/{self.pay}/").content.decode()
        self.assertIn("/static/portal/htmx.min.js", html)
        self.assertNotIn("//cdn", html)
        self.assertNotIn("unpkg", html)

    def test_declare_works_from_a_real_browser_with_csrf_and_same_origin(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        page = c.get(f"/p/{self.pay}/?lang=en", secure=True)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertContains(page, '<meta name="referrer" content="same-origin">')
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        r = c.post(
            f"/p/{self.pay}/declare?lang=en", {"csrfmiddlewaretoken": token, "tx_hash": "abc"}, secure=True,
            HTTP_ORIGIN="https://testserver", HTTP_REFERER=f"https://testserver/p/{self.pay}/?lang=en",
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Declaration.objects.count(), 1)
        # what no-referrer used to produce: an opaque origin
        r = c.post(f"/p/{self.pay}/declare", {"csrfmiddlewaretoken": token}, secure=True, HTTP_ORIGIN="null")
        self.assertEqual(r.status_code, 403)

    def test_random_tokens_share_one_bucket_per_address(self):
        for i in range(30):
            self.assertEqual(self.client.get(f"/i/random{i}/").status_code, 404)
        r = self.client.get("/i/random-more/?lang=en")
        self.assertContains(r, "Too many requests", status_code=429)
        # a valid link is unaffected: it has its own bucket
        self.assertEqual(self.client.get(f"/i/{self.view}/").status_code, 200)

    def test_void_page_names_a_payment_request(self):
        inv, _ = create_invoice(
            self.org, self.contact, currency="EUR", kind="payment_request",
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 100}],
        )
        inv, view, pay = send_invoice(inv, email_client=False)
        void_invoice(inv, "x", None)
        self.assertContains(self.client.get(f"/i/{view}/?lang=en"), "This payment request has been voided")
        self.assertContains(self.client.get(f"/p/{pay}/status?lang=en"), "This payment request has been voided")

    def test_admin_carries_csp_with_nonce(self):
        r = self.client.get("/hidden-admin-xyz/login/")
        csp = r["Content-Security-Policy"]
        self.assertRegex(csp, r"script-src [^;]*'nonce-[A-Za-z0-9_\-]+'")
        self.assertNotRegex(csp.split("style-src")[0], "unsafe-inline")
        self.assertIn("script-src 'self' 'nonce-", csp)
        script = csp.split("script-src")[1].split(";")[0]
        self.assertNotIn("unsafe-inline", script)


class PartialDeclarationTests(PortalBase):
    def test_pay_page_with_open_declaration_on_partial_invoice_says_declared(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="partially_paid")
        Declaration.objects.create(invoice=self.invoice, note="rest paid")
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, "We have been told that you have paid")
        self.assertNotContains(r, "Please pay the remaining amount")
        r = self.client.get(f"/p/{self.pay}/status?lang=en")
        self.assertContains(r, "We have been told that you have paid")
        self.assertNotContains(r, "Please pay the remaining amount")

    def test_partial_invoice_without_declaration_still_asks_for_the_rest(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="partially_paid")
        self.assertContains(self.client.get(f"/p/{self.pay}/?lang=en"), "Please pay the remaining amount")
