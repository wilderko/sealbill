"""The crypto pay page: method picker, quotes, QR, declaration check, live status."""
import html
import re
from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache, caches
from django.test import TestCase, override_settings
from django.utils import dateformat, formats

from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Declaration, Payment
from apps.payments.services import declare_payment
from apps.quotes.models import Quote
from apps.rails import fx, http, uris
from apps.rails.fx import RatePair
from apps.rails.models import RailAccount
from apps.vault.testing import UnsealedVaultMixin

EVM = "0x" + "ab" * 20
TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
REAL_PRICE_IN = fx.price_in  # before any test patches it


def fake_price_in(asset, currency):
    if asset == currency:
        return RatePair(Decimal(1), Decimal(1), "identity")
    rate = {"USDC": Decimal("0.9237"), "USDT": Decimal("0.9237")}.get((asset), Decimal(1))
    return RatePair(rate, rate * Decimal("1.001"), "kraken+coinbase")


class CryptoBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        caches["ratelimit"].clear(); cache.clear()
        patcher = mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in)
        self.price_in = patcher.start()
        self.addCleanup(patcher.stop)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.bank = add_bank(self.org)
        self.evm = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                              networks=["arbitrum", "ethereum"], assets=["USDC"])
        self.tron = RailAccount.objects.create(org=self.org, kind="tron", label="Tron", address=TRON,
                                               networks=["tron"], assets=["USDT"])
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján Žilinský")
        self.invoice, self.view, self.pay = self.sent([f"bank:{self.bank.pk}", f"rail:{self.evm.pk}"])

    def sent(self, methods, total=10000):
        inv = create_invoice(self.org, self.contact, currency="EUR", offered_methods=methods, idempotency_key=str(methods),
                             lines=[{"name": "Service", "quantity": 1, "unit_price_minor": total}])[0]
        return send_invoice(inv, email_client=False)

    def ask(self, method=None, network="arbitrum", asset="USDC", refresh="0", **extra):
        return self.client.post(f"/p/{self.pay}/quote/?lang=en", {
            "method": method or f"rail:{self.evm.pk}", "network": network, "asset": asset, "refresh": refresh}, **extra)


class PayPageTests(CryptoBase):
    def test_pay_page_lists_only_offered_rails(self):
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, "USDC on Arbitrum One")
        self.assertContains(r, "USDC on Ethereum")  # enabled on the account, offered with it
        self.assertNotContains(r, "Tron")  # an account of the issuer the invoice does not offer
        self.assertEqual(Quote.objects.count(), 0)  # looking creates nothing
        self.evm.active = False
        self.evm.save()
        self.assertNotContains(self.client.get(f"/p/{self.pay}/?lang=en"), "USDC on Arbitrum")

    def test_crypto_comes_first_and_opens_by_default(self):
        """Owner 2026-11-09: a customer sees crypto first and clicks through to a bank transfer or a card."""
        body = self.client.get(f"/p/{self.pay}/?lang=en").content.decode()
        self.assertLess(body.index('id="tab-crypto"'), body.index('id="tab-bank"'))
        self.assertLess(body.index('id="panel-crypto"'), body.index('id="panel-bank"'))
        self.assertIn('data-tabs-start="panel-crypto"', body)

    def test_without_crypto_the_page_opens_on_the_bank_transfer(self):
        inv, view, pay = self.sent([f"bank:{self.bank.pk}"])
        body = self.client.get(f"/p/{pay}/?lang=en").content.decode()
        self.assertIn('id="panel-bank"', body)
        self.assertNotIn('id="panel-crypto"', body)
        self.assertNotIn('data-tabs-start="panel-crypto"', body)

    def test_status_poll_never_creates_a_quote(self):
        self.client.get(f"/p/{self.pay}/status?lang=en")
        self.assertEqual(Quote.objects.count(), 0)

    def test_no_inline_script_or_handlers(self):
        pages = [self.client.get(f"/p/{self.pay}/"), self.ask()]
        for r in pages:
            body = r.content.decode()
            self.assertNotRegex(body, r"\son[a-z]+\s*=")
            self.assertNotIn(" style=", body)
            for tag in re.findall(r"<script\b[^>]*>", body):
                self.assertIn("src=", tag)
        self.assertContains(pages[0], "portal/quote.js")


