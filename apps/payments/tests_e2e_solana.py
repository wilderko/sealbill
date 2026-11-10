"""End to end on Solana: USDC and USDT (SPL tokens), read at `finalized` from a fake RPC node
(`e2e_helpers.SolanaRpc`) through `apps.rails.http`. A transfer is the balance change of a token
account the wallet owns; the sender may create that account with the payment itself."""
from apps.payments.e2e_helpers import Clock, E2EBase, SolanaRpc, solana_address
from apps.payments.models import Declaration, Payment
from apps.quotes.models import Quote
from apps.rails.models import RailAccount, WatchCursor
from apps.rails.networks import NETWORKS
from apps.webhooks.models import WebhookEvent

MINE = solana_address("mine")
PAYER = solana_address("payer")
STRANGER = solana_address("stranger")
USDC = NETWORKS["solana"].assets["USDC"].contract
USDT = NETWORKS["solana"].assets["USDT"].contract
FAKE_MINT = solana_address("a mint calling itself USDC")
K0 = 108_270_000


class SolanaBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.rpc = SolanaRpc(self.internet)
        self.rail = RailAccount.objects.create(org=self.org, kind="solana", label="Solana wallet", address=MINE,
                                               networks=["solana"], assets=["USDC", "USDT"])
        self.method = self.rail.method_key
        self.rpc.account_for(MINE, USDC)  # the wallet already holds a USDC account; no USDT one yet
        self.work("chain_scan")

    def scan(self):
        self.work("chain_scan", "finality_refresh")

    def sent(self, **kw):
        return self.invoice([self.method], **kw)

    def pay(self, amount, mint=USDC, **kw):
        kw.setdefault("sender", PAYER)
        sig = self.rpc.send(mint, kw.pop("to", MINE), amount, **kw)
        self.scan()
        return sig


class SolanaLifecycleTests(SolanaBase):
    def test_solana_usdc_whole_lifecycle(self):
        pk = self.create_invoice([self.method])
        self.send_invoice(pk)
        pay = self.letter_pay_path()
        self.assertIn("USDC on Solana", self.pay_page(pay))
        quote = self.quote(pay, self.method, "solana", "USDC")
        self.assertEqual(quote.payment_uri, f"solana:{MINE}?amount=108.27&spl-token={USDC}")
        sig = self.pay(K0)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.tx_hash, payment.log_index, payment.final, payment.from_address, payment.contract),
                         (sig, 2, True, PAYER, USDC))
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="108.27")

    def test_usdt_into_a_token_account_the_sender_creates_with_the_payment(self):
        pk, pay = self.sent()
        self.assertNotIn((MINE, USDT), self.rpc.token_accounts)
        quote = self.quote(pay, self.method, "solana", "USDT")
        self.pay(int(quote.amount_base_units), mint=USDT)
        (payment,) = self.payments(pk)
        self.assertEqual(payment.asset, "USDT")
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_transaction_listed_but_not_readable_yet_is_could_not_look(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.rpc.send(USDC, MINE, K0, sender=PAYER)
        tx = self.rpc.txs.pop(sig)  # finalized in the listing, null from getTransaction
        cursor = WatchCursor.objects.get(stream=f"sol-USDC:{self.rail.pk}").extra
        self.scan()
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(WatchCursor.objects.get(stream=f"sol-USDC:{self.rail.pk}").extra, cursor)
        self.assertEqual(self.health(f"scan:solana:solana:{self.rail.pk}").last_error_kind, "tx_missing")
        self.rpc.txs[sig] = tx
        self.scan()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)


