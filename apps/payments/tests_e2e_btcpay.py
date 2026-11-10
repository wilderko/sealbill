"""End to end through BTCPay Server: BTC on-chain, Monero and Zcash.

Each quote is its own BTCPay invoice on a fake Greenfield API (`e2e_helpers.BTCPayServer`,
reached through `apps.rails.http`). The worker's `btcpay_poll` reads every invoice back from the
API: a webhook only makes it look sooner. Every coin (BTC, Monero, Zcash) is confirmed by code once
BTCPay says Settled at the store's confirmation threshold (owner, 2026-11-09)."""
import hashlib
import hmac
import json
from decimal import Decimal

from apps.payments.e2e_helpers import BTCPayServer, Clock, E2EBase
from apps.payments.models import Payment
from apps.quotes.models import Quote
from apps.rails.models import RailAccount
from apps.webhooks.models import WebhookEvent

WEBHOOK_SECRET = "btcpay-webhook-secret"
BTC_DUE = Decimal("0.00166667")  # 100.00 EUR at 60 000 EUR per BTC, rounded up to the satoshi


class BTCPayBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.server = BTCPayServer(self.internet, api_key="greenfield-key")
        self.rail = RailAccount.objects.create(
            org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR", "ZEC"],
            config={"url": self.server.base, "store_id": self.server.store},
            credentials={"api_key": "greenfield-key", "webhook_secret": WEBHOOK_SECRET})
        self.method = self.rail.method_key

    def sent(self, **kw):
        return self.invoice([self.method], **kw)

    def btc_quote(self, pay):
        quote = self.quote(pay, self.method, "bitcoin", "BTC")
        self.assertEqual((quote.amount_base_units, quote.rate_source), ("166667", "btcpay"))
        self.assertTrue(quote.checkout_url.startswith(self.server.base + "/i/"))
        return quote

    def poll(self):
        self.work("btcpay_poll")

    def hook(self, body: dict, secret: str = WEBHOOK_SECRET):
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(f"/hooks/btcpay/{self.rail.pk}/", raw, content_type="application/json",
                                HTTP_BTCPAY_SIG=sig)


class BTCPayLifecycleTests(BTCPayBase):
    def test_btc_processing_then_settled_whole_lifecycle(self):
        pk = self.create_invoice([self.method])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        self.assertIn("Bitcoin", self.pay_page(pay))
        quote = self.btc_quote(pay)
        eid = quote.external_id
        self.assertIn(f'href="{quote.checkout_url}"', self.client.post(
            pay + "quote/", {"method": self.method, "network": "bitcoin", "asset": "BTC"},
            HTTP_HX_REQUEST="true").content.decode())  # the same quote again: the checkout link to open
        self.server.pay(eid, BTC_DUE, status="Processing")
        self.assertEqual(self.hook({"type": "InvoiceReceivedPayment", "invoiceId": eid}).status_code, 200)
        self.assertTrue(Quote.objects.get(pk=quote.pk).check_soon)
        self.poll()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.external_id, payment.network), ("detected", f"btcpay:store1:{eid}",
                                                                                  "bitcoin"))
        self.assertIn("Payment seen, waiting for confirmation", self.status_text(pay))
        self.assertTrue(WebhookEvent.objects.filter(type="payment.detected", payment_id=payment.pk).exists())
        self.server.pay(eid, BTC_DUE, status="Settled")
        self.poll()
        self.poll()  # read again: nothing more happens
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.amount_minor, payment.received_amount),
                         ("confirmed", 10000, BTC_DUE))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_monero_and_zcash_settled_are_confirmed_by_code(self):
        for asset, network in (("XMR", "monero"), ("ZEC", "zcash")):
            with self.subTest(asset=asset):
                pk, pay = self.sent()
                quote = self.quote(pay, self.method, network, asset)
                self.server.pay(quote.external_id, self.server.due(quote.external_id), status="Settled")
                self.poll()
                (payment,) = self.payments(pk)
                self.assertEqual((payment.state, payment.needs_staff_confirmation), ("confirmed", False))
                self.assertFalse(self.staff_letters("confirm_staff"))
                self.assertPaidEverywhere(pk, payment, pay)
                self.poll()  # BTCPay saying Settled again changes nothing
                self.assertEqual(self.payments(pk), [payment])


