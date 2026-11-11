"""End to end on the EVM rails: native ETH and allow-listed stablecoins, on Ethereum (L1),
Arbitrum and Base (L2) and BNB Smart Chain (18-decimal stablecoins).

Each test creates and sends an invoice through the API, opens the pay page through the link in
the client's letter (or the API's `pay_url`), takes a quote there, puts the transfer on a fake
JSON-RPC node (`e2e_helpers.EvmNode`, reached through `apps.rails.http`), and lets the worker's
own jobs find and settle it.

The matching rules themselves are pinned case by case in `apps/payments/tests_matching.py`
against a fake adapter; these tests check that the whole path, watcher included, delivers them.
"""
from decimal import Decimal

from django.core import mail

from apps.notify.models import OutboxMessage
from apps.payments.e2e_helpers import CLIENT, STAFF, Clock, E2EBase, evm_address
from apps.payments.models import Declaration, Payment
from apps.quotes.models import Quote
from apps.rails.models import RailAccount, WatchCursor
from apps.rails.networks import NETWORKS
from apps.webhooks.models import WebhookEvent

MINE = evm_address("mine")
PAYER = evm_address("payer")
STRANGER = evm_address("stranger")
USDC_ARB = NETWORKS["arbitrum"].assets["USDC"].contract
USDT_ARB = NETWORKS["arbitrum"].assets["USDT"].contract
USDC_BASE = NETWORKS["base"].assets["USDC"].contract
USDT_ETH = NETWORKS["ethereum"].assets["USDT"].contract
USDT_BSC = NETWORKS["bsc"].assets["USDT"].contract
BRIDGED_USDC_E = "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"  # Arbitrum's bridged USDC.e: not allow-listed
K0 = 108_270_000  # 100.00 EUR at 0.9237 EUR per USDC, rounded up to the cent
ALLOWANCE = 541_350  # 0.5 % of K0, under the 1 USD cap
ETH_K0 = 33_334_000_000_000_000  # 100.00 EUR at 3000 EUR per ETH, rounded up to 10**12 wei


class EvmBase(E2EBase):
    networks = ("ethereum", "arbitrum", "base", "bsc")
    assets = ("ETH", "USDC", "USDT")

    def setUp(self):
        super().setUp()
        self.nodes = {n: self.evm_node(n) for n in self.networks}
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM wallet", address=MINE,
                                               networks=list(self.networks), assets=list(self.assets))
        self.method = self.rail.method_key
        self.work("chain_scan")  # the worker's first pass marks where each stream starts

    def scan(self):
        self.work("chain_scan", "finality_refresh")

    def sent_invoice(self, **kw):
        return self.invoice([self.method], **kw)