class QuoteTests(CryptoBase):
    def test_quote_partial_shows_exact_amount_uri_and_qr(self):
        r = self.ask(HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        q = Quote.objects.get()
        amount = uris.decimal_amount(int(q.amount_base_units), 6)
        body = r.content.decode()
        self.assertNotIn("<html", body)  # the fragment, not a page
        self.assertIn(f"{amount} USDC", body)
        self.assertIn(EVM, body)
        self.assertIn(html.escape(q.payment_uri), body)
        self.assertRegex(body, r'<img[^>]+src="data:image/svg\+xml;base64,[A-Za-z0-9+/=]+"')
        self.assertIn("Send exactly this amount of USDC on Arbitrum One. "
                      "Another token or network cannot be matched automatically.", html.unescape(body))
        # One date format per page: the validity reads like the invoice's own dates, in UTC.
        self.assertIn(f'{dateformat.format(q.expires_at, formats.get_format("DATE_FORMAT"))} '
                      f'{q.expires_at.strftime("%H:%M")} UTC', body)
        self.assertIn("UTC", body)
        self.assertIn('data-expires="', body)
        self.assertIn('name="refresh" value="1"', body)

    def test_plain_post_renders_the_whole_page_with_the_quote(self):
        r = self.ask()
        self.assertContains(r, "<html")
        self.assertContains(r, "Send exactly this amount")

    def test_refresh_cancels_previous_quote(self):
        self.ask()
        first = Quote.objects.get()
        self.ask()  # same key, still fresh: handed out again
        self.assertEqual(Quote.objects.count(), 1)
        self.ask(refresh="1")
        first.refresh_from_db()
        self.assertEqual(first.state, "cancelled")
        self.assertEqual(Quote.objects.filter(state="open").count(), 1)

    def test_quote_rate_limited_after_10(self):
        for _ in range(10):
            self.assertEqual(self.ask().status_code, 200)
        r = self.ask()
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Too many attempts", status_code=429)

    def test_the_quote_bucket_is_per_address_across_links_and_one_ipv6_64(self):
        """Final review I3/I4: new orders through a public link are new pay tokens, so a bucket per token
        let one address quote without end. One /64 is one address."""
        _inv, _view, other_pay = self.sent([f"rail:{self.evm.pk}"], total=12000)
        for i in range(10):
            self.assertEqual(self.ask(REMOTE_ADDR=f"2001:db8:1:2::{i + 1}").status_code, 200)
        r = self.client.post(f"/p/{other_pay}/quote/?lang=en", {"method": f"rail:{self.evm.pk}", "network": "arbitrum",
                                                               "asset": "USDC", "refresh": "0"},
                             REMOTE_ADDR="2001:db8:1:2:ffff::9")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.ask(REMOTE_ADDR="2001:db8:1:3::1").status_code, 200)  # another /64

    def test_a_twenty_first_refresh_says_so_honestly_in_the_page_language(self):
        """Residual R1: the refusal says what happened and when to come back, in every site language."""
        for i in range(20):
            self.assertEqual(self.ask(refresh="1", REMOTE_ADDR=f"192.0.2.{i + 1}").status_code, 200)
        r = self.ask(refresh="1", REMOTE_ADDR="192.0.2.99")
        self.assertContains(r, "There have been too many payment attempts on this invoice today. Please try again "
                               "tomorrow, or contact us.", status_code=409)
        self.assertNotContains(r, "a few minutes", status_code=409)
        self.assertEqual(Quote.objects.filter(invoice=self.invoice).count(), 20)
        r = self.client.post(f"/p/{self.pay}/quote/?lang=sk", {"method": f"rail:{self.evm.pk}", "network": "arbitrum",
                                                              "asset": "USDC", "refresh": "1"},
                             REMOTE_ADDR="192.0.2.100")
        self.assertContains(r, "Na túto faktúru bolo dnes príliš veľa pokusov o platbu.", status_code=409)

    def test_quote_refused_on_void_invoice(self):
        void_invoice(self.invoice, "x", None)
        r = self.ask(HTTP_HX_REQUEST="true")
        self.assertContains(r, "can no longer be paid")
        self.assertNotContains(r, "not_payable")
        r = self.ask()
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, "has been voided", status_code=409)
        self.assertEqual(Quote.objects.count(), 0)

    def test_a_method_the_invoice_does_not_offer_is_refused(self):
        r = self.ask(method=f"rail:{self.tron.pk}", network="tron", asset="USDT")
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "not available", status_code=400)
        self.assertEqual(Quote.objects.count(), 0)
        r = self.ask(method="rail:abc")
        self.assertEqual(r.status_code, 400)

    def test_rates_unavailable_message(self):
        self.price_in.side_effect = fx.RatesUnavailable("down")
        with self.assertLogs("apps.quotes.services", "WARNING"):
            r = self.ask()
        self.assertContains(r, "Exchange rates are unavailable right now. Try again in a minute.", status_code=503)
        self.assertNotContains(r, "rates_unavailable", status_code=503)

    def timeouts(self, method):
        """Every outbound `requests.<method>` times out, the guarded calls to a staff-chosen URL too (a
        `requests.Session`, audit SEC-4); returns the timeouts it was given."""
        seen = []

        def timing_out(*args, timeout=None, **kw):
            seen.append(timeout)
            raise requests.Timeout()

        self.enterContext(override_settings(RAILS_HTTP_ENABLED=True))
        self.enterContext(mock.patch(f"apps.rails.http.requests.{method}", side_effect=timing_out))
        self.enterContext(mock.patch(f"requests.Session.{method}", side_effect=timing_out))
        return seen

    def test_quote_waits_at_most_five_seconds_on_the_exchange_rates(self):
        self.price_in.side_effect = REAL_PRICE_IN
        seen = self.timeouts("get")
        with self.assertLogs("apps.quotes.services", "WARNING"), self.assertLogs("apps.rails.http", "WARNING"):
            r = self.ask()
        self.assertContains(r, "Exchange rates are unavailable right now.", status_code=503)
        self.assertEqual(seen, [5])  # not the 10 s an exchange-rate call may take in the worker

    def test_quote_waits_at_most_five_seconds_on_the_gateway(self):
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"},
                                            credentials={"api_key": "KEY"})
        invoice, view, pay = self.sent([f"rail:{btcpay.pk}"])
        seen = self.timeouts("post")
        with self.assertLogs("apps.quotes.services", "WARNING"), self.assertLogs("apps.rails.http", "WARNING"):
            r = self.client.post(f"/p/{pay}/quote/?lang=en", {"method": f"rail:{btcpay.pk}", "network": "bitcoin",
                                                              "asset": "BTC", "refresh": "0"})
        self.assertContains(r, "This payment method is temporarily unavailable.", status_code=503)
        self.assertEqual(seen, [5])

    def test_every_error_code_has_a_sentence(self):
        from apps.portal.views import QUOTE_ERROR_TEXT
        from apps.quotes.errors import QUOTE_ERROR_CODES
        self.assertEqual(set(QUOTE_ERROR_TEXT), set(QUOTE_ERROR_CODES))


