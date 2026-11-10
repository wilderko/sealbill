"""Plan C carry-overs in the matching engine: the TON gate and the dust floor."""
import datetime
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from apps.notify.models import OutboxMessage
from apps.payments import matching
from apps.payments.models import REVIEW_REASONS, Payment
from apps.payments.tests_matching import (
    EVM, K0, PAYER, TON_ADDR, TON_USDT, TX, TX2, MatchingBase, transfer,
)
from apps.rails import jobs
from apps.rails.fx import RatePair
from apps.rails.models import PendingTransfer, RailAccount
from apps.staff.tests import StaffBase


class TonGateTests(MatchingBase):
    def test_ton_rows_wait_while_ton_disabled(self):
        with override_settings(TON_ENABLED=True):
            ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                             networks=["ton"], assets=["USDT"])
        t = transfer(5_000_000, tx="ab" * 32, network="ton", contract=TON_USDT.contract, to=TON_ADDR)
        row = PendingTransfer.objects.create(account=ton, **PendingTransfer.fields_of(t))
        waiting = Payment.objects.create(
            rail="ton", network="ton", asset="USDT", amount_minor=0, received_amount="5", tx_hash="cd" * 32,
            state="detected", amount_base_units="5000000", to_address=TON_ADDR, invoice=self.invoice)
        self.adapter.statuses[waiting.tx_hash] = "final"
        with override_settings(TON_ENABLED=False):
            with mock.patch.object(matching, "ingest") as ingest:
                self.assertEqual(jobs._retry_pending(matching), set())
            ingest.assert_not_called()
            self.assertTrue(PendingTransfer.objects.filter(pk=row.pk).exists())
            self.assertEqual(matching.refresh_finality(), 0)
        self.assertEqual(Payment.objects.get(pk=waiting.pk).state, "detected")
        with override_settings(TON_ENABLED=True):  # and they are not forgotten
            with mock.patch.object(matching, "ingest") as ingest:
                jobs._retry_pending(matching)
            ingest.assert_called_once()
        self.assertFalse(PendingTransfer.objects.filter(pk=row.pk).exists())


class DustTests(MatchingBase):
    def tearDown(self):
        cache.delete("fx:ETH:USD")
        super().tearDown()

    def test_dust_goes_to_review_without_staff_mail(self):
        p = self.row(self.ingest(transfer(50_000)))  # 0.05 USDC
        self.assertEqual((p.state, p.review_reason, p.invoice_id, p.quote_id), ("needs_review", "dust", None, None))
        self.assertEqual(OutboxMessage.objects.filter(kind="review_staff").count(), 0)
        self.assertIn("dust", REVIEW_REASONS)
        self.assertEqual(self.status(), "sent")

    def test_real_small_payment_above_floor_is_not_dust(self):
        p = self.row(self.ingest(transfer(500_000)))  # 0.50 USDC
        self.assertNotEqual(p.review_reason, "dust")
        self.assertEqual(p.review_reason, "no_matching_invoice")
        self.assertEqual(OutboxMessage.objects.filter(kind="review_staff").count(), 1)

    def test_the_floor_itself_is_not_dust(self):
        p = self.row(self.ingest(transfer(100_000)))  # exactly 0.10 USDC
        self.assertNotEqual(p.review_reason, "dust")

    def test_eth_is_judged_at_the_cached_price_and_never_called_for(self):
        eth = transfer(10**13, contract="")  # 0.00001 ETH
        with mock.patch("apps.rails.fx._compute", side_effect=AssertionError("must not call out")):
            p = self.row(self.ingest(eth))  # no cached price: never dust
        self.assertNotEqual(p.review_reason, "dust")
        cache.set("fx:ETH:USD", RatePair(Decimal("2000"), Decimal("2000"), "test"), timeout=60)
        with mock.patch("apps.rails.fx._compute", side_effect=AssertionError("must not call out")):
            dust = self.row(self.ingest(transfer(10**13, contract="", tx=TX2)))  # 0.02 USD
            real = self.row(self.ingest(transfer(10**14, contract="", tx=TX2[:-2] + "ee")))  # 0.20 USD
        self.assertEqual(dust.review_reason, "dust")
        self.assertNotEqual(real.review_reason, "dust")


class DustOnTheStaffPageTests(StaffBase):
    def make(self, n, base_units, reason):
        return Payment.objects.create(
            rail="evm", network="arbitrum", asset="USDC", amount_minor=0, received_amount=Decimal(base_units) / 10**6,
            tx_hash="0x" + f"{n:02x}" * 32, state="needs_review", review_reason=reason, final=True,
            amount_base_units=str(base_units), to_address=EVM)

    def test_dust_is_a_collapsed_count_not_a_row(self):
        self.make(1, 50_000, "dust")
        self.make(2, 60_000, "dust")
        self.make(3, 500_000, "no_matching_invoice")
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("<details", page)
        self.assertIn("2 very small transfers", page)
        self.assertEqual(page.count("probably address-poisoning spam"), 1)  # the summary, not a row each
        self.assertIn(str(REVIEW_REASONS["no_matching_invoice"]), page)
