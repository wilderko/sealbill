"""Fixes from the final review of the integration API (Part A): C1, I5, I6."""
from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.api.serializers import PaymentOutSerializer
from apps.invoices.services import void_invoice
from apps.orgs.models import Organization
from apps.payments import matching, services
from apps.payments.models import Payment
from apps.payments.tests_matching import MatchingBase
from apps.rails.models import RailAccount
from apps.webhooks.models import WebhookEndpoint, WebhookEvent


def shown(payment) -> dict:
    return PaymentOutSerializer(Payment.objects.get(pk=payment.pk)).data


class GatewayBase(MatchingBase):
    def setUp(self):
        super().setUp()
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)

    def xmr_quote(self, invoice, ext):
        return self.hold(invoice, 500_000_000_000, network="monero", asset="XMR", address="", account=self.btcpay,
                         external_id=ext, min_accept_base_units="500000000000", ceiling_base_units="500000000000")

    def xmr(self, quote, ext):
        return matching.record_gateway_payment(
            quote, network="monero", external_id=ext, received_amount=Decimal("0.5"), amount_minor=10000,
            state="detected", needs_staff_confirmation=True)


class ConfirmedReadsAsFinalTests(GatewayBase):
    def test_xmr_confirmed_by_staff_reads_final(self):
        p = self.xmr(self.xmr_quote(self.invoice, "btcpay:S1:x1"), "btcpay:S1:x1")
        self.assertFalse(shown(p)["final"])  # waiting for a person: not confirmed
        services.confirm_payment(p, self.staff, "seen")
        self.assertEqual(Payment.objects.get(pk=p.pk).final, False)  # the stored column is untouched
        self.assertTrue(shown(p)["final"])

    def test_manual_payment_reads_final(self):
        p = services.record_manual_payment(self.invoice, amount_minor=100, rail="bank", note="n", user=self.staff)
        self.assertTrue(shown(p)["final"])

    def test_gateway_payment_assigned_from_review_reads_final(self):
        other = self.sent()
        q = self.hold(self.invoice, 200_000, network="bitcoin", asset="BTC", address="", account=self.btcpay,
                      external_id="btcpay:S1:b2", min_accept_base_units="200000", ceiling_base_units="200000")
        void_invoice(self.invoice, "mistake", None)
        p = matching.record_gateway_payment(q, network="bitcoin", external_id="btcpay:S1:b2",
                                            received_amount=Decimal("0.002"), amount_minor=10000, state="confirmed")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")
        services.assign_payment(p, other, self.staff, amount_minor=10000, note="late")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "confirmed")
        self.assertTrue(shown(p)["final"])

    def test_chain_payment_below_finality_reads_false(self):
        p = Payment.objects.create(invoice=self.invoice, rail="evm", network="arbitrum", asset="USDC",
                                   amount_minor=0, received_amount="1", state="detected", final=False, tx_hash="0x" + "ab" * 32,
                                   rail_account=self.acct)
        self.assertFalse(shown(p)["final"])


class LateStaffConfirmedPaymentTests(GatewayBase):
    def test_late_xmr_on_a_void_invoice_goes_to_review_and_can_be_assigned(self):
        other = self.sent()
        q = self.xmr_quote(self.invoice, "btcpay:S1:x3")
        void_invoice(self.invoice, "mistake", None)
        p = self.xmr(q, "btcpay:S1:x3")
        r = Payment.objects.get(pk=p.pk)
        self.assertEqual((r.state, r.review_reason), ("needs_review", "invoice_closed"))
        services.assign_payment(p, other, self.staff, amount_minor=10000, note="late")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "confirmed")
        self.assertEqual(self.status(other), "paid")


class StaffAlertReachesRequesterTests(GatewayBase):
    def setUp(self):
        super().setUp()
        self.shop = Organization.objects.create(slug="shop", legal_name="Shop LLC", address="Main 2")
        type(self.invoice).objects.filter(pk=self.invoice.pk).update(requested_by_org=self.shop)
        bq = self.hold(self.invoice, 200_000, network="bitcoin", asset="BTC", address="", account=self.btcpay,
                       external_id="btcpay:S1:b1", min_accept_base_units="200000", ceiling_base_units="200000")
        matching.record_gateway_payment(bq, network="bitcoin", external_id="btcpay:S1:b1",
                                        received_amount=Decimal("0.002"), amount_minor=10000, state="confirmed")
        self.bq = bq

    def alert(self):
        self.assertTrue(matching.flag_gateway_invalidated(self.bq, external_id="btcpay:S1:b1", reason="dropped"))
        return WebhookEvent.objects.filter(type="staff.alert")

    def test_alert_reaches_the_shop_when_only_the_requester_has_an_endpoint(self):
        WebhookEndpoint.objects.create(org=self.shop, url="https://shop.example/h", secret="s", events=["*"])
        event = self.alert().get()
        self.assertEqual(event.data["kind"], "payment_invalid_after_confirmation")
        self.assertEqual(event.deliveries.count(), 1)

    def test_one_endpoint_is_not_told_twice(self):
        WebhookEndpoint.objects.create(org=self.org, url="https://eu.example/h", secret="s", events=["*"])
        WebhookEndpoint.objects.create(org=self.shop, url="https://shop.example/h", secret="s", events=["*"])
        self.assertEqual(self.alert().get().deliveries.count(), 2)


class OneConfirmedSumTests(GatewayBase):
    def test_confirmed_minor_is_confirmed_total_minor(self):
        from unittest import mock
        services.record_manual_payment(self.invoice, amount_minor=300, rail="bank", note="n", user=self.staff)
        self.assertEqual(services.confirmed_minor(self.invoice), 300)
        with mock.patch("apps.payments.services.confirmed_total_minor", return_value=7) as one:
            self.assertEqual(services.confirmed_minor(self.invoice), 7)
        one.assert_called_once()
