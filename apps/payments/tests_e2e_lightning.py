"""End to end on Lightning over Nostr Wallet Connect (receive-only, Alby Hub).

The NWC client itself (`nostr_sdk.NostrWalletConnect`) is the boundary: a fake hub
(`e2e_helpers.FakeHub`) makes the invoices and says what became of them, while the system's own
`nwc._run` (asyncio, the request budget, the test guard lifted by `RAILS_HTTP_ENABLED`) runs for
real. Only SETTLED is money; ACCEPTED is a hold."""
from decimal import Decimal
from unittest import mock

from apps.payments.e2e_helpers import E2EBase, FakeHub
from apps.payments.models import Payment
from apps.quotes.models import Quote
from apps.rails.models import RailAccount

SATS = 166_667  # 100.00 EUR at 60 000 EUR per BTC, rounded up to the satoshi


class LightningBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        self.enterContext(mock.patch("nostr_sdk.NostrWalletConnectUri.parse", return_value=object()))
        self.enterContext(mock.patch("nostr_sdk.NostrWalletConnect", side_effect=self.hub.connect))
        self.rail = RailAccount.objects.create(
            org=self.org, kind="lightning", label="Alby Hub", assets=["BTC"], config={"backend": "nwc"},
            credentials={"nwc_url": "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32})
        self.method = self.rail.method_key

    def sent(self, **kw):
        return self.invoice([self.method], **kw)

    def ln_quote(self, pay):
        quote = self.quote(pay, self.method, "lightning", "BTC")
        self.assertEqual(quote.amount_base_units, str(SATS))
        self.assertTrue(quote.bolt11.startswith("lnbc"))
        self.assertEqual(self.hub.made[-1].amount, SATS * 1000)
        return quote

    def poll(self):
        self.work("lightning_poll")


class LightningLifecycleTests(LightningBase):
    def test_lightning_whole_lifecycle(self):
        pk = self.create_invoice([self.method])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        self.assertIn("Lightning", self.pay_page(pay))
        quote = self.ln_quote(pay)
        self.assertIn(quote.bolt11, self.client.post(pay + "quote/", {"method": self.method, "network": "lightning",
                                                                       "asset": "BTC"}, HTTP_HX_REQUEST="true")
                      .content.decode())
        self.poll()
        self.assertEqual(Payment.objects.count(), 0)  # not paid yet: nothing recorded
        self.hub.settle(quote.external_id)
        self.poll()
        self.poll()  # read again: one payment
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.external_id, payment.amount_minor, payment.received_amount),
                         ("confirmed", f"ln:{quote.external_id}", 10000, Decimal("0.00166667")))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_held_payment_is_not_money(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id, state="ACCEPTED")
        self.poll()
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
        self.assertNotSettled(pk, pay)


class LightningWrongMoneyTests(LightningBase):
    def test_underpaid_goes_to_review_with_its_share(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id, msat=SATS * 500)
        self.poll()
        payment = self.assertInReview(self.payments(pk)[0], "no_matching_invoice", invoice_pk=pk, pay_path=pay)
        self.assertEqual(payment.amount_minor, 5000)
        self.assertNotSettled(pk)

    def test_slightly_overpaid_settles_with_a_refund_in_sats(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id, msat=(SATS + 1000) * 1000)
        self.poll()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.refund_due_base_units), ("confirmed", "1000"))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_far_overpaid_waits_for_staff(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id, msat=SATS * 2000)
        self.poll()
        self.assertInReview(self.payments(pk)[0], "overpaid_beyond_ceiling", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)

    def test_an_expired_invoice_expires_the_quote(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id, state="EXPIRED")
        self.poll()
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "expired")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay)

    def test_settled_after_the_invoice_was_paid_by_bank_waits_for_staff(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        staff = self.staff_client()
        r = staff.post(f"/invoices/{pk}/payments/", {"amount": "100.00", "rail": "bank", "note": "SEPA"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "cancelled")
        self.hub.settle(quote.external_id)  # the client paid the Lightning invoice as well
        self.poll()
        payment = Payment.objects.get(rail="lightning")
        self.assertInReview(payment, "invoice_closed", invoice_pk=pk)
        self.assertEqual(self.inv(pk).status, "paid")
        self.assertEqual(Payment.objects.filter(invoice_id=pk, state="confirmed").count(), 1)


class LightningOutageTests(LightningBase):
    def test_an_unreachable_hub_changes_nothing_and_the_payment_is_found_later(self):
        pk, pay = self.sent()
        quote = self.ln_quote(pay)
        self.hub.settle(quote.external_id)
        self.hub.offline = True
        with self.assertLogs("apps.rails.gateways.lightning.nwc", "WARNING"):
            self.poll()
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
        self.assertIsNotNone(self.health(f"lightning:{self.rail.pk}").failing_since)
        self.hub.offline = False
        self.poll()
        self.assertIsNone(self.health(f"lightning:{self.rail.pk}").failing_since)
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_quote_refused_while_the_hub_is_unreachable(self):
        pk, pay = self.sent()
        self.hub.offline = True
        with self.assertLogs("apps.rails.gateways.lightning.nwc", "WARNING"), \
                self.assertLogs("apps.quotes.services", "WARNING"):
            r = self.client.post(pay + "quote/", {"method": self.method, "network": "lightning", "asset": "BTC"},
                                 HTTP_HX_REQUEST="true")
        self.assertContains(r, "This payment method is temporarily unavailable")
        self.assertFalse(Quote.objects.exists())
