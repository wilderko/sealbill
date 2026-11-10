"""End to end on Tron: USDT (TRC-20), read from a fake TronGrid (`e2e_helpers.TronGrid`)
through `apps.rails.http`. TronGrid lists only confirmed transfers when asked to, so a Tron
payment is final the moment it is seen."""
from apps.payments.e2e_helpers import Clock, E2EBase, TronGrid, tron_address
from apps.payments.models import Declaration, Payment
from apps.quotes.models import Quote
from apps.rails.models import RailAccount, WatchCursor
from apps.rails.networks import NETWORKS
from apps.webhooks.models import WebhookEvent

MINE = tron_address("mine")
PAYER = tron_address("payer")
STRANGER = tron_address("stranger")
USDT = NETWORKS["tron"].assets["USDT"].contract
FAKE_USDT = tron_address("a token calling itself USDT")
K0 = 108_270_000  # 100.00 EUR at 0.9237 EUR per USDT


class TronBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.grid = TronGrid(self.internet)
        self.rail = RailAccount.objects.create(org=self.org, kind="tron", label="Tron wallet", address=MINE,
                                               networks=["tron"], assets=["USDT"])
        self.method = self.rail.method_key
        self.work("chain_scan")  # the first pass marks where the stream starts

    def scan(self):
        self.work("chain_scan", "finality_refresh")

    def sent(self, **kw):
        return self.invoice([self.method], **kw)

    def pay(self, amount, **kw):
        kw.setdefault("sender", PAYER)
        tx = self.grid.send(kw.pop("contract", USDT), kw.pop("to", MINE), amount, **kw)
        self.scan()
        return tx