class EvmLifecycleTests(EvmBase):
    def test_arbitrum_usdc_whole_lifecycle_through_the_worker(self):
        pk = self.create_invoice([self.method, f"bank:{self.bank.pk}"])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        page = self.pay_page(pay)
        self.assertIn("USDC on Arbitrum One", page)
        self.assertIn("Awaiting payment", self.status_text(pay))
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        self.assertEqual((quote.address, quote.amount_base_units), (MINE, str(K0)))
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDC_ARB, MINE, K0, sender=PAYER)
        node.finalize()
        out = self.run_worker_once()
        self.assertIn("chain_scan", out)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.tx_hash, payment.log_index, payment.network, payment.asset, payment.final),
                         (tx, 0, "arbitrum", "USDC", True))
        self.assertEqual((payment.amount_minor, payment.refund_due_base_units, payment.quote_id),
                         (10000, "", quote.pk))
        self.assertEqual((payment.received_amount, payment.from_address), (Decimal("108.27"), PAYER))
        self.assertIn("quote.paid", self.actions("quotes.quote", quote.pk))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.27")

    def test_ethereum_usdt_seen_then_final(self):
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "ethereum", "USDT")
        node = self.nodes["ethereum"]
        tx = node.send_token(USDT_ETH, MINE, int(quote.amount_base_units), sender=PAYER)  # not finalized yet
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.final, payment.quote_id), ("detected", False, quote.pk))
        self.assertEqual(self.inv(pk).status, "sent")
        self.assertIn("Payment seen, waiting for confirmation", self.status_text(pay))
        self.assertTrue(WebhookEvent.objects.filter(type="payment.detected", payment_id=payment.pk).exists())
        self.assertFalse(mail.outbox)
        self.scan()  # still not final: nothing changes, and the overlap re-read records nothing twice
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().state, "detected")
        node.finalize()
        self.scan()
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.final, payment.tx_hash), ("confirmed", True, tx))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.27")

    def test_base_native_eth_lifecycle(self):
        pk, pay = self.sent_invoice()
        self.assertIn("ETH on Base", self.pay_page(pay))
        quote = self.quote(pay, self.method, "base", "ETH")
        self.assertEqual(quote.amount_base_units, str(ETH_K0))
        self.assertIn(f"ethereum:{MINE}@8453?value={ETH_K0}", quote.payment_uri)
        node = self.nodes["base"]
        tx = node.send_native(MINE, ETH_K0, sender=PAYER)
        node.finalize()
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.asset, payment.contract, payment.log_index, payment.tx_hash),
                         ("ETH", "", -1, tx))
        self.assertEqual(payment.received_amount, Decimal("0.033334"))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="0.033334")

    def test_base_usdc_small_amount_final_after_five_minutes_on_the_l2_fast_path(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "base", "USDC")
        self.nodes["base"].send_token(USDC_BASE, MINE, int(quote.amount_base_units), sender=PAYER,
                                      at=clock.unix())
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual(payment.state, "detected")  # younger than 300 s and not finalized
        clock.advance(301)
        self.scan()
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.final), ("confirmed", True))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_bsc_usdt_eighteen_decimals_lifecycle_with_the_quote_taken_through_the_api(self):
        pk, pay = self.sent_invoice()
        r = self.api_post(f"/api/v1/invoices/{pk}/quotes", {"method": self.method, "network": "bsc", "asset": "USDT"})
        self.assertEqual(r.status_code, 201, r.content)
        amount = 108_270_000_000_000_000_000  # 108.27 USDT at 18 decimals
        self.assertEqual(r.json()["amount_base_units"], str(amount))
        # The same quote is what the pay page hands out for that key.
        self.assertEqual(self.quote(pay, self.method, "bsc", "USDT").pk, r.json()["id"])
        node = self.nodes["bsc"]
        node.send_token(USDT_BSC, MINE, amount + 5 * 10**16, sender=PAYER)  # 0.05 USDT over: within the ceiling
        node.finalize()
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual((payment.amount_base_units, payment.refund_due_base_units),
                         (str(amount + 5 * 10**16), str(5 * 10**16)))
        self.assertEqual(payment.received_amount, Decimal("108.32"))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.32")


