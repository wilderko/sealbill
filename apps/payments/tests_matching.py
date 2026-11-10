"""The matching engine: whether money that reached one of our addresses settles an invoice.

Every test runs against a fake chain adapter and the Arbitrum USDC allow-list entry (TON USDT
for the comment reference, BTCPay for gateway payments). Quotes are written straight to the
table with the windows `amounts.windows` gives at 1 USD per USDC: K0 = 108.27 USDC for 100.00
EUR, accepted from K0 - 0.54135 up to K0 + 10.827.
"""
import base64
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db.models import Sum
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.reference import payment_reference
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments import matching
from apps.payments.models import REVIEW_REASONS, Declaration, Payment
from apps.payments.services import confirm_payment, declare_payment
from apps.quotes import amounts
from apps.quotes.models import Quote
from apps.quotes.services import QUOTE_LIFETIME_S, expire_quotes
from apps.rails import http
from apps.rails.chains import base as chains
from apps.rails.chains.base import Transfer
from apps.rails.models import RailAccount, WatcherHealth
from apps.rails.networks import NETWORKS
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault

EVM = "0x" + "ab" * 20
PAYER = "0x" + "cd" * 20
STRANGER = "0x" + "12" * 20
USDC = NETWORKS["arbitrum"].assets["USDC"]
TON_USDT = NETWORKS["ton"].assets["USDT"]
TON_ADDR = "0:" + "ab" * 32
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
SECOND = datetime.timedelta(seconds=1)
K0 = 108_270_000  # 100.00 EUR at 0.9237 EUR per USDC, rounded up to the cent
STEP = USDC.step
ALLOWANCE = 541_350  # 0.5 % of K0, under the 1 USD cap
TX, TX2, TX3, TX4 = ("0x" + (c + "f") * 32 for c in "1234")  # hex letters: a hash has a case


def transfer(amount, *, tx=TX, log_index=0, final=True, at=T0 + 60 * SECOND, to=EVM, frm=PAYER,
             contract=USDC.contract, network="arbitrum", comment="", block=1000):
    return Transfer(network=network, contract=contract, to_address=to, from_address=frm,
                    amount_base_units=amount, tx_hash=tx, log_index=log_index, block_number=block,
                    block_time=at, final=final, comment=comment)


class FakeAdapter:
    """A chain that answers what the test told it to."""

    def __init__(self):
        self.statuses, self.txs, self.unavailable = {}, {}, False
        self.broken = {}  # tx hash -> an exception that is not "could not look"

    def scan(self, account, network):
        return []

    def transfers_in_tx(self, network, tx_hash):
        if self.unavailable:
            raise http.Unavailable("fake-rpc", "timeout")
        if tx_hash in self.broken:
            raise self.broken[tx_hash]
        return list(self.txs.get(tx_hash, []))

    def status(self, payment):
        if self.unavailable:
            raise http.Unavailable("fake-rpc", "timeout")
        if payment.tx_hash in self.broken:
            raise self.broken[payment.tx_hash]
        return self.statuses.get(payment.tx_hash, "pending")


class TransferTests(SimpleTestCase):
    def test_transfer_is_frozen_and_checked(self):
        t = transfer(5)
        with self.assertRaises(AttributeError):
            t.amount_base_units = 6
        for bad in (-1, 1.5, "5", True):
            with self.assertRaises(ValueError):
                transfer(bad)
        with self.assertRaises(ValueError):
            transfer(5, at=datetime.datetime(2026, 10, 1, 12, 0))  # naive
        for bad_hash in ("", "  "):
            with self.assertRaises(ValueError):
                transfer(5, tx=bad_hash)
        self.assertEqual(transfer(5).comment, "")

    def test_adapter_registry(self):
        fake = FakeAdapter()
        with chains.override_adapter("evm", fake):
            self.assertIs(chains.adapter_for("arbitrum"), fake)
            self.assertIs(chains.adapter_for("bsc"), fake)
        with self.assertRaises(LookupError):
            chains.adapter_for("nowhere")
        with self.assertRaises(ValueError):
            chains.register("btcpay", fake)


class MatchingBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.adapter = FakeAdapter()
        self.enterContext(chains.override_adapter("evm", self.adapter))
        self.enterContext(chains.override_adapter("ton", self.adapter))
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        self.acct = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                               networks=["arbitrum"], assets=["USDC", "ETH"])
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján Žilinský",
                                                 preferred_language="sk")
        self.invoice = self.sent()
        self.quote = self.hold(self.invoice, K0)

    def sent(self, total=10000):
        # Encrypted title and notes: a full-row load while sealed would raise.
        inv = create_invoice(self.org, self.contact, currency="EUR", title="Consulting", notes="Thanks",
                             lines=[{"name": "Service", "quantity": 1, "unit_price_minor": total}])[0]
        return send_invoice(inv, email_client=False)[0]

    def hold(self, invoice, amount, *, network="arbitrum", asset="USDC", address=EVM, account=None,
             issued=T0, state="open", minor=10000, **kw):
        low = high = amount  # a gateway quote: no window of its own
        if network in NETWORKS:
            low, high = amounts.windows(amount, NETWORKS[network].assets[asset], Decimal("1"), {})
        fields = dict(invoice=invoice, rail_account=account or self.acct, network=network, asset=asset,
                      address=address, amount_base_units=str(amount), min_accept_base_units=str(low),
                      ceiling_base_units=str(high), invoice_amount_minor=minor, issued_at=issued,
                      expires_at=issued + QUOTE_LIFETIME_S * SECOND, state=state)
        fields.update(kw)
        return Quote.objects.create(**fields)

    def ingest(self, t, **kw):
        kw.setdefault("account", self.acct)
        return matching.ingest(t, **kw)

    def row(self, payment):
        return Payment.objects.only(*matching.PAYMENT_PLAIN_FIELDS).get(pk=payment.pk)

    def status(self, invoice=None):
        return Invoice.objects.filter(pk=(invoice or self.invoice).pk).values_list("status", flat=True).get()

    def qstate(self, quote):
        return Quote.objects.filter(pk=quote.pk).values_list("state", flat=True).get()

    def outbox(self, kind):
        return list(OutboxMessage.objects.filter(kind=kind).order_by("pk"))

    def confirmed_sum(self, invoice=None):
        return Payment.objects.filter(invoice=invoice or self.invoice, state="confirmed").aggregate(
            s=Sum("amount_minor"))["s"] or 0

    def assertReview(self, payment, reason, *, invoice=None, quote=None, minor=0):
        p = self.row(payment)
        self.assertEqual((p.state, p.review_reason), ("needs_review", reason))
        self.assertEqual((p.invoice_id, p.quote_id, p.amount_minor),
                         (invoice.pk if invoice else None, quote.pk if quote else None, minor))
        self.assertIsNone(p.confirmed_at)
        self.assertTrue(OutboxMessage.objects.filter(
            kind="review_staff", recipient="staff", plain_context__payment_id=p.pk,
            plain_context__reason=reason).exists(), f"no review e-mail for {reason}")
        return p


