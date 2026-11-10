"""Regression tests from the invoicing audit of 2026-11-03 that concern the client pages.

WEB-1: the "I have paid" form has a bucket of its own, 5 an hour per pay link whoever sends it. An
invoice holds at most three open declarations, a repeated `(network, tx_hash)` is folded into the
open one, `invoice.declared` goes out for the first open declaration only, and the 300 s chain job
looks at the newest open declaration of each invoice, at most 100 a pass.

WEB-6: the PDF has a bucket of its own, 10 per 10 minutes per view link whoever asks, and `?lang=`
names one of the nine site languages or nothing.

`DeclarationFloodTest` and `PdfBucketTest` are ported from `docs/superpowers/audit/2026-11-03-invoicing/
poc/web/poc_web_1.py` and `poc_web_6.py` in the liberation.services repository.
"""
from unittest import mock

from django.conf import settings
from django.core.cache import cache, caches
from django.test import TestCase

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments import matching
from apps.payments.models import Declaration
from apps.payments.services import declare_payment
from apps.rails.chains import base as chains
from apps.rails.models import RailAccount
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault
from apps.webhooks import delivery
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

EVM = "0x" + "ab" * 20
ALREADY_DECLARED = "We already have your notice of payment and are checking it. You do not need to send it again."


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


class CountingAdapter:
    def __init__(self):
        self.calls = []

    def transfers_in_tx(self, network, tx_hash):
        self.calls.append(tx_hash)
        return []


class DeclarationBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        caches["ratelimit"].clear(); cache.clear()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.bank = add_bank(self.org)
        self.evm = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                              networks=["arbitrum"], assets=["USDC"])
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Client")
        self.invoice, self.view, self.pay = self.sent()

    def sent(self):
        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                offered_methods=[f"bank:{self.bank.pk}", f"rail:{self.evm.pk}"],
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        invoice, view, pay = send_invoice(inv, email_client=False)
        return invoice, view, pay

    def declare(self, pay, n, ip="198.51.100.7", network="arbitrum"):
        return self.client.post(f"/p/{pay}/declare", {"network": network, "tx_hash": tx(n), "note": "x"},
                                HTTP_X_FORWARDED_FOR=ip)


class DeclarationFloodTest(DeclarationBase):
    """poc_web_1: one pay link, two client addresses, one minute. Every declaration used to be
    stored, audited, turned into a webhook delivery and looked up on chain on every 300 s pass."""

    def setUp(self):
        super().setUp()
        # The organization's webhook consumer (liberation.services in production) wants every event.
        self.endpoint = WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s",
                                                       events=["*"])

    def test_one_pay_link_floods_declarations_webhooks_and_the_chain_job(self):
        accepted = 0
        n = 0
        for ip in ("198.51.100.7", "198.51.100.8"):
            for i in range(60):  # RATE_LIMIT = 60 per minute per (address, link): the page limit
                r = self.declare(self.pay, n, ip=ip)
                n += 1
                accepted += r.status_code == 302

        adapter = CountingAdapter()
        with mock.patch.dict(chains._ADAPTERS, {"evm": adapter}):
            matching.verify_open_declarations()

        # The healthy endpoint gets one delivery per 15 s run, oldest first.
        with mock.patch("apps.webhooks.delivery.post", return_value=200), \
                mock.patch("apps.webhooks.delivery.check_destination"):
            delivery.flush()

        # Expected of a defended endpoint: a small cap per invoice / per link.
        self.assertLessEqual(Declaration.objects.filter(invoice=self.invoice).count(), 10)
        # What the fix makes of it: three open declarations, one event, one chain lookup a pass.
        self.assertEqual(accepted, 3)
        self.assertEqual(Declaration.objects.filter(invoice=self.invoice).count(), 3)
        self.assertEqual(AuditEntry.objects.filter(action="declaration.create").count(), 3)
        self.assertEqual(WebhookDelivery.objects.filter(endpoint=self.endpoint).count(), 1)
        self.assertEqual(len(adapter.calls), 1)