class EvmAmountTests(EvmBase):
    def pay_arbitrum(self, amount, *, quote=None, pay=None):
        tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, amount, sender=PAYER)
        self.nodes["arbitrum"].finalize()
        self.scan()
        return tx, Payment.objects.get(network="arbitrum", tx_hash=tx)

    def test_shortfall_within_the_allowance_settles_in_full(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        _tx, payment = self.pay_arbitrum(K0 - ALLOWANCE)
        self.assertEqual((payment.amount_minor, payment.refund_due_base_units), (10000, ""))
        self.assertPaidEverywhere(pk, payment, pay)

    def underpay_declare_and_confirm(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        tx, payment = self.pay_arbitrum(50_000_000)
        self.assertInReview(payment, "no_matching_invoice")  # no window holds it: nobody's yet
        self.assertNotSettled(pk, pay)
        self.assertIn("Awaiting payment", self.status_text(pay))
        # The client says it was theirs: attributed, credit worked out, still for a person.
        self.declare(pay, "arbitrum", tx)
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.invoice_id, payment.amount_minor), ("needs_review", pk, 4618))
        self.assertIn("payment.reattribute", self.actions("payments.payment", payment.pk))
        self.assertIn("Payment received, our staff are checking it", self.status_text(pay))
        self.assertNotSettled(pk, status="declared")
        staff = self.staff_client()
        r = staff.post(f"/payments/{payment.pk}/confirm/", {"note": "checked the wallet"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.inv(pk).status, "partially_paid")
        self.assertEqual(OutboxMessage.objects.filter(kind="partial", invoice_id=pk, recipient="contact").count(), 1)
        return pk, pay, payment

    def test_underpayment_is_nobodys_until_declared_then_staff_confirm_a_partial_and_the_rest_is_paid(self):
        pk, pay, _payment = self.underpay_declare_and_confirm()
        # The rest, on a fresh quote for what is still owed (53.82 EUR).
        rest = self.quote(pay, self.method, "arbitrum", "USDC")
        self.assertEqual((rest.invoice_amount_minor, rest.amount_base_units), (5382, "58270000"))
        _tx2, second = self.pay_arbitrum(58_270_000)
        self.assertEqual((second.state, second.amount_minor), ("confirmed", 5382))
        self.assertPaidEverywhere(pk, second, pay)

    def test_the_declaration_staff_confirmed_is_borne_out_and_the_client_is_asked_for_the_rest(self):
        pk, pay, payment = self.underpay_declare_and_confirm()
        # The client's claim named exactly the transaction staff have just confirmed.
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.assertIn("Partially paid. Please pay the remaining amount.", self.status_text(pay))

    def test_overpayment_within_the_ceiling_settles_and_flags_the_refund_for_staff(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        over = K0 * 5 // 100
        _tx, payment = self.pay_arbitrum(K0 + over)
        self.assertEqual((payment.state, payment.amount_minor, payment.refund_due_base_units),
                         ("confirmed", 10000, str(over)))
        self.assertPaidEverywhere(pk, payment, pay)  # credited what the quote settles: paid, not overpaid
        staff = self.staff_client()
        self.assertIn(b"refund", staff.get("/").content.lower())
        r = staff.post(f"/payments/{payment.pk}/refund/", {"note": "sent 5.41 USDC back"})
        self.assertEqual(r.status_code, 302)
        payment.refresh_from_db()
        self.assertIsNotNone(payment.refund_acknowledged_at)
        self.assertIn("payment.refund_ack", self.actions("payments.payment", payment.pk))

    def test_overpayment_beyond_the_ceiling_waits_for_staff(self):
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        amount = K0 + K0 * 15 // 100
        tx, payment = self.pay_arbitrum(amount)
        self.assertInReview(payment, "no_matching_invoice")
        self.declare(pay, "arbitrum", tx)
        payment = self.assertInReview(payment, "overpaid_beyond_ceiling", invoice_pk=pk, pay_path=pay)
        self.assertEqual((payment.quote_id, payment.amount_minor, payment.refund_due_base_units),
                         (quote.pk, 10000, str(amount - K0)))
        self.assertNotSettled(pk, status="declared")
        staff = self.staff_client()
        self.assertEqual(staff.post(f"/payments/{payment.pk}/confirm/", {"note": "ok"}).status_code, 302)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_two_invoices_with_open_quotes_on_one_address_each_get_their_own_money(self):
        first, pay1 = self.sent_invoice()
        second, pay2 = self.sent_invoice()
        q1 = self.quote(pay1, self.method, "arbitrum", "USDC")
        q2 = self.quote(pay2, self.method, "arbitrum", "USDC")
        self.assertEqual((q1.address, q2.address), (MINE, MINE))
        self.assertEqual((int(q1.amount_base_units), int(q2.amount_base_units)), (K0, K0 + 10_000))
        _tx, p2 = self.pay_arbitrum(K0 + 10_000)  # the second client pays first
        self.assertEqual((p2.invoice_id, p2.quote_id, p2.state), (second, q2.pk, "confirmed"))
        self.assertNotSettled(first, pay1)
        self.assertEqual(Quote.objects.get(pk=q1.pk).state, "open")
        _tx, p1 = self.pay_arbitrum(K0)
        self.assertEqual((p1.invoice_id, p1.quote_id), (first, q1.pk))
        self.assertPaidEverywhere(first, p1, pay1)
        self.assertPaidEverywhere(second, p2, pay2)

    def test_one_batch_transaction_paying_two_invoices_settles_both(self):
        first, pay1 = self.sent_invoice()
        second, pay2 = self.sent_invoice()
        q1 = self.quote(pay1, self.method, "arbitrum", "USDC")
        q2 = self.quote(pay2, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_batch(USDC_ARB, [(MINE, int(q2.amount_base_units)), (STRANGER, 5_000_000),
                                        (MINE, int(q1.amount_base_units))], sender=PAYER)
        node.finalize()
        self.scan()
        p1, p2 = self.payments(first)[0], self.payments(second)[0]
        self.assertEqual((p1.tx_hash, p1.log_index, p2.tx_hash, p2.log_index), (tx, 2, tx, 0))
        self.assertPaidEverywhere(first, p1, pay1)
        self.assertPaidEverywhere(second, p2, pay2)

    def test_a_second_payment_of_a_settled_quote_is_a_duplicate_for_staff(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        _tx, first = self.pay_arbitrum(K0)
        self.assertEqual(first.state, "confirmed")
        _tx2, second = self.pay_arbitrum(K0)  # the client pressed send twice
        self.assertInReview(second, "duplicate", invoice_pk=pk)
        self.assertEqual(self.inv(pk).status, "paid")
        self.assertIn("Paid. Thank you.", self.status_text(pay))


class EvmWrongMoneyTests(EvmBase):
    """Money that must never settle the invoice, whatever its amount says."""

    def test_a_token_not_on_the_allow_list_is_never_seen_even_when_declared(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_token(BRIDGED_USDC_E, MINE, K0, sender=PAYER)  # same event, same amount, other contract
        node.finalize()
        self.scan()
        self.assertEqual(Payment.objects.count(), 0)
        self.declare(pay, "arbitrum", tx)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "open")
        self.assertNotSettled(pk, pay, status="declared")
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
        # Nothing of ours in it: staff are e-mailed once the half hour to find it is over.
        self.assertFalse(OutboxMessage.objects.filter(kind="declared_staff", invoice_id=pk).exists())
        clock.advance(31 * 60)
        self.work("declarations_verify")
        self.assertTrue(OutboxMessage.objects.filter(kind="declared_staff", invoice_id=pk).exists())

    def test_another_allow_listed_token_of_the_same_amount_goes_to_review(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDT_ARB, MINE, K0, sender=PAYER)  # USDT where USDC was asked for
        node.finalize()
        self.scan()
        payment = Payment.objects.get(tx_hash=tx)
        self.assertEqual(payment.asset, "USDT")
        self.assertInReview(payment, "no_matching_invoice")
        self.assertNotSettled(pk, pay)

    def test_the_right_token_on_the_wrong_network_goes_to_review(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["base"]
        tx = node.send_token(USDC_BASE, MINE, K0, sender=PAYER)  # Base, not Arbitrum
        node.finalize()
        self.scan()
        payment = Payment.objects.get(tx_hash=tx)
        self.assertEqual(payment.network, "base")
        self.assertInReview(payment, "no_matching_invoice")
        self.assertNotSettled(pk, pay)

    def test_a_transfer_to_another_address_is_not_ours_even_when_declared(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDC_ARB, STRANGER, K0, sender=PAYER)
        node.finalize()
        self.scan()
        self.declare(pay, "arbitrum", tx)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_reverted_transfer_is_nothing_even_when_declared(self):
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDC_ARB, MINE, K0, sender=PAYER, ok=False)
        eth_tx = node.send_native(MINE, ETH_K0, sender=PAYER, ok=False)
        node.finalize()
        self.scan()
        self.declare(pay, "arbitrum", tx)
        self.declare(pay, "arbitrum", eth_tx)
        self.work("declarations_verify")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")

    def test_the_same_transfer_declared_and_seen_by_two_scans_settles_once(self):
        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDC_ARB, MINE, K0, sender=PAYER)
        node.finalize()
        self.declare(pay, "arbitrum", tx)  # the client names it before the watcher has looked
        (payment,) = self.payments()
        self.assertEqual(payment.state, "confirmed")
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.scan()
        self.scan()  # the watcher reads it, and the overlap reads it again
        self.work("declarations_verify")
        self.assertEqual(self.payments(), [payment])
        self.assertPaidEverywhere(pk, payment, pay)
        self.assertEqual(WebhookEvent.objects.filter(type="payment.confirmed").count(), 1)


class EvmTimeTests(EvmBase):
    def test_a_transfer_after_the_quote_expired_is_attributed_for_staff(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        clock.advance(1801)
        self.work("quotes_expire")
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "expired")
        node = self.nodes["arbitrum"]
        node.send_token(USDC_ARB, MINE, K0, sender=PAYER, at=clock.unix())
        node.finalize()
        self.scan()
        (payment,) = self.payments(pk)
        self.assertInReview(payment, "after_expiry", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)
        staff = self.staff_client()
        self.assertEqual(staff.post(f"/payments/{payment.pk}/confirm/", {"note": "late but fine"}).status_code,
                         302)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_transfer_made_in_time_and_final_after_expiry_still_settles(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "ethereum", "USDT")
        node = self.nodes["ethereum"]
        node.send_token(USDT_ETH, MINE, int(quote.amount_base_units), sender=PAYER, at=clock.unix())
        self.scan()
        clock.advance(2400)  # the quote expires while the chain finalizes
        self.work("quotes_expire")
        node.finalize()
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual(payment.state, "confirmed")
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_transfer_that_vanishes_before_it_is_final_is_rejected_after_two_looks(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "ethereum", "USDT")
        node = self.nodes["ethereum"]
        tx = node.send_token(USDT_ETH, MINE, int(quote.amount_base_units), sender=PAYER, at=clock.unix())
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual(payment.state, "detected")
        # A reorganisation drops it: no receipt, no log, and the block at its height no longer
        # holds it. The chain then finalizes past that height: only then is it gone for good.
        node.receipts.pop(tx)
        node.logs = [log for log in node.logs if log["transactionHash"] != tx]
        node.blocks[payment.block_number]["transactions"] = []
        node.mine(at=clock.unix())
        node.finalize()
        self.work("finality_refresh")
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.check_attempts), ("detected", 1))  # one look is not enough
        clock.advance(601)
        with self.assertLogs("apps.payments.matching", "WARNING"):
            self.work("finality_refresh")
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.review_reason), ("rejected", "dropped"))
        self.assertTrue(WebhookEvent.objects.filter(type="payment.rejected", payment_id=payment.pk).exists())
        self.assertTrue(self.staff_letters("review_staff"))
        self.assertNotSettled(pk, pay)
        self.assertIn("Awaiting payment", self.status_text(pay))


