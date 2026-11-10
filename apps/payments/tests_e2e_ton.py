"""End to end on TON: Tether's USDT jetton, read from a fake toncenter v3
(`e2e_helpers.TonCenter`) through `apps.rails.http`. TON is off by default (`TON_ENABLED`); these
tests turn it on. A transfer younger than 30 s is not final yet; the payer's comment may carry
the invoice's payment reference."""
import re

from django.test import override_settings

from apps.invoices.models import Invoice
from apps.invoices.reference import payment_reference
from apps.payments.e2e_helpers import Clock, E2EBase, TonCenter, ton_address
from apps.payments.models import Declaration, Payment
from apps.rails.models import RailAccount
from apps.rails.networks import NETWORKS

MINE = ton_address("mine")
PAYER = ton_address("payer")
STRANGER = ton_address("stranger")
USDT = NETWORKS["ton"].assets["USDT"].contract
FAKE_MASTER = ton_address("a jetton calling itself USDT")
K0 = 108_270_000


class TonBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(TON_ENABLED=True))
        self.clock = Clock(self)
        self.center = TonCenter(self.internet)
        self.rail = RailAccount.objects.create(org=self.org, kind="ton", label="TON wallet", address=MINE,
                                               networks=["ton"], assets=["USDT"])
        self.method = self.rail.method_key
        self.work("chain_scan")

    def scan(self):
        self.work("chain_scan", "finality_refresh")

    def sent(self, **kw):
        return self.invoice([self.method], **kw)

    def pay(self, amount, *, master=USDT, to=MINE, age=60, **kw):
        tx = self.center.send(master, to, amount, sender=PAYER, at=self.clock.unix() - age, **kw)
        self.scan()
        return tx


class TonLifecycleTests(TonBase):
    def test_ton_usdt_whole_lifecycle(self):
        pk = self.create_invoice([self.method])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        self.assertIn("USDT on TON", self.pay_page(pay))
        quote = self.quote(pay, self.method, "ton", "USDT")
        reference = payment_reference(Invoice.objects.get(pk=pk))
        self.assertTrue(quote.payment_uri.startswith("ton://transfer/"))
        self.assertIn(f"&amount={K0}&text={reference}", quote.payment_uri)
        tx = self.pay(K0)
        (payment,) = self.payments(pk)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", payment.tx_hash))
        self.assertEqual((payment.tx_hash, payment.final), (tx, True))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.27")

    def test_a_young_transfer_is_seen_then_final_thirty_seconds_later(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        self.pay(K0, age=0)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.final), ("detected", False))
        self.assertIn("Payment seen, waiting for confirmation", self.status_text(pay))
        self.clock.advance(31)
        self.scan()
        payment.refresh_from_db()
        self.assertEqual((payment.state, payment.final), ("confirmed", True))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_the_payers_comment_settles_a_short_payment_as_partial(self):
        pk, pay = self.sent()
        quote = self.quote(pay, self.method, "ton", "USDT")
        reference = payment_reference(Invoice.objects.get(pk=pk))
        grouped = " ".join(reference[i:i + 4] for i in range(0, len(reference), 4))  # as a bank prints it
        self.pay(54_135_000, comment=grouped)  # half, with the invoice's reference as the comment
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.quote_id, payment.amount_minor), ("confirmed", quote.pk, 5000))
        self.assertEqual(self.inv(pk).status, "partially_paid")
        self.assertIn("Partially paid. Please pay the remaining amount.", self.status_text(pay))