class DeclareBucketTest(DeclarationBase):
    def test_five_an_hour_per_link_whoever_sends_them(self):
        # Every one a different address, and the same hash each time: the bucket is the link's.
        codes = [self.client.post(f"/p/{self.pay}/declare", {"network": "other", "note": "paid"},
                                  HTTP_X_FORWARDED_FOR=f"198.51.100.{i}").status_code for i in range(6)]
        self.assertEqual(codes[:5], [302, 302, 302, 409, 409])  # the fourth and fifth reach the cap of three
        self.assertEqual(codes[5], 429)
        r = self.client.post(f"/p/{self.pay}/declare", {"network": "other", "note": "paid"},
                             HTTP_X_FORWARDED_FOR="203.0.113.99", HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(r, ALREADY_DECLARED, status_code=429)
        # Another invoice's link keeps its own allowance.
        _other, _view, other_pay = self.sent()
        self.assertEqual(self.declare(other_pay, 1).status_code, 302)

    def test_the_refusal_is_in_the_clients_language(self):
        for i in range(3):
            self.declare(self.pay, i)
        r = self.client.post(f"/p/{self.pay}/declare?lang=sk", {"network": "arbitrum", "tx_hash": tx(9)},
                             HTTP_X_FORWARDED_FOR="198.51.100.7")
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, "Vaše oznámenie o platbe už máme", status_code=409)


class DeclarationCapTest(DeclarationBase):
    def test_at_most_three_open_declarations(self):
        from apps.payments.services import TooManyDeclarations

        for i in range(3):
            declare_payment(self.invoice, network="arbitrum", tx_hash=tx(i))
        with self.assertRaises(TooManyDeclarations):
            declare_payment(self.invoice, network="arbitrum", tx_hash=tx(3))
        self.assertEqual(self.invoice.declarations.filter(state="open").count(), 3)
        # Once staff dismiss one, there is room again.
        from apps.payments.services import dismiss_declaration

        dismiss_declaration(self.invoice.declarations.order_by("pk").first(), reason="", user=None)
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(3))
        self.assertEqual(self.invoice.declarations.filter(state="open").count(), 3)

    def test_the_same_network_and_hash_is_folded_into_the_open_one(self):
        first = declare_payment(self.invoice, network="arbitrum", tx_hash=tx(7))
        again = declare_payment(self.invoice, network="arbitrum", tx_hash=tx(7).upper().replace("0X", "0x"))
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Declaration.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(AuditEntry.objects.filter(action="declaration.create").count(), 1)
        # Another network with the same hash is another claim.
        other = declare_payment(self.invoice, network="base", tx_hash=tx(7))
        self.assertNotEqual(other.pk, first.pk)

    def test_a_folded_declaration_takes_no_room_under_the_cap(self):
        for _ in range(5):
            declare_payment(self.invoice, network="arbitrum", tx_hash=tx(1))
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(2))
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(3))
        self.assertEqual(self.invoice.declarations.filter(state="open").count(), 3)

    def test_invoice_declared_goes_out_for_the_first_open_declaration_only(self):
        WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s", events=["*"])
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(1))
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(2))
        self.assertEqual(WebhookEvent.objects.filter(type="invoice.declared").count(), 1)
        # A new one after staff closed the open ones is news again.
        from apps.payments.services import dismiss_declaration

        for d in self.invoice.declarations.filter(state="open"):
            dismiss_declaration(d, reason="", user=None)
        declare_payment(self.invoice, network="arbitrum", tx_hash=tx(3))
        self.assertEqual(WebhookEvent.objects.filter(type="invoice.declared").count(), 2)


class VerifyPassTest(DeclarationBase):
    def test_only_the_newest_open_declaration_of_each_invoice_is_looked_up(self):
        second, _v, _p = self.sent()
        for invoice in (self.invoice, second):
            for i in range(3):
                Declaration.objects.create(invoice=invoice, network="arbitrum", tx_hash=tx(invoice.pk * 10 + i))
        adapter = CountingAdapter()
        with mock.patch.dict(chains._ADAPTERS, {"evm": adapter}):
            matching.verify_open_declarations()
        self.assertEqual(sorted(adapter.calls), sorted([tx(self.invoice.pk * 10 + 2), tx(second.pk * 10 + 2)]))

    def test_a_newer_note_without_a_hash_does_not_hide_the_hash(self):
        Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash=tx(5))
        Declaration.objects.create(invoice=self.invoice, network="other", note="sent it from my bank")
        adapter = CountingAdapter()
        with mock.patch.dict(chains._ADAPTERS, {"evm": adapter}):
            matching.verify_open_declarations()
        self.assertEqual(adapter.calls, [tx(5)])

    def test_at_most_a_hundred_rows_a_pass(self):
        self.assertEqual(matching.DECLARATIONS_PER_PASS, 100)
        invoices = [self.invoice] + [self.sent()[0] for _ in range(2)]
        for invoice in invoices:
            Declaration.objects.create(invoice=invoice, network="arbitrum", tx_hash=tx(invoice.pk))
        adapter = CountingAdapter()
        with mock.patch.dict(chains._ADAPTERS, {"evm": adapter}), \
                mock.patch.object(matching, "DECLARATIONS_PER_PASS", 2):
            matching.verify_open_declarations()
        self.assertEqual(adapter.calls, [tx(invoices[0].pk), tx(invoices[1].pk)])  # oldest first