class EvmOutageTests(EvmBase):
    """A node that cannot answer is "could not look", never "not paid"."""

    def test_a_dead_node_changes_nothing_and_the_payment_is_found_once_it_answers(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        node = self.nodes["arbitrum"]
        node.send_token(USDC_ARB, MINE, K0, sender=PAYER, at=clock.unix())
        node.finalize()
        cursor = WatchCursor.objects.get(network="arbitrum", stream=f"evm-logs:{self.rail.pk}").position
        name = f"scan:evm:arbitrum:{self.rail.pk}"
        for how in ("timeout", "connection", "503", "429", "garbage"):
            with self.subTest(how=how):
                self.internet.down(node.url, how)
                with self.assertLogs("apps.rails.http", "WARNING"):
                    self.scan()
                self.assertEqual(Payment.objects.count(), 0)
                self.assertEqual(WatchCursor.objects.get(network="arbitrum",
                                                         stream=f"evm-logs:{self.rail.pk}").position, cursor)
                self.assertIsNotNone(self.health(name).failing_since)
                self.assertNotSettled(pk, pay)
                self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
        # Ten minutes of that: staff hear of it once, by e-mail and as a staff.alert event.
        clock.advance(601)
        with self.assertLogs("apps.rails.http", "WARNING"):
            self.work("chain_scan", "watcher_alert", "watcher_alert")
        alerts = [m for m in mail.outbox if m.to == [STAFF] and "watcher failing" in m.subject]
        self.assertEqual(len(alerts), 1)
        self.assertIn(name, alerts[0].body)
        self.assertEqual(WebhookEvent.objects.filter(type="staff.alert").count(), 1)
        self.internet.up(node.url)
        self.scan()
        (payment,) = self.payments(pk)
        self.assertIsNone(self.health(name).failing_since)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_dead_node_never_rejects_a_payment_waiting_for_finality(self):
        clock = Clock(self)
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "ethereum", "USDT")
        node = self.nodes["ethereum"]
        node.send_token(USDT_ETH, MINE, int(quote.amount_base_units), sender=PAYER, at=clock.unix())
        self.scan()
        (payment,) = self.payments(pk)
        self.internet.down(node.url, "503")
        for _ in range(3):
            clock.advance(700)
            with self.assertLogs("apps.rails.http", "WARNING"):
                self.scan()
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.check_attempts), ("detected", 0))
        self.assertIsNotNone(self.health("finality:ethereum").failing_since)
        self.internet.up(node.url)
        node.finalize()
        self.scan()
        payment.refresh_from_db()
        self.assertEqual(payment.state, "confirmed")
        self.assertIsNone(self.health("finality:ethereum").failing_since)

    def test_quote_refused_when_the_exchange_rates_cannot_be_had(self):
        pk, pay = self.sent_invoice()
        self.internet.down("https://api.coinbase.com", "timeout")
        with self.assertLogs("apps.rails.http", "WARNING"), self.assertLogs("apps.quotes.services", "WARNING"):
            r = self.client.post(pay + "quote/", {"method": self.method, "network": "arbitrum", "asset": "USDC"},
                                 HTTP_HX_REQUEST="true")
        self.assertContains(r, "Exchange rates are unavailable right now")
        self.assertFalse(Quote.objects.exists())
        self.assertIsNotNone(self.health("fx").failing_since)
        self.internet.up("https://api.coinbase.com")
        self.quote(pay, self.method, "arbitrum", "USDC")
        self.assertIsNone(self.health("fx").failing_since)