class TonWrongMoneyTests(TonBase):
    def test_an_aborted_transfer_is_nothing_even_when_declared(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        tx = self.pay(K0, aborted=True)
        self.declare(pay, "ton", tx)
        self.work("declarations_verify")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_jetton_that_is_not_tethers_is_never_counted(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        tx = self.pay(K0, master=FAKE_MASTER)
        self.declare(pay, "ton", tx)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_transfer_to_another_wallet_is_not_ours(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        self.pay(K0, to=STRANGER)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay)

    def test_overpayment_within_the_ceiling_settles_with_a_refund_flag(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        # Paid once the quote was on screen: `pay` dates the transfer a minute back so that it is
        # final, and a window (unlike an exact amount) is never won by a quote issued after the
        # money moved (audit finding MONEY-3).
        self.clock.advance(60)
        self.pay(K0 + 1_000_000)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.refund_due_base_units), ("confirmed", "1000000"))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_underpayment_without_a_comment_waits_for_staff(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        tx = self.pay(54_135_000)
        self.assertInReview(Payment.objects.get(tx_hash=tx), "no_matching_invoice")
        self.assertNotSettled(pk, pay)

    def test_the_same_transfer_declared_in_base64_and_read_twice_settles_once(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        tx = self.center.send(USDT, MINE, K0, sender=PAYER, at=self.clock.unix() - 60)
        self.declare(pay, "ton", self.center.rows[0]["transaction_hash"])  # as an explorer shows it
        (payment,) = self.payments(pk)
        self.assertEqual((payment.tx_hash, payment.state), (tx, "confirmed"))
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.scan()
        self.scan()
        self.assertEqual(Payment.objects.count(), 1)
        self.assertPaidEverywhere(pk, payment, pay)

    def test_two_invoices_on_one_wallet_each_get_their_own_transfer(self):
        a, pay_a = self.sent()
        b, pay_b = self.sent()
        qa = self.quote(pay_a, self.method, "ton", "USDT")
        qb = self.quote(pay_b, self.method, "ton", "USDT")
        self.pay(int(qb.amount_base_units))
        self.pay(int(qa.amount_base_units))
        self.assertEqual((self.payments(a)[0].quote_id, self.payments(b)[0].quote_id), (qa.pk, qb.pk))


class TonTimeAndOutageTests(TonBase):
    def test_a_transfer_after_the_quote_expired_waits_for_staff(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        self.clock.advance(1900)
        self.work("quotes_expire")
        self.pay(K0)
        self.assertInReview(self.payments(pk)[0], "after_expiry", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)

    def test_toncenter_failing_changes_nothing_and_the_transfer_is_found_later(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        self.center.send(USDT, MINE, K0, sender=PAYER, at=self.clock.unix() - 60)
        name = f"scan:ton:ton:{self.rail.pk}"
        for how in ("timeout", "429", "garbage"):
            with self.subTest(how=how):
                self.internet.down("https://toncenter.com", how)
                with self.assertLogs("apps.rails.http", "WARNING"):
                    self.scan()
                self.assertEqual(Payment.objects.count(), 0)
                self.assertIsNotNone(self.health(name).failing_since)
        self.internet.up("https://toncenter.com")
        self.scan()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)

    def test_a_young_transfer_is_never_rejected_while_toncenter_is_down(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "ton", "USDT")
        self.pay(K0, age=0)
        (payment,) = self.payments(pk)
        self.internet.down("https://toncenter.com", "503")
        for _ in range(3):
            self.clock.advance(700)
            with self.assertLogs("apps.rails.http", "WARNING"):
                self.work("finality_refresh")
        payment.refresh_from_db()
        self.assertEqual(payment.state, "detected")
        self.internet.up("https://toncenter.com")
        self.work("finality_refresh")
        payment.refresh_from_db()
        self.assertEqual(payment.state, "confirmed")


class TonNewWalletTests(TonBase):
    def test_a_payment_made_before_the_first_scan_of_a_new_wallet_is_found(self):
        wallet = ton_address("new wallet")
        rail = RailAccount.objects.create(org=self.org, kind="ton", label="new", address=wallet,
                                          networks=["ton"], assets=["USDT"])
        pk, pay = self.invoice([rail.method_key])
        quote = self.quote(pay, rail.method_key, "ton", "USDT")
        self.center.send(USDT, wallet, int(quote.amount_base_units), sender=PAYER, at=self.clock.unix() - 40)
        self.scan()  # the first look at this wallet reads ten minutes back
        self.scan()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)