class StatusTests(CryptoBase):
    def payment(self, **kw):
        defaults = dict(invoice=self.invoice, rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                        received_amount=Decimal("108.27"), state="detected")
        defaults.update(kw)
        return Payment.objects.create(**defaults)

    def status(self):
        return self.client.get(f"/p/{self.pay}/status?lang=en").content.decode()

    def test_status_shows_detected_as_waiting(self):
        self.payment()
        body = self.status()
        self.assertIn("Payment seen, waiting for confirmation", body)
        self.assertNotIn("Awaiting payment", body)

    def test_status_shows_review_as_being_checked(self):
        self.payment(state="needs_review", review_reason="amount_low")
        body = self.status()
        self.assertIn("Payment received, our staff are checking it", body)
        self.assertNotIn("Awaiting payment", body)

    def test_monero_waiting_for_staff_reads_as_being_checked(self):
        self.payment(rail="btcpay", network="monero", asset="XMR", needs_staff_confirmation=True)
        body = self.status()
        self.assertIn("our staff are checking it", body)
        self.assertNotIn("waiting for confirmation", body)

    def test_review_wins_over_detected(self):
        self.payment()
        self.payment(state="needs_review", tx_hash="b")
        self.assertIn("our staff are checking it", self.status())

    def test_rejected_payment_changes_nothing(self):
        self.payment(state="rejected")
        self.assertIn("Awaiting payment", self.status())

    def test_stale_open_declaration_does_not_unsettle_a_paid_invoice(self):
        declare_payment(self.invoice)
        self.assertIn("We have been told", self.status())
        for status in ("paid", "overpaid"):
            type(self.invoice).objects.filter(pk=self.invoice.pk).update(status=status)
            self.assertTrue(Declaration.objects.filter(invoice=self.invoice, state="open").exists())
            body = self.status()
            self.assertIn("Paid. Thank you.", body)
            self.assertNotIn("We have been told", body)

    def test_a_payment_in_review_does_not_hide_a_paid_invoice(self):
        self.payment(state="needs_review")
        type(self.invoice).objects.filter(pk=self.invoice.pk).update(status="paid")
        self.assertIn("Paid. Thank you.", self.status())