class PdfBucketTest(UnsealedVaultMixin, TestCase):
    """poc_web_6 measured the cost: every GET of the PDF rendered it from scratch (about 110 ms in
    English, 220 ms in Korean), 60 a minute per address, in whichever language the caller named.
    The measurement is now an assertion on the bucket: ten renders per link per ten minutes,
    whoever asks. (The PoC's warm-up request and its timings are left out: the warm-up would be
    the eleventh render.)"""

    def setUp(self):
        super().setUp()
        caches["ratelimit"].clear(); cache.clear()
        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        bank = add_bank(org)
        self.contact, _ = find_or_create_contact(org, "a@example.com", legal_name="Client")
        inv, _ = create_invoice(org, self.contact, currency="EUR", offered_methods=[f"bank:{bank.pk}"],
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        _inv, self.view, _pay = send_invoice(inv, email_client=False)
        inv2, _ = create_invoice(org, self.contact, currency="EUR", offered_methods=[f"bank:{bank.pk}"],
                                 lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        _inv2, self.other_view, _pay2 = send_invoice(inv2, email_client=False)

    def test_pdf_cost(self):
        for lang in ("en", "ko"):
            for _ in range(5):
                r = self.client.get(f"/i/{self.view}/pdf?lang={lang}", HTTP_X_FORWARDED_FOR="198.51.100.7")
                self.assertEqual(r.status_code, 200)
        allowed = sum(self.client.get(f"/i/{self.view}/pdf?lang=ko", HTTP_X_FORWARDED_FOR="198.51.100.9").status_code == 200
                      for _ in range(3))
        self.assertEqual(allowed, 0, "a second address is limited by the link's bucket")

    def test_refused_pdf_says_so_and_other_links_are_untouched(self):
        with mock.patch("apps.portal.views.render_invoice_pdf", return_value=b"%PDF-1.7") as render:
            for i in range(10):
                self.assertEqual(self.client.get(f"/i/{self.view}/pdf", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code,
                                 200)
            r = self.client.get(f"/i/{self.view}/pdf?lang=en", HTTP_X_FORWARDED_FOR="10.0.1.1")
            self.assertEqual(r.status_code, 429)
            self.assertContains(r, "Too many attempts. Wait a few minutes and try again.", status_code=429)
            self.assertEqual(r["Cache-Control"], "no-store")
            self.assertEqual(render.call_count, 10)
            # The invoice page itself and another invoice's PDF are not limited by it.
            self.assertEqual(self.client.get(f"/i/{self.view}/", HTTP_X_FORWARDED_FOR="10.0.1.1").status_code, 200)
            self.assertEqual(self.client.get(f"/i/{self.other_view}/pdf", HTTP_X_FORWARDED_FOR="10.0.1.1").status_code,
                             200)

    def test_lang_names_one_of_the_nine_site_languages_or_nothing(self):
        codes = {code for code, _ in settings.LANGUAGES}
        with mock.patch("apps.portal.views.render_invoice_pdf", return_value=b"%PDF-1.7") as render:
            for lang in ("ko", "zh", "../../etc", "EN", "x" * 500):
                self.client.get(f"/i/{self.view}/pdf", {"lang": lang})
        used = [c.args[1] for c in render.call_args_list]
        self.assertEqual(used[0], "ko")
        self.assertTrue(set(used) <= codes, used)
        self.assertEqual(used[1:], ["en"] * 4)  # the contact's language (English here), never the caller's string

    def test_sealed_vault_answers_unavailable_not_500(self):
        vault.seal()
        r = self.client.get(f"/i/{self.view}/pdf")
        self.assertEqual(r.status_code, 503)