class EvmSealedVaultTests(EvmBase):
    def test_a_payment_arriving_while_the_vault_is_sealed_settles_and_the_letters_wait_for_the_unlock(self):
        from apps.notify.models import OutboxMessage as Outbox
        from apps.vault.testing import TEST_VAULT_PASSWORD
        from apps.vault.vault import vault
        from apps.webhooks.models import WebhookDelivery

        pk, pay = self.sent_invoice()
        self.quote(pay, self.method, "arbitrum", "USDC")
        self.deliver_webhooks()
        vault.seal()  # a restart: the data key is gone until someone unlocks
        node = self.nodes["arbitrum"]
        node.send_token(USDC_ARB, MINE, K0, sender=PAYER)
        node.finalize()
        with self.assertLogs("apps.core.jobs", "WARNING"):
            self.run_worker_once()
        (payment,) = self.payments(pk)
        from apps.invoices.models import Invoice

        status = Invoice.objects.filter(pk=pk).values_list("status", flat=True).get()  # plaintext only: sealed
        self.assertEqual((payment.state, status), ("confirmed", "paid"))
        self.assertEqual(Outbox.objects.get(kind="receipt", invoice_id=pk).state, "queued")
        self.assertTrue(WebhookDelivery.objects.filter(state="queued", event__type="invoice.paid").exists())
        self.assertFalse([m for m in mail.outbox if m.to == [CLIENT]])
        self.assertTrue([m for m in mail.outbox if m.to == [STAFF] and "sealed" in m.subject])
        r = self.client.get(pay)
        self.assertEqual(r.status_code, 503)  # the client page says it is unavailable, never the unlock form
        vault.unseal(TEST_VAULT_PASSWORD)
        self.run_worker_once()
        self.assertPaidEverywhere(pk, payment, pay)