class DeclarationTests(CryptoBase):
    def post(self, **data):
        return self.client.post(f"/p/{self.pay}/declare", data)

    def test_declaration_with_hash_verifies_immediately(self):
        with mock.patch("apps.portal.views.verify_declaration") as verify:
            r = self.post(network="arbitrum", tx_hash=self.GOOD, note="")
        self.assertEqual(r.status_code, 302)
        d = Declaration.objects.get()
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[0].pk, d.pk)

    def test_unavailable_chain_is_swallowed(self):
        with mock.patch("apps.portal.views.verify_declaration", side_effect=http.Unavailable("chain:arbitrum", "down")):
            r = self.post(network="arbitrum", tx_hash=self.GOOD)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Declaration.objects.count(), 1)

    GOOD = "0x" + "ab" * 32

    def test_in_request_verify_is_bounded(self):
        from apps.rails import http as rails_http
        seen = {}

        with mock.patch("apps.portal.views.verify_declaration") as verify:
            def run(declaration):
                seen["cap"] = rails_http._capped(20)
                try:
                    rails_http.first_answer(["https://a.example", "https://b.example"],
                                            lambda u: seen.setdefault("urls", []).append(u) or (_ for _ in ()).throw(rails_http.Unavailable("x", "t")))
                except rails_http.Unavailable:
                    pass
            verify.side_effect = run
            self.post(network="arbitrum", tx_hash=self.GOOD)
        self.assertEqual(seen["cap"], 3)
        self.assertEqual(seen["urls"], ["https://a.example"])
        self.assertEqual(rails_http._capped(20), 20)  # outside a request nothing is capped

    def test_malformed_evm_hash_makes_no_call(self):
        with mock.patch("apps.portal.views.verify_declaration") as verify, \
                mock.patch("apps.rails.http.requests") as req:
            r = self.post(network="arbitrum", tx_hash="zz' or 1=1 --")
        self.assertEqual(r.status_code, 302)
        verify.assert_not_called()
        req.post.assert_not_called()
        self.assertEqual(Declaration.objects.count(), 1)  # the declaration still stands

    def test_in_request_verify_once_per_invoice_per_300s(self):
        with mock.patch("apps.portal.views.verify_declaration") as verify:
            self.post(network="arbitrum", tx_hash=self.GOOD)
            self.post(network="arbitrum", tx_hash="0x" + "cd" * 32)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(Declaration.objects.count(), 2)

    def test_any_failure_after_the_declaration_is_saved_is_not_a_500(self):
        with mock.patch("apps.portal.views.verify_declaration", side_effect=RuntimeError("boom")):
            with self.assertLogs("apps.portal.views", "INFO") as logs:
                r = self.post(network="arbitrum", tx_hash=self.GOOD)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Declaration.objects.count(), 1)
        self.assertIn("RuntimeError", "".join(logs.output))
        self.assertNotIn("boom", "".join(logs.output))

    def test_no_check_without_hash_or_for_other_networks(self):
        with mock.patch("apps.portal.views.verify_declaration") as verify:
            self.post(network="arbitrum", tx_hash="")
            self.post(network="other", tx_hash=self.GOOD)
        verify.assert_not_called()
        self.assertEqual(Declaration.objects.count(), 2)

    def test_network_is_a_choice_of_the_offered_ones_plus_other(self):
        body = self.client.get(f"/p/{self.pay}/?lang=en").content.decode()
        sel = re.search(r'<select name="network".*?</select>', body, re.S).group(0)
        self.assertIn('value="arbitrum"', sel)
        self.assertIn('value="ethereum"', sel)
        self.assertIn('value="other"', sel)
        self.assertNotIn('value="tron"', sel)
        r = self.post(network="solana", tx_hash="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Declaration.objects.count(), 0)


class QuoteLanguageTests(CryptoBase):
    def test_quote_page_is_translated_in_every_language(self):
        from django.conf import settings
        english = None
        for lang, _name in settings.LANGUAGES:
            caches["ratelimit"].clear()
            r = self.client.post(f"/p/{self.pay}/quote/?lang={lang}", {
                "method": f"rail:{self.evm.pk}", "network": "arbitrum", "asset": "USDC", "refresh": "0"},
                HTTP_HX_REQUEST="true")
            text = r.content.decode()
            for leak in ("{%", "{{", "{#", "%(", "network_name"):
                self.assertNotIn(leak, text, f"{lang}: {leak}")
            if lang == "en":
                english = text
                continue
            self.assertNotIn("Send exactly this amount", text, lang)
            self.assertNotIn("Amount to send", text, lang)
            self.assertNotIn("Get a new amount", text, lang)
            self.assertIn("USDC", text)
            self.assertIn("Arbitrum One", text)
        self.assertIn("Send exactly this amount", english)

    def test_every_refusal_sentence_is_translated(self):
        from django.conf import settings
        from django.utils import translation
        from apps.portal.views import QUOTE_ERROR_TEXT
        for lang, _name in settings.LANGUAGES:
            if lang == "en":
                continue
            with translation.override(lang):
                for code, text in QUOTE_ERROR_TEXT.items():
                    with translation.override("en"):
                        english = str(text)
                    self.assertNotEqual(str(text), english, f"{lang}/{code}")


class StaffConfirmedAssetNoteTests(CryptoBase):
    NOTE = "A person confirms Monero and Zcash payments. This may take a while during European night hours."

    def offer(self, assets):
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=assets,
                                            config={"url": "https://btcpay.example", "store_id": "S1"},
                                            credentials={"api_key": "KEY"})
        return self.sent([f"rail:{btcpay.pk}"])[2]

    def test_pay_page_no_longer_says_a_person_confirms_xmr_or_zec(self):
        """Owner 2026-11-09: Monero and Zcash are confirmed by the chain like every other coin."""
        for asset in ("XMR", "ZEC"):
            pay = self.offer([asset])
            self.assertNotContains(self.client.get(f"/p/{pay}/?lang=en"), "A person confirms")

    def test_pay_page_is_silent_when_only_other_assets_are_offered(self):
        self.assertNotContains(self.client.get(f"/p/{self.pay}/?lang=en"), "A person confirms")
        pay = self.offer(["BTC"])
        self.assertNotContains(self.client.get(f"/p/{pay}/?lang=en"), "A person confirms")

    def test_quote_partial_leaves_the_note_to_the_page(self):
        # The note is on the pay page; the quote renders inside it, so repeating it showed it twice.
        from django.template.loader import render_to_string
        base = {"quote": mock.Mock(address="", bolt11="", rail_account=mock.Mock(method_key="m"), network="monero",
                                   asset="XMR"),
                "amount": "1", "network_name": "Monero", "token": "t", "lang_query": "",
                "expires_iso": "2026-01-01T00:00:00+00:00", "expires_at": None}
        html_ = render_to_string("portal/_quote.html", {**base, "asset": "XMR", "staff_confirmed": True})
        self.assertNotIn("A person confirms Monero and Zcash payments.", html_)