class ChainMatchingTests(MatchingBase):
    def test_exact_amount_final_confirms_and_marks_paid(self):
        p = self.row(self.ingest(transfer(K0)))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.amount_minor),
                         ("confirmed", self.invoice.pk, self.quote.pk, 10000))
        self.assertEqual((p.rail, p.network, p.asset, p.rail_account_id), ("evm", "arbitrum", "USDC", self.acct.pk))
        self.assertEqual((p.amount_base_units, p.received_amount, p.contract), (str(K0), Decimal("108.27"),
                                                                                 USDC.contract.lower()))
        self.assertEqual((p.to_address, p.from_address, p.tx_hash, p.log_index), (EVM, PAYER, TX, 0))
        self.assertEqual((p.block_number, p.block_time, p.final), (1000, T0 + 60 * SECOND, True))
        self.assertEqual((p.review_reason, p.refund_due_base_units, p.needs_staff_confirmation), ("", "", False))
        self.assertIsNotNone(p.confirmed_at)
        self.assertIsNone(p.confirmed_by)
        self.assertTrue(p.applied)
        self.assertEqual(self.status(), "paid")
        self.assertEqual(self.qstate(self.quote), "paid")
        self.assertEqual(len(self.outbox("receipt")), 1)
        self.assertEqual(self.outbox("receipt")[0].recipient, "contact")
        self.assertEqual(self.outbox("review_staff"), [])
        entry = AuditEntry.objects.get(action="payment.confirm", object_pk=str(p.pk))
        self.assertEqual((entry.actor_username, entry.detail), ("system", None))
        self.assertEqual(entry.system_detail["invoice"], self.invoice.pk)
        self.assertEqual(entry.system_detail["quote"], self.quote.pk)

    def test_short_within_allowance_is_paid(self):
        self.assertEqual(self.quote.min_accept, K0 - ALLOWANCE)
        p = self.row(self.ingest(transfer(K0 - ALLOWANCE)))
        self.assertEqual((p.state, p.amount_minor, p.refund_due_base_units), ("confirmed", 10000, ""))
        self.assertEqual(self.status(), "paid")
        self.assertEqual(self.qstate(self.quote), "paid")

    def test_overpay_5_percent_paid_with_refund_flag(self):
        over = K0 * 5 // 100
        p = self.row(self.ingest(transfer(K0 + over)))
        self.assertEqual((p.state, p.amount_minor, p.refund_due_base_units), ("confirmed", 10000, str(over)))
        self.assertEqual(self.status(), "paid")  # credited what the quote settles, not more
        self.assertEqual(self.confirmed_sum(), 10000)

    def test_overpay_15_percent_goes_to_review(self):
        amount = K0 + K0 * 15 // 100
        p = self.assertReview(self.ingest(transfer(amount)), "no_matching_invoice")
        msg = OutboxMessage.objects.get(kind="review_staff")
        self.assertEqual(msg.plain_context, {"payment_id": p.pk, "reason": "no_matching_invoice",
                                             "network": "arbitrum", "amount_base_units": str(amount),
                                             "asset": "USDC"})
        self.assertIsNone(msg.invoice_id)
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")
        # With the client's word for whose it is, it is still far too much: review, attributed.
        q = self.ingest(transfer(amount, tx=TX2), declared_invoice=self.invoice)
        q = self.assertReview(q, "overpaid_beyond_ceiling", invoice=self.invoice, quote=self.quote, minor=10000)
        self.assertEqual(q.refund_due_base_units, str(amount - K0))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")

    def test_large_shortfall_without_attribution_is_review(self):
        self.assertReview(self.ingest(transfer(K0 // 2)), "no_matching_invoice")
        # One base unit below the window is already outside it.
        self.assertReview(self.ingest(transfer(K0 - ALLOWANCE - 1, tx=TX2)), "no_matching_invoice")
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)

    def test_large_shortfall_declared_goes_to_review_with_its_credit(self):
        # Only the claim ties a shortfall below the window to this invoice, and anyone who read
        # the hash off the chain can make one: attributed with its credit, confirmed by staff.
        p = self.assertReview(self.ingest(transfer(K0 // 2), declared_invoice=self.invoice), "no_matching_invoice",
                              invoice=self.invoice, quote=self.quote, minor=(K0 // 2) * 10000 // K0)  # 5000
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")
        self.assertEqual(self.outbox("partial"), [])
        clerk = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        confirm_payment(p, clerk)
        self.assertEqual(self.status(), "partially_paid")
        self.assertEqual(self.qstate(self.quote), "paid")  # consumed: a second payment of it is a duplicate
        partial = self.outbox("partial")
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0].plain_context["paid_minor"], 5000)
        # Rounded down, never up.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        sent = (K0 + STEP) // 3
        p2 = self.row(self.ingest(transfer(sent, tx=TX2), declared_invoice=other))
        self.assertEqual((p2.state, p2.quote_id, p2.amount_minor), ("needs_review", q2.pk, 3333))

    def test_two_windows_ambiguous_unless_one_exact(self):
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        self.assertReview(self.ingest(transfer(K0 + STEP // 2)), "ambiguous")  # in both windows, exact for neither
        p = self.row(self.ingest(transfer(K0 + STEP, tx=TX2)))  # in both windows, exact for one
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", other.pk, q2.pk))
        self.assertEqual(self.status(other), "paid")
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")

    def test_after_expiry_goes_to_review_with_invoice(self):
        late = transfer(K0, at=self.quote.expires_at + SECOND)
        p = self.assertReview(self.ingest(late), "after_expiry", invoice=self.invoice, quote=self.quote, minor=10000)
        self.assertEqual(OutboxMessage.objects.get(kind="review_staff").invoice_id, self.invoice.pk)
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")  # not consumed: staff decide
        self.assertEqual(p.refund_due_base_units, "")
        # The last second of a quote is still in time.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        p2 = self.row(self.ingest(transfer(K0 + STEP, tx=TX2, at=q2.expires_at)))
        self.assertEqual((p2.state, p2.invoice_id), ("confirmed", other.pk))

    def test_second_identical_transfer_is_duplicate(self):
        first = self.row(self.ingest(transfer(K0)))
        second = self.ingest(transfer(K0, tx=TX2))
        self.assertEqual(first.state, "confirmed")
        self.assertReview(second, "duplicate", invoice=self.invoice, quote=self.quote, minor=10000)
        self.assertEqual(self.status(), "paid")
        self.assertEqual(self.confirmed_sum(), 10000)  # the second is not credited
        self.assertEqual(len(self.outbox("receipt")), 1)

    def test_token_not_allowed_review_without_invoice(self):
        bridged = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"  # USDC.e on Arbitrum: deliberately not listed
        p = self.ingest(transfer(K0, contract=bridged), declared_invoice=self.invoice)
        p = self.assertReview(p, "token_not_allowed")
        self.assertEqual((p.asset, p.amount_base_units, p.contract, p.received_amount),
                         ("", str(K0), bridged.lower(), Decimal(0)))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")

    def test_not_our_address_ignored(self):
        self.assertIsNone(self.ingest(transfer(K0, to=STRANGER)))
        self.assertIsNone(self.ingest(transfer(K0, to="not an address")))
        self.assertIsNone(self.ingest(transfer(K0, network="tron")))  # not this account's family
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertEqual(self.qstate(self.quote), "open")
        # Our address written in another case is ours.
        self.assertIsNotNone(self.ingest(transfer(K0, to="0x" + "AB" * 20)))

    def test_zero_amount_transfer_is_ignored(self):
        # Address poisoning sends zero-value transfers of real tokens: no money, nothing to review.
        self.assertIsNone(self.ingest(transfer(0)))
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(OutboxMessage.objects.count(), 0)

    def test_ingest_twice_one_payment(self):
        t = transfer(K0)
        a, b = self.ingest(t), self.ingest(t)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(len(self.outbox("receipt")), 1)
        # The same hash written in capitals is the same transfer.
        self.assertEqual(self.ingest(transfer(K0, tx=TX.upper().replace("0X", "0x"))).pk, a.pk)
        # A race: the row appears between the lookup and the insert. The insert loses and the
        # row that won is returned, with nothing of the loser left behind.
        t2 = transfer(K0 // 2, tx=TX2)
        c = self.ingest(t2)
        won = Payment.objects.get(pk=c.pk)
        with mock.patch.object(matching, "_existing", side_effect=[None, won]):
            d = self.ingest(t2)
        self.assertEqual(d.pk, c.pk)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(len(self.outbox("review_staff")), 1)

    def test_non_final_detected_then_final_confirms(self):
        p = self.row(self.ingest(transfer(K0, final=False)))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.final), ("detected", self.invoice.pk, self.quote.pk,
                                                                         False))
        self.assertEqual((p.review_reason, p.refund_due_base_units), ("", ""))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")
        self.assertEqual(self.outbox("receipt"), [])
        # Finality takes longer than the quote lives: it expires meanwhile, which changes nothing,
        # because the transfer was made in time.
        expire_quotes(now=self.quote.expires_at + 60 * SECOND)
        self.assertEqual(self.qstate(self.quote), "expired")
        self.adapter.statuses[TX] = "pending"
        self.assertEqual(matching.refresh_finality(), 0)
        self.assertEqual(self.row(p).state, "detected")
        self.adapter.statuses[TX] = "final"
        self.assertEqual(matching.refresh_finality(), 1)
        p = self.row(p)
        self.assertEqual((p.state, p.final, p.amount_minor), ("confirmed", True, 10000))
        self.assertEqual(self.status(), "paid")
        self.assertEqual(self.qstate(self.quote), "paid")
        self.assertEqual(len(self.outbox("receipt")), 1)
        self.assertIsNotNone(WatcherHealth.objects.get(name="finality:arbitrum").last_ok_at)
        self.assertEqual(matching.refresh_finality(), 0)  # nothing is detected any more

    def test_dropped_twice_rejects(self):
        p = self.ingest(transfer(K0, final=False))
        start = timezone.now()

        def check(seconds, status):
            with mock.patch("django.utils.timezone.now", return_value=start + seconds * SECOND):
                return matching.settle_if_final(p, status).state

        self.assertEqual(check(0, "dropped"), "detected")
        self.assertEqual(check(300, "dropped"), "detected")
        self.assertEqual(check(400, "pending"), "detected")  # seen again: the streak starts over
        self.assertEqual(check(700, "dropped"), "detected")
        self.assertEqual(check(1299, "dropped"), "detected")  # 599 s after the streak began
        with self.assertLogs("apps.payments.matching", level="WARNING") as logs:
            self.assertEqual(check(1300, "dropped"), "rejected")
        self.assertEqual(logs.output, [f"WARNING:apps.payments.matching:payment #{p.pk} rejected (dropped)"])
        r = self.row(p)
        self.assertEqual((r.state, r.review_reason, r.invoice_id), ("rejected", "dropped", self.invoice.pk))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(self.quote), "open")
        self.assertTrue(OutboxMessage.objects.filter(kind="review_staff", plain_context__reason="dropped").exists())
        self.assertEqual(check(2000, "final"), "rejected")  # a rejected payment stays rejected
        void_invoice(self.invoice, "never paid", None)  # nothing is being confirmed any more

    def test_transfer_before_quote_is_not_a_candidate(self):
        early = T0 - (matching.CLOCK_SKEW_S + 1) * SECOND
        self.assertEqual(matching.CLOCK_SKEW_S, 120)
        self.assertReview(self.ingest(transfer(K0, at=early)), "no_matching_invoice")
        # With the client's word for whose it is, it still came before that invoice's quote.
        declared = self.ingest(transfer(K0, tx=TX2, at=early), declared_invoice=self.invoice)
        self.assertReview(declared, "before_quote", invoice=self.invoice)
        self.assertEqual(self.status(), "sent")
        # Within the clock-skew allowance it is a candidate.
        p = self.row(self.ingest(transfer(K0, tx=TX3, at=T0 - matching.CLOCK_SKEW_S * SECOND)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", self.quote.pk))

    def test_void_invoice_quote_goes_to_review(self):
        void_invoice(self.invoice, "mistake", None)
        self.assertEqual(self.qstate(self.quote), "cancelled")
        p = self.ingest(transfer(K0))
        self.assertReview(p, "invoice_closed", invoice=self.invoice, quote=self.quote, minor=10000)
        self.assertEqual(self.status(), "void")

    def test_quote_cancelled_after_the_transfer_still_counts(self):
        # The client paid, then asked for a fresh quote before the transfer was final.
        cancelled_at = T0 + 120 * SECOND
        Quote.objects.filter(pk=self.quote.pk).update(state="cancelled", closed_at=cancelled_at)
        p = self.row(self.ingest(transfer(K0, at=cancelled_at)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", self.quote.pk))
        self.assertEqual(self.qstate(self.quote), "paid")
        # Paying a quote after it was cancelled is late.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP, state="cancelled", closed_at=cancelled_at)
        late = self.ingest(transfer(K0 + STEP, tx=TX2, at=cancelled_at + SECOND))
        self.assertReview(late, "after_expiry", invoice=other, quote=q2, minor=10000)

    def test_native_eth_matches_by_empty_contract(self):
        eth = NETWORKS["arbitrum"].assets["ETH"]
        amount = 40_000_000_000_000_000  # 0.04 ETH
        q = self.hold(self.sent(), amount, asset="ETH")
        p = self.row(self.ingest(transfer(amount, contract="", log_index=-1)))
        self.assertEqual((p.state, p.quote_id, p.asset, p.log_index, p.contract), ("confirmed", q.pk, "ETH", -1, ""))
        self.assertEqual(p.received_amount, Decimal(amount).scaleb(-eth.decimals))

    def test_ingest_while_sealed(self):
        other, third = self.sent(), self.sent()
        q2 = self.hold(other, K0 + STEP)
        q3 = self.hold(third, K0 + 2 * STEP)
        declaration = declare_payment(third, network="arbitrum", tx_hash=TX4, note="sent from my wallet")
        vault.seal()
        paid = self.row(self.ingest(transfer(K0)))
        self.assertEqual(paid.state, "confirmed")
        self.assertEqual(self.status(), "paid")
        self.assertEqual(self.qstate(self.quote), "paid")
        self.assertReview(self.ingest(transfer(K0 // 2, tx=TX2)), "no_matching_invoice")
        seen = self.row(self.ingest(transfer(K0 + STEP, tx=TX3, final=False)))
        self.assertEqual(seen.state, "detected")
        self.adapter.statuses[TX3] = "final"
        self.assertEqual(matching.refresh_finality(), 1)
        self.assertEqual(self.status(other), "paid")
        self.assertEqual(self.qstate(q2), "paid")
        self.adapter.txs[TX4] = [transfer(K0 + 2 * STEP, tx=TX4)]
        verified = matching.verify_declaration(declaration)
        self.assertEqual((verified.state, verified.quote_id), ("confirmed", q3.pk))
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "matched")
        receipts = self.outbox("receipt")
        self.assertEqual(len(receipts), 3)
        self.assertTrue(all(m.to is None and m.body_context is None for m in receipts))  # addressed at flush
        self.assertFalse(AuditEntry.objects.filter(actor_username="system", detail__isnull=False).exists())
        vault.unseal(TEST_VAULT_PASSWORD)
        with override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"]):
            self.assertEqual(notify.flush_outbox(), 4)  # three receipts and one review
        self.assertEqual(len(mail.outbox), 4)


class StaffMailTests(MatchingBase):
    @override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"], SITE_URL="https://pay.example")
    def test_review_and_confirm_mails_are_written_for_staff_in_english(self):
        late = self.ingest(transfer(K0, at=self.quote.expires_at + SECOND))
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["XMR"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"})
        xq = self.hold(self.sent(), 500_000_000_000, network="monero", asset="XMR", address="", account=btcpay,
                       external_id="btcpay:S1:x1", ceiling_base_units="500000000000",
                       min_accept_base_units="500000000000")
        waiting = matching.record_gateway_payment(
            xq, network="monero", external_id="btcpay:S1:x1", received_amount=Decimal("0.5"), amount_minor=10000,
            state="detected", needs_staff_confirmation=True)
        self.assertEqual(notify.flush_outbox(), 2)
        review, confirm = mail.outbox
        self.assertEqual(review.to, ["staff@example.com"])
        self.assertEqual(review.subject, f"Payment #{late.pk} needs review")
        self.assertIn(str(REVIEW_REASONS["after_expiry"]), review.body)
        self.assertIn("108.27 USDC", review.body)
        self.assertIn("Arbitrum One", review.body)
        self.assertIn(self.invoice.number, review.body)
        self.assertIn("https://pay.example/", review.body)
        self.assertEqual(confirm.subject, f"Payment #{waiting.pk} is waiting for your confirmation")
        self.assertIn("0.5 XMR", confirm.body)
        self.assertIn("Monero", confirm.body)
        for message in (review, confirm):
            html = message.alternatives[0][0]
            self.assertIn(f"#{late.pk if message is review else waiting.pk}", html)
            self.assertNotIn("{{", message.body + html)


class LogHygieneTests(MatchingBase):
    def test_logs_name_ids_and_reasons_only(self):
        with self.assertLogs("apps.payments.matching", level="INFO") as logs:
            self.ingest(transfer(K0))
            self.ingest(transfer(K0 // 2, tx=TX2))
            self.ingest(transfer(K0 + STEP, tx=TX3, final=False))
        text = "\n".join(logs.output)
        self.assertIn("no_matching_invoice", text)
        for secret in (EVM, PAYER, TX, TX2, TX3, str(K0), str(K0 // 2), "108.27", "ab" * 20):
            self.assertNotIn(secret, text.lower())


class DeclarationTests(MatchingBase):
    def test_verify_declaration_matches_and_marks_declaration_matched(self):
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        typed = TX.upper().replace("0X", "0x")
        declaration = declare_payment(other, network="arbitrum", tx_hash=typed)
        self.adapter.txs[typed] = [transfer(K0 + STEP), transfer(5, log_index=1, to=STRANGER)]
        p = self.row(matching.verify_declaration(declaration))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.amount_minor), ("confirmed", other.pk, q2.pk, 10000))
        self.assertEqual(p.tx_hash, TX)
        self.assertEqual(Payment.objects.count(), 1)  # the transfer to someone else's address is ignored
        self.assertEqual(self.status(other), "paid")
        self.assertEqual(self.qstate(self.quote), "open")
        d = Declaration.objects.only(*matching.DECLARATION_PLAIN_FIELDS).get(pk=declaration.pk)
        self.assertEqual((d.state, d.verify_attempts), ("matched", 0))
        self.assertIsNotNone(d.verified_at)
        self.assertIsNone(matching.verify_declaration(declaration))  # matched: nothing more to do
        # Could not look: counted, raised, and the claim stays open.
        again = declare_payment(self.invoice, network="arbitrum", tx_hash=TX2)
        self.adapter.unavailable = True
        with self.assertRaises(http.Unavailable):
            matching.verify_declaration(again)
        d = Declaration.objects.only(*matching.DECLARATION_PLAIN_FIELDS).get(pk=again.pk)
        self.assertEqual((d.state, d.verify_attempts, d.verified_at), ("open", 1, None))
        # Nothing to look up without a network and a hash.
        bare = declare_payment(self.invoice, network="", tx_hash="")
        self.assertIsNone(matching.verify_declaration(bare))

    @override_settings(TON_ENABLED=True)
    def test_claim_choosing_between_overlapping_windows_goes_to_review(self):
        # The victim pays 3 % over their own quote; an attacker's neighbouring quote holds that
        # amount too. The attacker declares the hash first: the declaration is all that chose,
        # so the payment is attributed to the attacker for staff to judge, and credits nothing.
        attacker = self.sent()
        aq = self.hold(attacker, K0 + STEP)
        sent = K0 * 103 // 100
        declaration = declare_payment(attacker, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(sent)]
        p = self.assertReview(matching.verify_declaration(declaration), "ambiguous", invoice=attacker, quote=aq,
                              minor=10000)
        self.assertEqual(p.refund_due_base_units, str(sent - (K0 + STEP)))
        self.assertEqual((self.status(attacker), self.status()), ("declared", "sent"))
        self.assertEqual((self.qstate(aq), self.qstate(self.quote)), ("open", "open"))
        self.assertEqual(self.confirmed_sum(attacker), 0)
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "open")
        # Not final yet: in review at once, never waiting to be confirmed.
        seen = self.row(self.ingest(transfer(sent, tx=TX2, final=False), declared_invoice=attacker))
        self.assertEqual((seen.state, seen.review_reason, seen.invoice_id, seen.final),
                         ("needs_review", "ambiguous", attacker.pk, False))
        # A TON comment is the payer's own text inside the transaction: it may choose.
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        tq = self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        self.hold(attacker, K0 + STEP, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        own = transfer(sent, tx="ton-1", comment=payment_reference(self.invoice), network="ton",
                       contract=TON_USDT.contract, to=TON_ADDR, frm="0:" + "cd" * 32)
        c = self.row(matching.ingest(own, account=ton))
        self.assertEqual((c.state, c.invoice_id, c.quote_id), ("confirmed", self.invoice.pk, tq.pk))

    def test_declaration_never_attributes_another_orgs_address(self):
        org_b = Organization.objects.create(slug="b", legal_name="Other s.r.o.", address="Side 2")
        add_bank(org_b)
        theirs = "0x" + "ef" * 20
        acct_b = RailAccount.objects.create(org=org_b, kind="evm", label="EVM B", address=theirs,
                                            networks=["arbitrum"], assets=["USDC"])
        contact_b, _ = find_or_create_contact(org_b, "b@example.com", legal_name="Bea")
        inv_b = send_invoice(create_invoice(org_b, contact_b, currency="EUR", lines=[
            {"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0], email_client=False)[0]
        self.hold(inv_b, K0, address=theirs, account=acct_b)
        declaration = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0 // 2, to=theirs)]
        self.assertIsNone(matching.verify_declaration(declaration))
        self.assertEqual(Payment.objects.count(), 0)
        d = Declaration.objects.only(*matching.DECLARATION_PLAIN_FIELDS).get(pk=declaration.pk)
        self.assertEqual((d.state, d.verified_at), ("open", None))
        # Their own scan finds it, and it is nobody's there.
        self.assertReview(matching.ingest(transfer(K0 // 2, to=theirs), account=acct_b), "no_matching_invoice")
        self.assertEqual((self.status(), self.status(inv_b)), ("declared", "sent"))

    def test_reattribution_never_auto_confirms(self):
        # The watcher saw it first: too short for any window, so nobody's. The client's word for
        # whose it is comes after anyone could have read the hash off the chain, so it attributes
        # the payment, credit worked out, and leaves the confirming to staff.
        scanned = self.assertReview(self.ingest(transfer(K0 // 2)), "no_matching_invoice")
        declaration = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0 // 2)]
        p = self.row(matching.verify_declaration(declaration))
        self.assertEqual(p.pk, scanned.pk)
        self.assertEqual((p.state, p.review_reason, p.invoice_id, p.quote_id, p.amount_minor),
                         ("needs_review", "no_matching_invoice", self.invoice.pk, self.quote.pk, 5000))
        self.assertIsNone(p.confirmed_at)
        self.assertEqual(self.status(), "declared")  # unchanged: nothing is credited
        self.assertEqual(self.qstate(self.quote), "open")
        self.assertEqual(self.confirmed_sum(), 0)
        self.assertEqual(self.outbox("partial"), [])
        d = Declaration.objects.only(*matching.DECLARATION_PLAIN_FIELDS).get(pk=declaration.pk)
        self.assertEqual(d.state, "open")
        self.assertIsNotNone(d.verified_at)
        self.assertTrue(AuditEntry.objects.filter(action="payment.reattribute", object_pk=str(p.pk)).exists())
        attributed = OutboxMessage.objects.filter(kind="review_staff", invoice=self.invoice).get()
        self.assertEqual(attributed.plain_context["payment_id"], p.pk)
        self.assertEqual(matching.verify_declaration(declaration).pk, p.pk)  # asking again changes nothing
        # One step for staff: the credit is already on it.
        clerk = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        confirm_payment(p, clerk)
        self.assertEqual(self.status(), "partially_paid")
        self.assertEqual(self.confirmed_sum(), 5000)
        # Two windows held the amount; a later claim picks one of them, still only for staff, and
        # waiting for finality does not change that.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        seen = self.assertReview(self.ingest(transfer(K0 + STEP // 2, tx=TX2, final=False)), "ambiguous")
        late = declare_payment(other, network="arbitrum", tx_hash=TX2)
        self.adapter.txs[TX2] = [transfer(K0 + STEP // 2, tx=TX2)]
        r = self.row(matching.verify_declaration(late))
        self.assertEqual((r.pk, r.state, r.review_reason, r.invoice_id, r.quote_id, r.amount_minor, r.final),
                         (seen.pk, "needs_review", "ambiguous", other.pk, q2.pk, 10000, False))
        self.adapter.statuses[TX2] = "final"
        self.assertEqual(matching.refresh_finality(), 0)
        self.assertEqual((self.row(r).state, self.row(r).final), ("needs_review", True))
        self.assertEqual(self.status(other), "declared")
        self.assertEqual(self.qstate(q2), "open")

    def test_claimed_shortfall_with_competing_declaration_is_ambiguous(self):
        # Another client has named the same transaction: a claim is all that would tie the
        # shortfall to either invoice, so neither gets it.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        rival = declare_payment(other, network="arbitrum", tx_hash=TX)
        mine = declare_payment(self.invoice, network="arbitrum", tx_hash=TX.upper().replace("0X", "0x"))
        self.adapter.txs[mine.tx_hash] = [transfer(K0 // 2)]
        p = self.assertReview(matching.verify_declaration(mine), "ambiguous")
        self.assertEqual(self.confirmed_sum(), 0)
        self.assertEqual((self.qstate(self.quote), self.qstate(q2)), ("open", "open"))
        # Looking up the rival's claim next settles nothing either.
        self.adapter.txs[TX] = [transfer(K0 // 2)]
        again = self.row(matching.verify_declaration(rival))
        self.assertEqual((again.pk, again.state, again.review_reason, again.invoice_id),
                         (p.pk, "needs_review", "ambiguous", None))
        self.assertEqual((self.status(), self.status(other)), ("declared", "declared"))
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)
        # A rival claim on another transaction is no rival: the claim attributes the shortfall,
        # credit worked out, for staff to confirm (a claim never confirms a shortfall).
        third = self.sent()
        q3 = self.hold(third, K0 + 2 * STEP)
        sent = (K0 + 2 * STEP) // 2
        p3 = self.ingest(transfer(sent, tx=TX2), declared_invoice=third)
        self.assertReview(p3, "no_matching_invoice", invoice=third, quote=q3, minor=sent * 10000 // (K0 + 2 * STEP))
        # A claim in a window is not a shortfall: a rival claim does not stand in its way.
        fourth = self.sent()
        q4 = self.hold(fourth, K0 + 3 * STEP)
        declare_payment(other, network="arbitrum", tx_hash=TX3)
        p4 = self.row(self.ingest(transfer(K0 + 3 * STEP, tx=TX3), declared_invoice=fourth))
        self.assertEqual((p4.state, p4.quote_id), ("confirmed", q4.pk))

    def test_a_claim_against_another_invoices_amount_is_ignored(self):
        # The amount is exactly another invoice's quote: the claim does not outweigh it, nor take
        # it away (audit finding MONEY-2). The amount decides, as if no claim had been made.
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        p = self.row(self.ingest(transfer(K0 + STEP), declared_invoice=self.invoice))
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", other.pk, q2.pk))
        # Nor when the amount fits only other invoices' windows: two of them hold it, so it is
        # nobody's, as it would be unclaimed.
        p = self.ingest(transfer(K0 + 2 * STEP, tx=TX2), declared_invoice=self.sent())
        self.assertReview(p, "ambiguous")
        self.assertEqual((self.qstate(self.quote), self.qstate(q2)), ("open", "paid"))
        self.assertEqual((self.status(), self.status(other)), ("sent", "paid"))
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 1)

    def test_verify_open_declarations_gives_up_after_24h(self):
        fresh = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        stale = declare_payment(self.invoice, network="arbitrum", tx_hash=TX2)
        Declaration.objects.filter(pk=stale.pk).update(created_at=timezone.now() - datetime.timedelta(hours=25))
        declare_payment(self.invoice, network="arbitrum", tx_hash="")  # nothing to look up
        self.adapter.txs[TX] = [transfer(K0)]
        self.adapter.txs[TX2] = [transfer(K0 + 1, tx=TX2)]
        self.assertEqual(matching.verify_open_declarations(), 1)
        self.assertEqual(Declaration.objects.get(pk=fresh.pk).state, "matched")
        self.assertEqual(Declaration.objects.get(pk=stale.pk).state, "matched")  # by the settlement, not a look-up
        self.assertEqual(Payment.objects.count(), 1)  # the stale hash was never looked up
        self.assertIsNone(Declaration.objects.get(pk=stale.pk).verified_at)
        # An open claim that could not be looked up is retried and recorded as a watcher failure.
        other = self.sent()
        late = declare_payment(other, network="arbitrum", tx_hash=TX3)
        self.adapter.unavailable = True
        self.assertEqual(matching.verify_open_declarations(), 0)
        self.assertEqual(Declaration.objects.get(pk=late.pk).verify_attempts, 1)
        self.assertEqual(WatcherHealth.objects.get(name="declarations:arbitrum").last_error_kind, "timeout")
        # Older than a day it is left for staff, still open.
        Declaration.objects.filter(pk=late.pk).update(created_at=timezone.now() - datetime.timedelta(hours=25))
        self.adapter.unavailable = False
        self.assertEqual(matching.verify_open_declarations(), 0)
        self.assertEqual(Declaration.objects.get(pk=late.pk).state, "open")


@override_settings(TON_ENABLED=True)
class TonCommentTests(MatchingBase):
    def test_declared_ton_hash_compares_as_lowercase_hex(self):
        raw = bytes(range(1, 33))
        hexed = raw.hex()
        std = base64.b64encode(raw).decode()
        url = base64.urlsafe_b64encode(raw).decode()
        for declared in (hexed, hexed.upper(), std, url, f" {std} ", std.rstrip("="), url.rstrip("=")):
            self.assertEqual(matching._tx_key("ton", declared), hexed, declared)
        self.assertEqual(matching._tx_key("ton", "not-a-hash"), "not-a-hash")
        self.assertEqual(matching._tx_key("solana", std), std)  # case-sensitive families untouched

    def test_declared_tron_hash_with_0x_is_the_same_transaction(self):
        bare = "ab" * 32
        for declared in (bare, bare.upper(), "0x" + bare, "0X" + bare.upper(), f" 0x{bare} "):
            self.assertEqual(matching._tx_key("tron", declared), bare, declared)
        self.assertEqual(matching._tx_key("arbitrum", "0x" + bare), "0x" + bare)  # EVM keeps its 0x

    def test_tron_declaration_with_0x_is_matched_and_is_a_rival(self):
        tron_addr = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        tron = RailAccount.objects.create(org=self.org, kind="tron", label="Tron", address=tron_addr,
                                          networks=["tron"], assets=["USDT"])
        usdt = NETWORKS["tron"].assets["USDT"]
        tq = self.hold(self.invoice, K0, network="tron", asset="USDT", address=tron.address, account=tron)
        bare = "ab" * 32
        declaration = declare_payment(self.invoice, network="tron", tx_hash="0x" + bare.upper())
        t = transfer(K0, tx=bare, network="tron", contract=usdt.contract, to=tron.address, frm=tron.address)
        p = self.row(matching.ingest(t, account=tron))
        self.assertEqual((p.state, p.quote_id), ("confirmed", tq.pk))
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "matched")
        # Written with 0x, another invoice's claim on the same transaction is still a rival claim.
        other, third = self.sent(), self.sent()
        self.hold(other, K0 + STEP, network="tron", asset="USDT", address=tron.address, account=tron)
        self.hold(third, K0 + 2 * STEP, network="tron", asset="USDT", address=tron.address, account=tron)
        declare_payment(other, network="tron", tx_hash="0x" + "cd" * 32)
        short = transfer(K0 // 2, tx="cd" * 32, network="tron", contract=usdt.contract, to=tron.address,
                         frm=tron.address)
        self.assertReview(matching.ingest(short, account=tron, declared_invoice=third), "ambiguous")

    def test_base64_declaration_is_borne_out_by_the_hex_payment(self):
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        tq = self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        raw = bytes(range(1, 33))
        declaration = declare_payment(self.invoice, network="ton", tx_hash=base64.b64encode(raw).decode())
        t = transfer(K0, tx=raw.hex(), network="ton", contract=TON_USDT.contract, to=TON_ADDR,
                     frm="0:" + "cd" * 32)
        p = self.row(matching.ingest(t, account=ton))
        self.assertEqual((p.state, p.quote_id), ("confirmed", tq.pk))
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "matched")

    def test_ton_comment_reference_attributes(self):
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        tq = self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        other = self.sent()
        self.hold(other, K0 + STEP, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        ref = payment_reference(self.invoice)
        common = dict(network="ton", contract=TON_USDT.contract, to=TON_ADDR, frm="0:" + "cd" * 32)
        written = f"{ref[:4]} {ref[4:]}".lower()  # as a wallet user might type it
        p = self.row(matching.ingest(transfer(50_000_000, tx="ton-1", comment=written, **common), account=ton))
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", self.invoice.pk, tq.pk))
        self.assertEqual(p.amount_minor, 50_000_000 * 10000 // K0)
        self.assertEqual(self.status(), "partially_paid")
        # A comment that is not a reference attributes nothing.
        p2 = matching.ingest(transfer(50_000_000, tx="ton-2", comment="thanks!", **common), account=ton)
        self.assertReview(p2, "no_matching_invoice")
        # Nor does a valid reference of an invoice with no quote on this address.
        stranger = self.sent()
        p3 = matching.ingest(transfer(50_000_000, tx="ton-3", comment=payment_reference(stranger), **common),
                             account=ton)
        self.assertReview(p3, "no_matching_invoice")
        self.assertEqual(self.status(other), "sent")


    def test_comment_matching_two_invoices_is_no_claim(self):
        # Invoice numbers are unique per organization only, so two open invoices can share a
        # reference. A comment naming both names neither.
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        org_b = Organization.objects.create(slug="b", legal_name="Other s.r.o.", address="Side 2")
        add_bank(org_b)
        ton_b = RailAccount.objects.create(org=org_b, kind="ton", label="TON B", address=TON_ADDR,
                                           networks=["ton"], assets=["USDT"])
        contact_b, _ = find_or_create_contact(org_b, "b@example.com", legal_name="Bea")
        inv_b = send_invoice(create_invoice(org_b, contact_b, currency="EUR", lines=[
            {"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0], email_client=False)[0]
        ref = payment_reference(self.invoice)
        self.assertEqual(payment_reference(inv_b), ref)
        self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        self.hold(inv_b, K0 + STEP, network="ton", asset="USDT", address=TON_ADDR, account=ton_b)
        t = transfer(50_000_000, tx="ton-1", comment=ref, network="ton", contract=TON_USDT.contract,
                     to=TON_ADDR, frm="0:" + "cd" * 32)
        self.assertReview(matching.ingest(t, account=ton), "no_matching_invoice")
        self.assertEqual((self.status(), self.status(inv_b)), ("sent", "sent"))
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)


class ClaimsNeverDecideTests(MatchingBase):
    """A claim, or another invoice's overpayment window, never confirms what the amount alone
    would not. The probes of the final review, with their exact numbers: V's quote is 108.27
    USDC (`self.quote`), V's exchange keeps 5 USDC, and 103.27 arrives."""

    SHORT = 103_270_000

    def test_short_payment_is_not_taken_by_anothers_overpayment_window(self):
        attacker = self.sent()
        aq = self.hold(attacker, 103_000_000)  # holds 103.27 as a 0.27 overpayment
        self.assertTrue(aq.min_accept <= self.SHORT <= aq.ceiling)
        self.assertLess(self.SHORT, self.quote.min_accept)  # outside V's own window
        p = self.assertReview(self.ingest(transfer(self.SHORT)), "ambiguous")
        self.assertEqual(p.refund_due_base_units, "")
        self.assertEqual((self.status(attacker), self.status()), ("sent", "sent"))
        self.assertEqual((self.qstate(aq), self.qstate(self.quote)), ("open", "open"))
        self.assertEqual(self.outbox("receipt"), [])
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)
        # The same transfer claimed at first sight by A: attributed for staff, never confirmed.
        claimed = self.ingest(transfer(self.SHORT, tx=TX2), declared_invoice=attacker)
        c = self.assertReview(claimed, "ambiguous", invoice=attacker, quote=aq, minor=10000)
        self.assertEqual(c.refund_due_base_units, str(self.SHORT - 103_000_000))
        self.assertEqual(self.confirmed_sum(attacker), 0)
        self.assertEqual(self.qstate(aq), "open")

    def test_overpayment_confirms_once_no_larger_quote_was_live(self):
        # V's quote had expired when the transfer was made: no larger quote was live, so the
        # overpayment of a quote issued later confirms with a refund flag, as before.
        late = self.quote.expires_at + 60 * SECOND
        other = self.sent()
        oq = self.hold(other, 103_000_000, issued=late - 600 * SECOND)
        p = self.row(self.ingest(transfer(self.SHORT, at=late)))
        self.assertEqual((p.state, p.quote_id, p.refund_due_base_units), ("confirmed", oq.pk, "270000"))

    def test_a_larger_quote_closed_before_the_transfer_was_not_live(self):
        attacker = self.sent()
        aq = self.hold(attacker, 103_000_000)
        # V's quote was cancelled (a refresh) after the transfer was made: it was live then.
        Quote.objects.filter(pk=self.quote.pk).update(state="cancelled", closed_at=T0 + 120 * SECOND)
        self.assertReview(self.ingest(transfer(self.SHORT, at=T0 + 60 * SECOND)), "ambiguous")
        # Paid before the transfer was made: not live, so the overpayment is A's.
        Quote.objects.filter(pk=self.quote.pk).update(state="paid", closed_at=T0 + 30 * SECOND)
        p = self.row(self.ingest(transfer(self.SHORT, tx=TX2, at=T0 + 60 * SECOND)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", aq.pk))

    @override_settings(TON_ENABLED=True)
    def test_a_ton_comment_may_still_choose_the_overpaid_quote(self):
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        attacker = self.sent()
        self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        aq = self.hold(attacker, 103_000_000, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        own = transfer(self.SHORT, tx="ton-1", comment=payment_reference(attacker), network="ton",
                       contract=TON_USDT.contract, to=TON_ADDR, frm="0:" + "cd" * 32)
        p = self.row(matching.ingest(own, account=ton))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.refund_due_base_units),
                         ("confirmed", attacker.pk, aq.pk, "270000"))

    def test_first_sight_declaration_never_confirms_a_shortfall(self):
        # Before the scanner sees V's 103.27, an attacker declares its hash on their own invoice,
        # quoted at 109.28: as a shortfall it would credit them 94.50 EUR.
        attacker = self.sent()
        aq = self.hold(attacker, 109_280_000)
        declaration = declare_payment(attacker, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(self.SHORT)]
        p = self.assertReview(matching.verify_declaration(declaration), "no_matching_invoice", invoice=attacker,
                              quote=aq, minor=9450)
        self.assertEqual(self.confirmed_sum(attacker), 0)
        self.assertEqual((self.status(attacker), self.qstate(aq)), ("declared", "open"))
        self.assertEqual(self.outbox("partial"), [])
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "open")
        # V's later declaration changes nothing: it stays in review, for staff to judge.
        mine = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        again = self.row(matching.verify_declaration(mine))
        self.assertEqual((again.pk, again.state, again.invoice_id), (p.pk, "needs_review", attacker.pk))
        self.assertEqual(self.confirmed_sum(), 0)
        # Not final yet: in review at once, never waiting to be confirmed.
        seen = self.row(self.ingest(transfer(self.SHORT, tx=TX2, final=False), declared_invoice=attacker))
        self.assertEqual((seen.state, seen.review_reason, seen.final), ("needs_review", "no_matching_invoice", False))


class OneBadRowTests(MatchingBase):
    """An unexpected error on one row never stops a pass: it is logged by type, recorded as a
    failing watcher, and the rows after it are still looked at."""

    def test_refresh_finality_goes_on_past_a_broken_row(self):
        other = self.sent()
        q2 = self.hold(other, K0 + STEP)
        first = self.ingest(transfer(K0, final=False))
        second = self.ingest(transfer(K0 + STEP, tx=TX2, final=False))
        self.adapter.broken[TX] = AttributeError("'str' object has no attribute 'get'")
        self.adapter.statuses[TX2] = "final"
        with self.assertLogs("apps.payments.matching", level="ERROR") as logs:
            self.assertEqual(matching.refresh_finality(), 1)
        self.assertEqual((self.row(first).state, self.row(second).state), ("detected", "confirmed"))
        self.assertEqual((self.status(other), self.qstate(q2)), ("paid", "paid"))
        health = WatcherHealth.objects.get(name="finality:arbitrum")
        self.assertEqual((health.consecutive_failures, health.last_error_kind), (1, "AttributeError"))
        self.assertIn("AttributeError", "\n".join(logs.output))
        self.assertNotIn("has no attribute", "\n".join(logs.output))  # the type, never the message

    def test_verify_open_declarations_goes_on_past_a_broken_row(self):
        other = self.sent()
        self.hold(other, K0 + STEP)
        broken = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)  # the oldest goes first
        good = declare_payment(other, network="arbitrum", tx_hash=TX2)
        self.adapter.broken[TX] = AttributeError("'str' object has no attribute 'get'")
        self.adapter.txs[TX2] = [transfer(K0 + STEP, tx=TX2)]
        with self.assertLogs("apps.payments.matching", level="ERROR") as logs:
            self.assertEqual(matching.verify_open_declarations(), 1)
        self.assertEqual(Declaration.objects.get(pk=good.pk).state, "matched")
        self.assertEqual(Declaration.objects.get(pk=broken.pk).state, "open")
        health = WatcherHealth.objects.get(name="declarations:arbitrum")
        self.assertEqual((health.consecutive_failures, health.last_error_kind), (1, "AttributeError"))
        self.assertNotIn("has no attribute", "\n".join(logs.output))

    def test_a_mistyped_evm_hash_raises_no_watcher_alarm(self):
        from apps.rails.chains import evm

        self.enterContext(chains.override_adapter("evm", evm.EvmAdapter()))
        post = self.enterContext(mock.patch("apps.rails.http.post_json",
                                            side_effect=AssertionError("no RPC call for a malformed hash")))
        declare_payment(self.invoice, network="arbitrum", tx_hash="0x123")
        self.assertEqual(matching.verify_open_declarations(), 0)
        post.assert_not_called()
        self.assertFalse(WatcherHealth.objects.filter(name="declarations:arbitrum", failing_since__isnull=False).exists())


class FinalityTests(MatchingBase):
    def test_refresh_finality_could_not_look_changes_nothing(self):
        p = self.ingest(transfer(K0, final=False))
        self.adapter.unavailable = True
        self.assertEqual(matching.refresh_finality(), 0)
        r = self.row(p)
        self.assertEqual((r.state, r.check_attempts), ("detected", 0))
        health = WatcherHealth.objects.get(name="finality:arbitrum")
        self.assertEqual((health.consecutive_failures, health.last_error_kind), (1, "timeout"))
        self.assertEqual(self.status(), "sent")

    def test_non_final_review_payment_is_watched_until_final(self):
        p = self.assertReview(self.ingest(transfer(K0 // 2, final=False)), "no_matching_invoice")
        self.assertFalse(p.final)
        self.adapter.statuses[TX] = "final"
        self.assertEqual(matching.refresh_finality(), 0)  # still in review, now final
        r = self.row(p)
        self.assertEqual((r.state, r.final), ("needs_review", True))
        self.assertEqual(matching.refresh_finality(), 0)


class GatewayTests(MatchingBase):
    def setUp(self):
        super().setUp()
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})

    def gateway_quote(self, invoice, asset, network, amount, external_id):
        return self.hold(invoice, amount, network=network, asset=asset, address="", account=self.btcpay,
                         external_id=external_id, min_accept_base_units=str(amount), ceiling_base_units=str(amount))

    def test_gateway_payment_idempotent_and_xmr_waits_for_staff(self):
        xq = self.gateway_quote(self.invoice, "XMR", "monero", 500_000_000_000, "btcpay:S1:x1")
        kw = dict(network="monero", external_id="btcpay:S1:x1", received_amount=Decimal("0.5"), amount_minor=10000,
                  state="detected", needs_staff_confirmation=True)
        p = matching.record_gateway_payment(xq, **kw)
        again = matching.record_gateway_payment(xq, **kw)
        self.assertEqual(p.pk, again.pk)
        r = self.row(p)
        self.assertEqual((r.state, r.needs_staff_confirmation, r.rail, r.network, r.asset, r.invoice_id, r.quote_id),
                         ("detected", True, "btcpay", "monero", "XMR", self.invoice.pk, xq.pk))
        self.assertEqual((r.external_id, r.amount_minor, r.received_amount, r.rail_account_id),
                         ("btcpay:S1:x1", 10000, Decimal("0.5"), self.btcpay.pk))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(xq), "open")
        confirm = self.outbox("confirm_staff")
        self.assertEqual(len(confirm), 1)
        self.assertEqual((confirm[0].recipient, confirm[0].invoice_id), ("staff", self.invoice.pk))
        self.assertEqual(confirm[0].plain_context, {"payment_id": p.pk, "network": "monero", "asset": "XMR",
                                                    "received_amount": "0.5"})
        # BTC: seen, then settled; asking again changes nothing.
        other = self.sent()
        bq = self.gateway_quote(other, "BTC", "bitcoin", 200_000, "btcpay:S1:b1")
        kw = dict(network="bitcoin", external_id="btcpay:S1:b1", received_amount=Decimal("0.002"), amount_minor=10000)
        a = matching.record_gateway_payment(bq, state="detected", **kw)
        self.assertEqual((self.row(a).state, self.status(other)), ("detected", "sent"))
        b = matching.record_gateway_payment(bq, state="confirmed", **kw)
        c = matching.record_gateway_payment(bq, state="confirmed", **kw)
        self.assertEqual({a.pk, b.pk, c.pk}, {a.pk})
        r = self.row(a)
        self.assertEqual((r.state, r.final), ("confirmed", True))
        self.assertIsNotNone(r.confirmed_at)
        self.assertEqual(self.status(other), "paid")
        self.assertEqual(self.qstate(bq), "paid")
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt", invoice=other).count(), 1)
        # A later "seen" never undoes a confirmation.
        self.assertEqual(self.row(matching.record_gateway_payment(bq, state="detected", **kw)).state, "confirmed")
        self.assertEqual(len(self.outbox("confirm_staff")), 1)
        self.assertEqual(Payment.objects.count(), 2)

    def test_gateway_review_and_closed_invoice(self):
        q = self.gateway_quote(self.invoice, "BTC", "bitcoin", 200_000, "btcpay:S1:b2")
        p = matching.record_gateway_payment(q, network="bitcoin", external_id="btcpay:S1:b2",
                                            received_amount=Decimal("0.001"), amount_minor=5000,
                                            state="needs_review", review_reason="btcpay_partial")
        r = self.assertReview(p, "btcpay_partial", invoice=self.invoice, quote=q, minor=5000)
        self.assertEqual(r.amount_base_units, "")
        self.assertEqual(OutboxMessage.objects.get(kind="review_staff").plain_context["received_amount"], "0.001")
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(q), "open")
        # Money the gateway confirms for a voided invoice is not applied: staff decide.
        voided = self.sent()
        vq = self.gateway_quote(voided, "BTC", "bitcoin", 200_001, "btcpay:S1:b3")
        void_invoice(voided, "mistake", None)
        v = matching.record_gateway_payment(vq, network="bitcoin", external_id="btcpay:S1:b3",
                                            received_amount=Decimal("0.002"), amount_minor=10000, state="confirmed")
        self.assertReview(v, "invoice_closed", invoice=voided, quote=vq, minor=10000)
        self.assertEqual(self.status(voided), "void")
        for bad in (dict(state="paid"), dict(state="needs_review"), dict(state="needs_review", review_reason="nope"),
                    dict(state="confirmed", needs_staff_confirmation=True), dict(state="confirmed", amount_minor=0),
                    dict(state="detected", external_id="")):
            args = dict(network="bitcoin", external_id="btcpay:S1:b9", received_amount=Decimal("0.002"),
                        amount_minor=10000, state="detected")
            args.update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                matching.record_gateway_payment(q, **args)

    def test_gateway_confirmed_never_clears_staff_confirmation(self):
        xq = self.gateway_quote(self.invoice, "XMR", "monero", 500_000_000_000, "btcpay:S1:x2")
        kw = dict(network="monero", external_id="btcpay:S1:x2", received_amount=Decimal("0.5"), amount_minor=10000)
        p = matching.record_gateway_payment(xq, state="detected", needs_staff_confirmation=True, **kw)
        with self.assertLogs("apps.payments.matching", level="INFO") as logs:
            again = matching.record_gateway_payment(xq, state="confirmed", **kw)
        r = self.row(again)
        self.assertEqual((r.pk, r.state, r.needs_staff_confirmation, r.confirmed_at), (p.pk, "detected", True, None))
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.qstate(xq), "open")
        self.assertEqual(self.outbox("receipt"), [])
        self.assertIn(matching.STAFF_CONFIRMATION_REQUIRED, "\n".join(logs.output))
        entry = AuditEntry.objects.get(action="payment.gateway_ignored", object_pk=str(p.pk))
        self.assertEqual(entry.system_detail["code"], "staff_confirmation_required")
        # A person still confirms it.
        clerk = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        confirm_payment(r, clerk)
        self.assertEqual(self.status(), "paid")


class StaffQuoteTests(MatchingBase):
    def review_row(self, minor):
        return Payment.objects.create(
            invoice=self.invoice, quote=self.quote, rail="evm", network="arbitrum", asset="USDC",
            amount_minor=minor, received_amount=Decimal("54.135"), amount_base_units=str(K0 // 2),
            state="needs_review", review_reason="ambiguous", tx_hash=TX4, rail_account=self.acct,
            to_address=EVM, final=True)

    def test_staff_confirm_consumes_the_quote(self):
        from django.contrib.auth import get_user_model
        staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        confirm_payment(self.review_row(5000), staff, "checked")
        self.assertEqual(self.qstate(self.quote), "paid")
        second = self.ingest(transfer(K0, tx=TX2))
        self.assertReview(second, "duplicate", invoice=self.invoice, quote=self.quote, minor=10000)
        self.assertEqual(self.confirmed_sum(), 5000)

    def test_staff_assign_consumes_the_quote(self):
        from django.contrib.auth import get_user_model
        from apps.payments.services import assign_payment
        staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        assign_payment(self.review_row(0), self.invoice, staff, amount_minor=5000, note="by hand")
        self.assertEqual(self.qstate(self.quote), "paid")
        second = self.ingest(transfer(K0, tx=TX2))
        self.assertReview(second, "duplicate", invoice=self.invoice, quote=self.quote, minor=10000)

    def test_reassigning_elsewhere_leaves_the_quote_and_clears_it(self):
        from django.contrib.auth import get_user_model
        from apps.payments.services import assign_payment
        staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        other = self.sent()
        theirs = self.hold(other, K0 + 50 * STEP)
        row = self.review_row(0)  # attributed to our quote, but staff know it paid the other invoice
        assign_payment(row, other, staff, amount_minor=5000, note="their payment")
        p = self.row(row)
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", other.pk, None))
        self.assertEqual(self.qstate(self.quote), "open")  # our quote was not what this paid
        self.assertEqual(self.qstate(theirs), "open")  # nor was theirs: a partial payment leaves it payable
        self.assertEqual(self.status(other), "partially_paid")