class BTCPayWrongMoneyTests(BTCPayBase):
    def test_a_partial_payment_left_to_expire_waits_for_staff_with_its_share(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE / 2, status="Expired", additional="PaidPartial")
        self.poll()
        payment = self.assertInReview(self.payments(pk)[0], "btcpay_partial", invoice_pk=pk, pay_path=pay)
        self.assertEqual(payment.amount_minor, 5000)  # its share of the invoice
        self.assertNotSettled(pk)

    def test_settled_short_of_the_amount_is_never_confirmed(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE - Decimal("0.00000100"), status="Settled")
        self.poll()
        self.assertInReview(self.payments(pk)[0], "btcpay_partial", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)

    def test_paid_over_within_the_ceiling_settles_with_a_refund_in_satoshis(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE + Decimal("0.00005000"), status="Settled", additional="PaidOver")
        self.poll()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.refund_due_base_units), ("confirmed", "5000"))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_paid_far_over_waits_for_staff(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE * 2, status="Settled", additional="PaidOver")
        self.poll()
        self.assertInReview(self.payments(pk)[0], "overpaid_beyond_ceiling", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)

    def test_monero_sent_to_a_bitcoin_checkout_is_never_confirmed(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, Decimal("0.7"), status="Settled", method_id="XMR-CHAIN")
        self.poll()
        payment = self.assertInReview(self.payments(pk)[0], "btcpay_invalid", invoice_pk=pk, pay_path=pay)
        self.assertEqual(payment.network, "monero")
        self.assertNotSettled(pk)

    def test_late_marked_and_invalid_invoices_wait_for_staff(self):
        for status, additional, reason in (("Settled", "PaidLate", "btcpay_late"), ("Settled", "Marked", "btcpay_marked"),
                                           ("Invalid", "None", "btcpay_invalid")):
            with self.subTest(reason=reason):
                pk, pay = self.sent()
                quote = self.btc_quote(pay)
                self.server.pay(quote.external_id, BTC_DUE, status=status, additional=additional)
                self.poll()
                self.assertInReview(self.payments(pk)[0], reason, invoice_pk=pk, pay_path=pay)
                self.assertNotSettled(pk)

    def test_a_signed_webhook_never_decides_a_payment(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        r = self.hook({"type": "InvoiceSettled", "invoiceId": quote.external_id, "status": "Settled"})
        self.assertEqual(r.status_code, 200)
        self.poll()  # BTCPay's own API still says New: nothing was paid
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay)
        with self.assertLogs("apps.rails.views", "WARNING"):
            forged = self.hook({"type": "InvoiceSettled", "invoiceId": quote.external_id}, secret="guess")
        self.assertEqual(forged.status_code, 401)

    def test_an_expired_unpaid_checkout_expires_the_quote(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, Decimal("0"), status="Expired")
        self.poll()
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "expired")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay)


class BTCPayOutageTests(BTCPayBase):
    def test_btcpay_down_changes_nothing_and_the_payment_is_found_later(self):
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE, status="Settled")
        name = f"btcpay:{self.rail.pk}"
        for how in ("timeout", "503", "garbage"):
            with self.subTest(how=how):
                self.internet.down(self.server.base, how)
                with self.assertLogs("apps.rails", "WARNING"):
                    self.poll()
                self.assertEqual(Payment.objects.count(), 0)
                self.assertIsNotNone(self.health(name).failing_since)
                self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
                self.assertIsNone(Quote.objects.get(pk=quote.pk).read_at)
        self.internet.up(self.server.base)
        self.poll()
        self.assertIsNone(self.health(name).failing_since)
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_a_settlement_btcpay_reports_after_a_long_outage_is_still_found(self):
        clock = Clock(self)
        pk, pay = self.sent()
        quote = self.btc_quote(pay)
        self.server.pay(quote.external_id, BTC_DUE, status="Processing")
        self.poll()
        self.internet.down(self.server.base, "503")
        for _ in range(4):  # 28 hours of a dead BTCPay, the worker's hourly release running throughout
            clock.advance(7 * 3600)
            with self.assertLogs("apps.rails.http", "WARNING"):
                self.work("quotes_expire", "quotes_release", "btcpay_poll")
        self.internet.up(self.server.base)
        self.server.pay(quote.external_id, BTC_DUE, status="Settled")
        self.poll()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_quote_refused_while_btcpay_is_down(self):
        pk, pay = self.sent()
        self.internet.down(self.server.base, "connection")
        with self.assertLogs("apps.rails.http", "WARNING"), self.assertLogs("apps.quotes.services", "WARNING"):
            r = self.client.post(pay + "quote/", {"method": self.method, "network": "bitcoin", "asset": "BTC"},
                                 HTTP_HX_REQUEST="true")
        self.assertContains(r, "This payment method is temporarily unavailable")
        self.assertFalse(Quote.objects.exists())
        self.assertIsNotNone(self.health(f"btcpay:{self.rail.pk}").failing_since)