class SolanaWrongMoneyTests(SolanaBase):
    def test_underpayment_waits_for_staff_and_the_declared_signature_attributes_it(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.pay(60_000_000)
        payment = self.assertInReview(Payment.objects.get(tx_hash=sig), "no_matching_invoice")
        self.assertNotSettled(pk, pay)
        self.declare(pay, "solana", sig)
        self.assertInReview(payment, "no_matching_invoice", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk, status="declared")

    def test_overpayment_within_the_ceiling_settles_with_a_refund_flag(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        self.pay(K0 + 2_500_000)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.state, payment.refund_due_base_units), ("confirmed", "2500000"))
        self.assertPaidEverywhere(pk, payment, pay)

    def test_a_failed_transaction_is_nothing_even_when_declared(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.pay(K0, err={"InstructionError": [2, {"Custom": 1}]})
        self.declare(pay, "solana", sig)
        self.work("declarations_verify")
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_mint_not_on_the_allow_list_is_never_counted(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.pay(K0, mint=FAKE_MINT)
        self.declare(pay, "solana", sig)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_a_transfer_to_another_wallet_is_not_ours(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.pay(K0, to=STRANGER)
        self.declare(pay, "solana", sig)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertNotSettled(pk, pay, status="declared")

    def test_the_other_stablecoin_of_the_same_amount_goes_to_review(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.pay(K0, mint=USDT)
        self.assertInReview(Payment.objects.get(tx_hash=sig), "no_matching_invoice")
        self.assertNotSettled(pk, pay)

    def test_the_same_signature_seen_twice_and_declared_settles_once(self):
        pk, pay = self.sent()
        self.quote(pay, self.method, "solana", "USDC")
        sig = self.rpc.send(USDC, MINE, K0, sender=PAYER)
        self.declare(pay, "solana", sig)  # found by the client's claim first
        self.scan()
        self.scan()
        self.assertEqual(Payment.objects.filter(tx_hash=sig).count(), 1)
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)
        self.assertEqual(WebhookEvent.objects.filter(type="payment.confirmed").count(), 1)

    def test_two_invoices_on_one_wallet_each_get_their_own_transfer(self):
        a, pay_a = self.sent()
        b, pay_b = self.sent()
        qa = self.quote(pay_a, self.method, "solana", "USDC")
        qb = self.quote(pay_b, self.method, "solana", "USDC")
        self.pay(int(qa.amount_base_units))
        self.pay(int(qb.amount_base_units))
        self.assertEqual((self.payments(a)[0].quote_id, self.payments(b)[0].quote_id), (qa.pk, qb.pk))
        self.assertEqual((self.inv(a).status, self.inv(b).status), ("paid", "paid"))


class SolanaOutageTests(SolanaBase):
    def test_a_dead_rpc_changes_nothing_and_the_transfer_is_found_later(self):
        pk, pay = self.sent()
        quote = self.quote(pay, self.method, "solana", "USDC")
        self.rpc.send(USDC, MINE, K0, sender=PAYER)
        name = f"scan:solana:solana:{self.rail.pk}"
        for how in ("timeout", "503", "garbage"):
            with self.subTest(how=how):
                self.internet.down(self.rpc.url, how)
                with self.assertLogs("apps.rails.http", "WARNING"):
                    self.scan()
                self.assertEqual(Payment.objects.count(), 0)
                self.assertIsNotNone(self.health(name).failing_since)
                self.assertNotSettled(pk, pay)
        self.assertEqual(Quote.objects.get(pk=quote.pk).state, "open")
        self.internet.up(self.rpc.url)
        self.scan()
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)


class SolanaNewWalletTests(SolanaBase):
    """A wallet added while invoices go out: the client may pay before the worker has looked at it."""

    def test_a_payment_made_before_the_first_scan_of_a_new_wallet_is_found(self):
        wallet = solana_address("new wallet")
        self.rpc.account_for(wallet, USDC)
        rail = RailAccount.objects.create(org=self.org, kind="solana", label="new", address=wallet,
                                          networks=["solana"], assets=["USDC"])
        pk, pay = self.invoice([rail.method_key])
        quote = self.quote(pay, rail.method_key, "solana", "USDC")
        self.rpc.send(USDC, wallet, int(quote.amount_base_units), sender=PAYER)
        self.scan()  # the first look at this wallet
        self.scan()
        self.assertEqual(len(self.payments(pk)), 1, "the client's USDC was never seen")
        self.assertPaidEverywhere(pk, self.payments(pk)[0], pay)


class SolanaTimeTests(SolanaBase):
    def test_a_transfer_after_the_quote_expired_waits_for_staff(self):
        clock = Clock(self)
        pk, pay = self.sent()
        quote = self.quote(pay, self.method, "solana", "USDC")
        clock.advance(1900)
        self.work("quotes_expire")
        self.pay(int(quote.amount_base_units))
        self.assertInReview(self.payments(pk)[0], "after_expiry", invoice_pk=pk, pay_path=pay)
        self.assertNotSettled(pk)