class EvmNewWalletTests(EvmBase):
    """A wallet added while invoices go out: the client may pay before the worker has looked at it."""

    def new_wallet(self, assets):
        wallet = evm_address("new wallet")
        rail = RailAccount.objects.create(org=self.org, kind="evm", label="new", address=wallet,
                                          networks=["base"], assets=list(assets))
        pk, pay = self.invoice([rail.method_key])
        return wallet, rail, pk, pay

    def test_a_token_payment_made_before_the_first_scan_of_a_new_wallet_is_found(self):
        wallet, rail, pk, pay = self.new_wallet(["USDC"])
        quote = self.quote(pay, rail.method_key, "base", "USDC")
        self.nodes["base"].send_token(USDC_BASE, wallet, int(quote.amount_base_units), sender=PAYER)
        self.nodes["base"].finalize()
        self.scan()  # the first look at this wallet: it reads ten minutes back
        self.scan()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_a_native_payment_made_before_the_first_scan_of_a_new_wallet_is_found(self):
        wallet, rail, pk, pay = self.new_wallet(["ETH"])
        quote = self.quote(pay, rail.method_key, "base", "ETH")
        self.nodes["base"].send_native(wallet, int(quote.amount_base_units), sender=PAYER)
        self.nodes["base"].finalize()
        self.scan()  # the first look at this wallet
        self.scan()
        self.assertEqual(len(self.payments(pk)), 1, "the client's ETH was never seen")
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)