class TronLifecycleTests(TronBase):
    def test_tron_usdt_whole_lifecycle(self):
        pk = self.create_invoice([self.method])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        self.assertIn("USDT on Tron (TRC-20)", self.pay_page(pay))
        quote = self.quote(pay, self.method, "tron", "USDT")
        self.assertEqual((quote.address, quote.amount_base_units, quote.payment_uri), (MINE, str(K0), ""))
        tx = self.pay(K0)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.tx_hash, payment.log_index, payment.final, payment.from_address),
                         (tx, 0, True, PAYER))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.27")

    def test_an_unconfirmed_transfer_is_seen_once_tron_confirms_it(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        tx = self.pay(K0, confirmed=False)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("Awaiting payment", self.status_text(pay))
        self.grid.confirm(tx)
        self.scan()
        (payment,) = self.payments(pk)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_payment_made_before_the_first_scan_of_a_new_wallet_is_found(self):
        second = RailAccount.objects.create(org=self.org, kind="tron", label="new wallet", address=STRANGER,
                                            networks=["tron"], assets=["USDT"])
        pk, pay = self.invoice([second.method_key])
        quote = self.quote(pay, second.method_key, "tron", "USDT")
        self.grid.send(USDT, STRANGER, int(quote.amount_base_units), sender=PAYER)
        self.scan()  # the first look at this wallet only marks the start, and reads ten minutes back
        self.scan()
        (payment,) = self.payments(pk)
        self.assertEqual(payment.state, "confirmed")


class TronWrongMoneyTests(TronBase):
    def test_underpayment_goes_to_review_and_a_declaration_with_0x_attributes_it(self):
        pk, pay = self.sent()
        quote = self.quote(pay, self.method, "tron", "USDT")
        tx = self.pay(80_000_000)
        payment = self.assertInReview(Payment.objects.get(tx_hash=tx), "no_matching_invoice")
        self.assertNotSettled(pk, pay)
        self.declare(pay, "tron", "0x" + tx.upper())  # a wallet's spelling of the same id
        payment = self.assertInReview(payment, "no_matching_invoice", invoice_pk=pk, pay_path=pay)
        self.assertEqual((payment.quote_id, payment.amount_minor), (quote.pk, 80_000_000 * 10000 // K0))
        self.assertNotSettled(pk, status="declared")

    def test_overpayment_within_the_ceiling_settles_with_a_refund_flag(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        self.pay(K0 + 3_000_000)
        (payment,) = self.payments(pk)
        self.assertEqual(payment.refund_due_base_units, "3000000")
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_token_that_is_not_tethers_usdt_is_never_counted(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        tx = self.pay(K0, contract=FAKE_USDT)
        self.declare(pay, "tron", tx)
        self.work("declarations_verify")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_transfer_to_another_address_is_not_ours(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        tx = self.pay(K0, to=STRANGER)
        self.declare(pay, "tron", tx)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_the_same_transfer_read_by_overlapping_scans_and_declared_settles_once(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        tx = self.pay(K0)
        self.scan()  # the ten-minute overlap lists it again
        self.work("declarations_verify")
        self.assertEqual(Payment.objects.filter(tx_hash=tx).count(), 1)
        self.assertPaidEverywhere(pk, Payment.objects.get(tx_hash=tx), pay)
        self.assertEqual(WebhookEvent.objects.filter(type="payment.confirmed").count(), 1)

    def test_two_invoices_on_one_address_each_get_their_own_transfer(self):
        a, pay_a = self.sent()
        b, pay_b = self.sent()
        qa = self.quote(pay_a, self.method, "tron", "USDT")
        qb = self.quote(pay_b, self.method, "tron", "USDT")
        self.assertNotEqual(qa.amount_base_units, qb.amount_base_units)
        self.pay(int(qb.amount_base_units))
        self.assertEqual(self.inv(b).status, "paid")
        self.assertNotSettled(a, pay_a)
        self.pay(int(qa.amount_base_units))
        self.assertEqual(self.inv(a).status, "paid")
        self.assertEqual(self.payments(a)[0].quote_id, qa.pk)


class TronTimeAndOutageTests(TronBase):
    def test_a_transfer_after_the_quote_expired_waits_for_staff(self):
        clock = Clock(self)
        pk, pay = self.sent()
        quote = self.quote(pay, self.method, "tron", "USDT")
        clock.advance(1900)
        self.work("quotes_expire")
        self.pay(K0)
        (payment,) = self.payments(pk)
        self.assertInReview(payment, "after_expiry", invoice_pk=pk, pay_path=pay)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "expired")
        self.assertNotSettled(pk)

    def test_trongrid_failing_changes_nothing_and_the_transfer_is_found_later(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        cursor = WatchCursor.objects.get(stream=f"tron-trc20:{self.rail.pk}").position
        self.grid.send(USDT, MINE, K0, sender=PAYER)
        name = f"scan:tron:tron:{self.rail.pk}"
        for how in ("429", "timeout", "garbage", "401"):
            with self.subTest(how=how):
                self.internet.down(self.grid.base, how)
                with self.assertLogs("apps.rails.http", "WARNING"):
                    self.scan()
                self.assertEqual(Payment.objects.count(), 0)
                self.assertEqual(WatchCursor.objects.get(stream=f"tron-trc20:{self.rail.pk}").position, cursor)
                self.assertIsNotNone(self.health(name).failing_since)
                self.assertNotSettled(pk, pay)
        self.internet.up(self.grid.base)
        self.scan()
        self.assertIsNone(self.health(name).failing_since)
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_a_declaration_trongrid_cannot_look_up_stays_open_and_is_tried_again(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "tron", "USDT")
        tx = self.grid.send(USDT, MINE, K0, sender=PAYER)
        self.internet.down(self.grid.base, "503")
        with self.assertLogs("apps.rails.http", "WARNING"):
            self.declare(pay, "tron", tx)  # the in-request look fails quietly
        with self.assertLogs("apps.rails.http", "WARNING"):
            self.work("declarations_verify")
        declaration = Declaration.objects.get(invoice_id=pk)
        self.assertEqual((declaration.state, declaration.verify_attempts), ("open", 2))
        self.assertIsNotNone(self.health("declarations:tron").failing_since)
        self.assertNotSettled(pk, status="declared")
        self.internet.up(self.grid.base)
        self.work("declarations_verify")
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)


class TronNewWalletTests(TronBase):
    def test_a_payment_to_a_new_wallet_is_found_when_the_worker_first_looks_a_quarter_hour_later(self):
        clock = Clock(self)
        wallet = tron_address("new wallet")
        rail = RailAccount.objects.create(org=self.org, kind="tron", label="new", address=wallet,
                                          networks=["tron"], assets=["USDT"])
        pk, pay = self.invoice([rail.method_key])
        quote = self.quote(pay, rail.method_key, "tron", "USDT")
        self.grid.send(USDT, wallet, int(quote.amount_base_units), sender=PAYER)
        clock.advance(15 * 60)  # the worker was restarting when the wallet was added
        self.scan()  # its first look at this wallet
        self.scan()
        self.assertEqual(len(self.payments(pk)), 1, "the client's USDT was never seen")