class EvmSettledElsewhereTests(EvmBase):
    def test_a_transfer_for_an_invoice_already_paid_another_way_is_reviewed_as_closed_like_lightning(self):
        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        staff = self.staff_client()
        r = staff.post(f"/invoices/{pk}/payments/", {"amount": "100.00", "rail": "bank", "note": "SEPA"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "cancelled")  # nothing more is owed
        # The client also sends the amount the pay page showed them, well inside its validity.
        node = self.nodes["arbitrum"]
        node.send_token(USDC_ARB, MINE, K0, sender=PAYER)
        node.finalize()
        self.scan()
        payment = Payment.objects.get(rail="evm")
        self.assertEqual(self.inv(pk).status, "paid")
        # A refund is due because the invoice was already settled, not because the client was late.
        self.assertInReview(payment, "invoice_closed", invoice_pk=pk)


class EvmRetiredWalletTests(EvmBase):
    def test_a_quote_still_on_screen_is_watched_after_its_wallet_is_switched_off_in_the_admin(self):
        import json

        pk, pay = self.sent_invoice()
        quote = self.quote(pay, self.method, "arbitrum", "USDC")
        admin = self.staff_client(superuser=True)
        r = admin.post(f"/hidden-admin-xyz/rails/railaccount/{self.rail.pk}/change/", {
            "org": self.org.pk, "kind": "evm", "label": "EVM wallet (retired)", "address": MINE,
            "networks": json.dumps(list(self.networks)), "assets": json.dumps(list(self.assets)), "config": "{}",
            "_save": "Save"})  # "active" left unticked: the wallet is being rotated out
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertFalse(RailAccount.objects.get(pk=self.rail.pk).active)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")  # the client was told to pay it
        node = self.nodes["arbitrum"]
        tx = node.send_token(USDC_ARB, MINE, K0, sender=PAYER)  # within the quote's validity
        node.finalize()
        # The client says so at once, before the worker's next pass (once the payment is seen and
        # settles the invoice, "I have paid" is refused, 409): the pay page no longer offers
        # Arbitrum to name it on.
        self.declare(pay, "other", tx)
        self.scan()
        self.work("declarations_verify")
        self.assertEqual(len(self.payments()), 1, "the client paid the open quote and nobody saw it")
