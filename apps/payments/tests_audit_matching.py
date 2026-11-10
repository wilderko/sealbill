"""Regression tests from the invoicing audit of 2026-11-03 that concern matching.

- MONEY-1: a single window fit loses (`ambiguous`) to another invoice's quote, live when the
  transfer was made, that asked for more than arrived: by any amount when the transfer only
  overpays the fit (plan B's C1), within two of that quote's allowances when it is a shortfall.
  Fits are counted per invoice, so a refreshed quote and its held predecessor are one.
- MONEY-2: a declaration that conflicts with the amount is ignored. The amount decides, and the
  declaration stays open for staff. A claim may add information, never take it away.
- MONEY-3: a quote of another invoice released in the last 30 days still counts against the
  amount it held, and a window fit needs a quote issued by the block time (the clock-skew
  allowance is for exact amounts only).
- MONEY-6: one address takes every network and every USD stablecoin of its amount group, so an
  exact hit on a quote of another network or asset makes a fit here `ambiguous`.
- MONEY-7: lateness is judged by the block time against the quote's expiry and close, never by
  the quote having been `released`.
- E2E-1: staff confirming or assigning a declared payment match the declaration naming it.
- E2E-3: money for an invoice already settled another way is `invoice_closed`, not late.

The first four classes are ported from `docs/superpowers/audit/2026-11-03-invoicing/poc/money/
poc_money_1.py`, `poc_money_2.py`, `poc_money_3.py` and the payment half of `poc_money_6.py` in
the liberation.services repository, with their assertions.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.audit.models import AuditEntry
from apps.invoices.errors import InvoiceError
from apps.payments import matching
from apps.payments.models import Declaration, Payment
from apps.invoices.reference import payment_reference
from apps.payments.services import assign_payment, confirm_payment, declare_payment, record_manual_payment
from apps.payments.tests_matching import (
    EVM, K0, SECOND, STEP, T0, TON_ADDR, TON_USDT, TX, TX2, TX3, MatchingBase, transfer,
)
from apps.quotes import amounts
from apps.quotes.models import Quote
from apps.quotes.groups import RELEASED_MEMORY_S
from apps.quotes.services import QUOTE_AMOUNT_HOLD_S, QuoteError, create_quote, release_quotes
from apps.quotes.tests import fake_price_in
from apps.rails.models import RailAccount
from apps.rails.networks import NETWORKS
from apps.payments.testing import person

USDC = NETWORKS["arbitrum"].assets["USDC"]
USDT_ARB = NETWORKS["arbitrum"].assets["USDT"].contract
USDC_BASE = NETWORKS["base"].assets["USDC"].contract
USDT_BSC = NETWORKS["bsc"].assets["USDT"].contract
DAY = datetime.timedelta(days=1)


def claim_state(declaration):
    return Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get()


# ---------------------------------------------------------------------------
# MONEY-1 (poc_money_1)
# ---------------------------------------------------------------------------
class NeighbourShortfallTests(MatchingBase):
    """poc_money_1: a victim's shortfall just beyond its own allowance used to land in the
    neighbouring unique amount's window and be confirmed for the neighbour's invoice."""

    def test_a_victims_short_payment_is_not_confirmed_for_the_neighbouring_invoice(self):
        victim_quote = self.quote          # V: 108.27 USDC for self.invoice (100.00 EUR)
        neighbour = self.sent()            # another client's invoice, same price
        nq = self.hold(neighbour, K0 - STEP)  # its unique amount: 108.26 USDC, open at the same time

        # V's exchange keeps a fee one base unit larger than V's allowance.
        arrived = victim_quote.min_accept - 1
        self.assertLess(arrived, victim_quote.min_accept)        # outside V's own window
        self.assertTrue(nq.min_accept <= arrived <= nq.amount)    # inside N's allowance (a shortfall, not an overpay)

        p = self.row(self.ingest(transfer(arrived)))
        # What the design asks for: a transfer V may have sent, outside every window but one
        # only because the neighbour sits one cent lower, is not code's to give away.
        self.assertNotEqual(
            (p.state, p.invoice_id), ("confirmed", neighbour.pk),
            "the victim's money was confirmed as the neighbour's payment: neighbour invoice "
            f"status={self.status(neighbour)!r}, victim invoice status={self.status()!r}")
        self.assertReview(p, "ambiguous")
        self.assertEqual((self.status(neighbour), self.status()), ("sent", "sent"))
        self.assertEqual((self.qstate(nq), self.qstate(victim_quote)), ("open", "open"))

    def test_same_with_the_usd_cap_and_a_one_dollar_exchange_fee(self):
        """At 1337 EUR (~1560 USDC) the allowance is the 1 USD cap; with USDT/USDC priced a
        hair above 1 USD it is slightly under 1.00 token, so the most common exchange fee
        (exactly 1.00) is outside the payer's own window and inside the neighbour's."""
        usd_price = Decimal("1.0003")
        big = 1_560_020_000                      # V: 1560.02
        lo_v, hi_v = amounts.windows(big, USDC, usd_price, {})
        lo_n, hi_n = amounts.windows(big - STEP, USDC, usd_price, {})
        victim = self.sent(total=133700)
        neighbour = self.sent(total=133700)
        vq = self.hold(victim, big, minor=133700, min_accept_base_units=str(lo_v), ceiling_base_units=str(hi_v))
        nq = self.hold(neighbour, big - STEP, minor=133700, min_accept_base_units=str(lo_n),
                       ceiling_base_units=str(hi_n))
        arrived = big - 1_000_000                # the client sent 1560.02, the exchange kept 1.00
        self.assertLess(arrived, vq.min_accept)
        self.assertTrue(nq.min_accept <= arrived <= nq.amount)
        p = self.row(self.ingest(transfer(arrived, tx="0x" + "9e" * 32)))
        self.assertNotEqual(
            (p.state, p.invoice_id), ("confirmed", neighbour.pk),
            f"1559.02 USDC from the 1560.02 client confirmed for the 1560.01 client "
            f"(neighbour={self.status(neighbour)!r}, victim={self.status(victim)!r}, "
            f"credit={p.amount_minor})")
        self.assertReview(p, "ambiguous")


class DirectionFreeRuleTests(MatchingBase):
    """MONEY-1, the rest of the ruling: fits counted per invoice, and the bound on a shortfall."""

    def test_a_refreshed_quote_and_its_held_predecessor_are_one_invoice(self):
        # The client asked for a fresh quote: the first is cancelled, its amount still held, and
        # an exchange kept 0.30 of what the fresh one asked for. Both windows hold the transfer,
        # but only one invoice can be paid by it.
        refreshed_at = T0 + 30 * SECOND
        Quote.objects.filter(pk=self.quote.pk).update(state="cancelled", closed_at=refreshed_at)
        fresh = self.hold(self.invoice, K0 + STEP, issued=refreshed_at)
        arrived = K0 + STEP - 300_000
        self.assertTrue(self.quote.min_accept <= arrived <= self.quote.ceiling)
        self.assertTrue(fresh.min_accept <= arrived <= fresh.ceiling)
        p = self.row(self.ingest(transfer(arrived, at=T0 + 60 * SECOND)))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.amount_minor),
                         ("confirmed", self.invoice.pk, fresh.pk, 10000))
        self.assertEqual(self.status(), "paid")

    def test_a_shortfall_is_held_up_only_by_a_quote_within_two_allowances(self):
        # Another client's quote asked for 0.80 more than arrived: within two of its allowances
        # (2 x 0.54), so the transfer may be that client's fee-short payment.
        near = self.sent()
        self.hold(near, K0 + 50 * STEP)
        arrived = K0 - 300_000  # a fee-short payment of self.quote, inside its window
        self.assertReview(self.ingest(transfer(arrived)), "ambiguous")
        # Another client's quote 2.30 above what arrived is too far to be paid by it.
        Quote.objects.filter(invoice=near).update(state="paid", closed_at=T0)  # no longer live
        far = self.sent()
        self.hold(far, K0 + 200 * STEP)
        p = self.row(self.ingest(transfer(arrived, tx=TX2)))
        self.assertEqual((p.state, p.quote_id, p.amount_minor), ("confirmed", self.quote.pk, 10000))
        self.assertEqual(self.status(far), "sent")

    def test_the_bound_is_two_of_the_organizations_usd_cap_when_that_is_larger(self):
        # A 100.00 USDC quote: 0.5 % (0.50) is below the 1 USD cap. Its client's exchange keeps
        # 1.50: three of the quote's own allowances, but within two of the cap. What arrives,
        # 98.50, lands in a neighbour's window as a 0.30 shortfall.
        hundred = self.sent()
        hq = self.hold(hundred, 100_000_000)
        self.assertEqual(hq.amount - hq.min_accept, 500_000)
        neighbour = self.sent()
        nq = self.hold(neighbour, 98_800_000)
        arrived = 98_500_000
        self.assertTrue(nq.min_accept <= arrived < nq.amount)
        self.assertGreater(hq.amount - arrived, 2 * (hq.amount - hq.min_accept))
        self.assertReview(self.ingest(transfer(arrived)), "ambiguous")
        self.assertEqual((self.status(neighbour), self.status(hundred)), ("sent", "sent"))

    def test_a_live_quote_of_the_same_invoice_is_never_the_one_paid_short(self):
        # The same client also holds a quote on Base that asks for more: whichever they meant,
        # the money is theirs. Neither a shortfall nor an overpayment of the Arbitrum quote waits.
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "base"])
        self.hold(self.invoice, K0 + 50 * STEP, network="base")  # 0.80 above the shortfall below
        p = self.row(self.ingest(transfer(K0 - 300_000)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", self.quote.pk))
        # 128.27 on Arbitrum, beyond every window of the first invoice, and 133.27 on Base.
        other = self.sent(total=20000)
        oq = self.hold(other, K0 + 2000 * STEP, minor=10000)
        self.hold(other, K0 + 2500 * STEP, network="base", minor=10000)  # 5.00 above: any amount more
        q = self.row(self.ingest(transfer(K0 + 2000 * STEP + 300_000, tx=TX2)))
        self.assertEqual((q.state, q.quote_id, q.refund_due_base_units), ("confirmed", oq.pk, "300000"))


# ---------------------------------------------------------------------------
# MONEY-2 (poc_money_2)
# ---------------------------------------------------------------------------
class FrontRunDeclarationTests(MatchingBase):
    """poc_money_2: anyone holding a pay link of another invoice of the organization could, by
    declaring the hash of a victim's transfer before the watcher's next scan, push a payment that
    matches the victim's unique amount exactly into review, unattributed, for good."""

    def test_a_strangers_declaration_does_not_downgrade_an_exact_match(self):
        attacker = self.sent()  # any open invoice of the same organization (or of one sharing the address)
        # Reference: without the declaration this exact transfer confirms V at once.
        # The attacker watches the address, sees V's transfer in a block, declares its hash on
        # their own pay page; the portal verifies in-request, before the 60 s scan.
        d = declare_payment(attacker, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0)]  # exactly V's unique amount
        matching.verify_declaration(d)
        # The scanner now sees the same transfer.
        scanned = self.row(self.ingest(transfer(K0)))
        p = self.row(scanned)
        self.assertEqual(
            (p.state, p.invoice_id), ("confirmed", self.invoice.pk),
            f"V's exact payment is {p.state}/{p.review_reason}, invoice={p.invoice_id}; "
            f"V's invoice is {self.status()!r} although the amount named it exactly")
        # In the PoC the victim declared too. V's invoice is paid now, and a paid invoice takes
        # no declaration: there is nothing left for V to rescue.
        with self.assertRaises(InvoiceError):
            declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        # The stranger's claim attributed nothing and stays open for staff.
        self.assertEqual(claim_state(d), "open")
        self.assertEqual((self.status(attacker), self.status()), ("declared", "paid"))
        self.assertEqual(self.confirmed_sum(attacker), 0)

    def test_a_declaration_whose_invoice_no_window_names_is_ignored(self):
        # V's exchange keeps 0.30, inside V's allowance. Only V's window holds it. A stranger's
        # invoice with a quote far away declares the hash first: the amount still decides.
        attacker = self.sent()
        self.hold(attacker, K0 + 2000 * STEP)  # 128.27
        d = declare_payment(attacker, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0 - 300_000)]
        p = self.row(matching.verify_declaration(d))
        self.assertEqual((p.state, p.invoice_id, p.quote_id, p.amount_minor),
                         ("confirmed", self.invoice.pk, self.quote.pk, 10000))
        self.assertEqual(claim_state(d), "open")
        self.assertEqual((self.status(attacker), self.status()), ("declared", "paid"))

    def test_a_conflicting_declaration_leaves_a_payment_in_review_as_it_was(self):
        # In two windows: nobody's. A stranger's claim names a third invoice with no window
        # holding it; it attributes nothing, and the payment stays as the amount left it.
        other = self.sent()
        self.hold(other, K0 + STEP)
        seen = self.assertReview(self.ingest(transfer(K0 + STEP // 2)), "ambiguous")
        stranger = self.sent()
        self.hold(stranger, K0 + 2000 * STEP)
        d = declare_payment(stranger, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0 + STEP // 2)]
        again = self.row(matching.verify_declaration(d))
        self.assertEqual((again.pk, again.state, again.review_reason, again.invoice_id, again.quote_id),
                         (seen.pk, "needs_review", "ambiguous", None, None))
        self.assertFalse(AuditEntry.objects.filter(action="payment.reattribute").exists())
        self.assertEqual(claim_state(d), "open")


# ---------------------------------------------------------------------------
# MONEY-3 (poc_money_3)
# ---------------------------------------------------------------------------
@override_settings(TON_ENABLED=True)
class ContradictedCommentTests(MatchingBase):
    def test_a_ton_comment_the_amount_contradicts_is_ambiguous(self):
        # The payer wrote one invoice's reference and sent another invoice's exact amount: the
        # payer's own two statements disagree, and neither wins.
        ton = RailAccount.objects.create(org=self.org, kind="ton", label="TON", address=TON_ADDR,
                                         networks=["ton"], assets=["USDT"])
        tq = self.hold(self.invoice, K0, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        other = self.sent()
        oq = self.hold(other, K0 + STEP, network="ton", asset="USDT", address=TON_ADDR, account=ton)
        t = transfer(K0 + STEP, tx="ton-1", comment=payment_reference(self.invoice), network="ton",
                     contract=TON_USDT.contract, to=TON_ADDR, frm="0:" + "cd" * 32)
        self.assertReview(matching.ingest(t, account=ton), "ambiguous")
        self.assertEqual((self.status(), self.status(other)), ("sent", "sent"))
        self.assertEqual((self.qstate(tq), self.qstate(oq)), ("open", "open"))


class ReleasedAmountReuseTests(MatchingBase):
    """poc_money_3: once a quote's 24 h hold ran out (`released`), its exact amount was handed to
    the next quote of another invoice, and a late transfer of the old amount was confirmed for
    that other invoice. Nothing in matching remembered that the amount was somebody else's."""

    def test_late_payment_of_a_released_amount_does_not_settle_someone_elses_invoice(self):
        # Monday: X is quoted K0 and does not pay in time.
        x_quote = self.quote
        Quote.objects.filter(pk=x_quote.pk).update(state="expired", closed_at=x_quote.expires_at)
        release_quotes(now=x_quote.expires_at + (QUOTE_AMOUNT_HOLD_S + 60) * SECOND)
        self.assertEqual(Quote.objects.get(pk=x_quote.pk).state, "released")
        # Wednesday: Y, same price, same rate, gets the smallest free unique amount: K0 again.
        wednesday = T0 + datetime.timedelta(days=2)
        y_invoice = self.sent()
        y_quote = self.hold(y_invoice, K0, issued=wednesday)
        # X pays the amount they were shown on Monday.
        p = self.row(self.ingest(transfer(K0, at=wednesday + 300 * SECOND)))
        self.assertNotEqual(
            (p.state, p.invoice_id), ("confirmed", y_invoice.pk),
            f"X's late payment confirmed for Y: Y={self.status(y_invoice)!r}, X={self.status()!r}, "
            f"y_quote={Quote.objects.get(pk=y_quote.pk).state}")
        self.assertReview(p, "ambiguous")
        self.assertEqual((self.status(y_invoice), self.status()), ("sent", "sent"))


class ReleasedAmountTests(MatchingBase):
    """MONEY-3, the rest of the ruling."""

    def setUp(self):
        super().setUp()
        self.wednesday = T0 + 2 * DAY
        Quote.objects.filter(pk=self.quote.pk).update(state="released", closed_at=self.quote.expires_at)
        self.y = self.sent()

    def test_a_late_fee_short_payment_of_a_released_amount_is_ambiguous(self):
        self.hold(self.y, K0, issued=self.wednesday)
        arrived = K0 - 300_000  # X pays Monday's amount, and the exchange keeps 0.30
        self.assertReview(self.ingest(transfer(arrived, at=self.wednesday + 300 * SECOND)), "ambiguous")
        self.assertEqual(self.status(self.y), "sent")

    def test_an_exact_payment_is_not_held_up_by_a_released_neighbours_window(self):
        # X's released 108.27 would take 108.28 as an overpayment; Y was quoted 108.28 exactly.
        # An exact amount beats a window, as it does against a quote still held.
        yq = self.hold(self.y, K0 + STEP, issued=self.wednesday)
        p = self.row(self.ingest(transfer(K0 + STEP, at=self.wednesday + 300 * SECOND)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", yq.pk))

    def test_an_amount_released_more_than_thirty_days_ago_is_forgotten(self):
        later = self.quote.expires_at + DAY + 31 * DAY
        yq = self.hold(self.y, K0, issued=later)
        p = self.row(self.ingest(transfer(K0, at=later + 300 * SECOND)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", yq.pk))

    def test_a_released_amount_counts_against_a_claim_too(self):
        # Y has the amount now and declares X's late payment of it: attributed to Y for staff,
        # never confirmed, because the amount was X's first.
        yq = self.hold(self.y, K0, issued=self.wednesday)
        p = self.ingest(transfer(K0, at=self.wednesday + 300 * SECOND), declared_invoice=self.y)
        self.assertReview(p, "ambiguous", invoice=self.y, quote=yq, minor=10000)
        self.assertEqual(self.confirmed_sum(self.y), 0)


class ClockSkewTests(MatchingBase):
    """MONEY-3, scenario 2: a quote issued after the money moved (within the clock-skew
    allowance) never wins the transfer by its window."""

    def setUp(self):
        super().setUp()
        self.block = T0 + 60 * SECOND
        self.arrived = K0 - 2_000_000  # 106.27: nobody's window held it when it was made
        self.attacker = self.sent()
        # The attacker saw it land and pressed "refresh quote" 60 s later, before the next scan.
        self.aq = self.hold(self.attacker, self.arrived + 300_000, issued=self.block + 60 * SECOND)
        self.assertTrue(self.aq.min_accept <= self.arrived <= self.aq.ceiling)

    def test_a_quote_issued_after_the_transfer_does_not_win_it_by_its_window(self):
        p = self.assertReview(self.ingest(transfer(self.arrived, at=self.block)), "no_matching_invoice")
        self.assertEqual((self.status(self.attacker), self.qstate(self.aq)), ("sent", "open"))
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)
        self.assertIsNone(p.invoice_id)

    def test_nor_with_a_declaration(self):
        d = declare_payment(self.attacker, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(self.arrived, at=self.block)]
        self.assertReview(matching.verify_declaration(d), "before_quote", invoice=self.attacker, quote=self.aq,
                          minor=10000)
        self.assertEqual(self.confirmed_sum(self.attacker), 0)
        self.assertEqual(claim_state(d), "open")

    def test_an_exact_amount_within_the_skew_still_confirms(self):
        p = self.row(self.ingest(transfer(self.aq.amount, at=self.block)))
        self.assertEqual((p.state, p.quote_id), ("confirmed", self.aq.pk))


# ---------------------------------------------------------------------------
# MONEY-6 (poc_money_6, payment half)
# ---------------------------------------------------------------------------
class CrossNetworkPaymentTests(MatchingBase):
    def setUp(self):
        super().setUp()
        # The account watches Arbitrum, Base and BNB Smart Chain with the same address.
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "base", "bsc"],
                                                           assets=["USDC", "USDT", "ETH"])
        self.y = self.sent()

    def test_a_wrong_network_payment_does_not_settle_another_invoice(self):  # poc_money_6, payment half
        self.enterContext(mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in))
        x_quote = self.quote                         # X: K0 USDC on Arbitrum
        y_invoice = self.y
        y_quote = create_quote(y_invoice, self.acct, "base", "USDC", now=T0)   # K0 + STEP
        self.assertEqual((x_quote.address, y_quote.address), (EVM, EVM))
        self.assertEqual(y_quote.amount, K0 + STEP)
        p = self.row(self.ingest(transfer(K0, network="base", contract=USDC_BASE)))
        self.assertNotEqual((p.state, p.invoice_id), ("confirmed", y_invoice.pk),
                            f"X's wrong-network payment settled Y's invoice: Y={self.status(y_invoice)!r}, "
                            f"X={self.status()!r}")
        self.assertReview(p, "ambiguous")
        self.assertEqual(self.qstate(y_quote), "open")

    def test_a_legacy_pair_sharing_one_amount_is_ambiguous_on_both_networks(self):
        # Issued before amount groups: Y holds K0 on Base while X holds K0 on Arbitrum.
        self.hold(self.y, K0, network="base", shares_amount=True)
        self.assertReview(self.ingest(transfer(K0, network="base", contract=USDC_BASE)), "ambiguous")
        self.assertReview(self.ingest(transfer(K0, tx=TX2)), "ambiguous")
        self.assertEqual((self.status(), self.status(self.y)), ("sent", "sent"))
        self.assertEqual(Payment.objects.filter(state="confirmed").count(), 0)

    def test_a_legacy_pair_of_one_invoice_is_that_invoices(self):
        mine = self.hold(self.invoice, K0, network="base", shares_amount=True)
        p = self.row(self.ingest(transfer(K0, network="base", contract=USDC_BASE)))
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", self.invoice.pk, mine.pk))

    def test_a_declaration_is_held_when_another_invoice_has_its_exact_amount_on_another_network(self):
        # A legacy pair: Y's K0 on Base, X's K0 on Arbitrum. Y declares a K0 transfer on Base.
        # The claim agrees with the amount here, but the amount is X's exactly too: attributed to
        # Y for staff, never confirmed.
        yq = self.hold(self.y, K0, network="base", shares_amount=True)
        p = self.ingest(transfer(K0, network="base", contract=USDC_BASE), declared_invoice=self.y)
        self.assertReview(p, "ambiguous", invoice=self.y, quote=yq, minor=10000)
        self.assertEqual(self.confirmed_sum(self.y), 0)

    def test_the_wrong_stablecoin_on_the_right_network_is_ambiguous(self):
        self.hold(self.y, K0 + STEP, asset="USDT")  # Y: the next free amount of the group, in USDT
        self.assertReview(self.ingest(transfer(K0, contract=USDT_ARB)), "ambiguous")
        self.assertEqual(self.status(self.y), "sent")

    def test_eighteen_decimal_stablecoins_are_compared_by_value(self):
        self.hold(self.y, (K0 + STEP) * 10**12, network="bsc", asset="USDT")
        self.assertReview(self.ingest(transfer(K0 * 10**12, network="bsc", contract=USDT_BSC)), "ambiguous")
        self.assertEqual(self.status(self.y), "sent")

    def test_another_networks_quote_that_asked_for_more_counts_like_one_here(self):
        # Y asked for 2.00 more than X on Base, paid on Arbitrum, and the exchange kept 1.00: it
        # lands in X's window as an overpayment, while Y's quote was live and asked for more.
        self.hold(self.y, K0 + 200 * STEP, network="base")
        self.assertReview(self.ingest(transfer(K0 + 100 * STEP)), "ambiguous")

    def test_each_client_paying_on_its_own_network_is_confirmed(self):
        yq = self.hold(self.y, K0 + STEP, network="base")
        x = self.row(self.ingest(transfer(K0)))
        y = self.row(self.ingest(transfer(K0 + STEP, tx=TX2, network="base", contract=USDC_BASE)))
        self.assertEqual((x.state, x.quote_id, y.state, y.quote_id), ("confirmed", self.quote.pk, "confirmed", yq.pk))
        self.assertEqual((self.status(), self.status(self.y)), ("paid", "paid"))


# ---------------------------------------------------------------------------
# MONEY-7 (the `_outcome` half; the release half is in apps/quotes/tests_audit.py)
# ---------------------------------------------------------------------------
class LatenessByTimeTests(MatchingBase):
    """A quote released while its payment still waited for finality (before release_quotes
    learnt to keep those) is judged by the time the money moved, not by its state."""

    def release(self, quote, closed_at):
        Quote.objects.filter(pk=quote.pk).update(state="released", closed_at=closed_at)

    def test_an_on_time_payment_whose_quote_was_released_is_confirmed_once_final(self):
        p = self.row(self.ingest(transfer(K0, final=False, at=T0 + 60 * SECOND)))  # paid in time
        self.assertEqual((p.state, p.quote_id), ("detected", self.quote.pk))
        self.release(self.quote, self.quote.expires_at)
        after = self.row(matching.settle_if_final(p, "final"))
        self.assertEqual((after.state, after.review_reason, after.quote_id, after.amount_minor),
                         ("confirmed", "", self.quote.pk, 10000))
        self.assertEqual(self.status(), "paid")

    def test_a_payment_made_after_its_released_quote_had_closed_is_late(self):
        Quote.objects.filter(pk=self.quote.pk).update(state="cancelled", closed_at=T0 + 30 * SECOND)
        p = self.row(self.ingest(transfer(K0, final=False, at=T0 + 60 * SECOND)))
        self.assertEqual(p.state, "detected")
        self.release(self.quote, T0 + 30 * SECOND)
        self.assertReview(matching.settle_if_final(p, "final"), "after_expiry", invoice=self.invoice,
                          quote=self.quote, minor=10000)

    def test_a_released_quote_another_payment_paid_is_a_duplicate(self):
        # The quote settles half of a 200.00 invoice: a second payment of it must not count twice.
        half = self.sent(total=20000)
        q = self.hold(half, K0 + STEP)
        first = self.row(self.ingest(transfer(K0 + STEP, final=False, at=T0 + 60 * SECOND)))
        second = self.row(self.ingest(transfer(K0 + STEP, tx=TX2, at=T0 + 90 * SECOND)))
        self.assertEqual((first.state, second.state, self.status(half)), ("detected", "confirmed", "partially_paid"))
        self.release(q, T0 + 90 * SECOND)
        self.assertReview(matching.settle_if_final(first, "final"), "duplicate", invoice=half, quote=q, minor=10000)
        self.assertEqual(self.confirmed_sum(half), 10000)


# ---------------------------------------------------------------------------
# E2E-1 and E2E-3
# ---------------------------------------------------------------------------
class StaffMatchesTheDeclarationTests(MatchingBase):
    def setUp(self):
        super().setUp()
        self.clerk = get_user_model().objects.create_user("clerk", password="x", is_staff=True)

    def test_confirming_a_declared_partial_matches_the_declaration(self):
        self.assertReview(self.ingest(transfer(K0 // 2)), "no_matching_invoice")
        d = declare_payment(self.invoice, network="arbitrum", tx_hash=TX)
        self.adapter.txs[TX] = [transfer(K0 // 2)]
        p = self.row(matching.verify_declaration(d))
        self.assertEqual((p.state, p.invoice_id), ("needs_review", self.invoice.pk))
        self.assertEqual(claim_state(d), "open")
        confirm_payment(p, self.clerk)
        self.assertEqual(self.status(), "partially_paid")
        self.assertEqual(claim_state(d), "matched")
        self.assertTrue(AuditEntry.objects.filter(action="declaration.matched", object_pk=str(d.pk)).exists())

    def test_assigning_a_payment_matches_the_declaration_naming_it(self):
        p = self.assertReview(self.ingest(transfer(K0 // 2)), "no_matching_invoice")
        mine = declare_payment(self.invoice, network="arbitrum", tx_hash=TX.upper().replace("0X", "0x"))
        other = self.sent()
        theirs = declare_payment(other, network="arbitrum", tx_hash=TX)
        elsewhere = declare_payment(self.invoice, network="arbitrum", tx_hash=TX3)
        assign_payment(p, self.invoice, self.clerk, amount_minor=5000, note="checked the wallet")
        self.assertEqual(self.status(), "partially_paid")
        self.assertEqual((claim_state(mine), claim_state(theirs), claim_state(elsewhere)), ("matched", "open", "open"))


class SettledElsewhereTests(MatchingBase):
    def test_money_for_an_invoice_already_paid_another_way_is_invoice_closed(self):
        record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="SEPA", user=person())
        self.assertEqual((self.status(), self.qstate(self.quote)), ("paid", "cancelled"))
        Quote.objects.filter(pk=self.quote.pk).update(closed_at=T0 + 30 * SECOND)  # the bank money came first
        p = self.ingest(transfer(K0, at=T0 + 60 * SECOND))  # well inside the quote's validity
        self.assertReview(p, "invoice_closed", invoice=self.invoice, quote=self.quote, minor=10000)


# ---------------------------------------------------------------------------
# Fix round 1: the allocator keeps away from amounts released lately
# ---------------------------------------------------------------------------
class ReleasedAmountAllocationTests(MatchingBase):
    """MONEY-3 makes an amount released in the last `RELEASED_MEMORY_S` stand in a transfer's
    way. If the allocator handed it straight out again, as the smallest free `k`, the next
    client's exact payment of it would go to review: so it is handed out last."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in))
        Quote.objects.filter(pk=self.quote.pk).delete()  # X's quote is allocated here instead
        self.x_quote = create_quote(self.invoice, self.acct, "arbitrum", "USDC", now=T0)
        self.assertEqual(self.x_quote.amount, K0)
        # X does not pay; the quote expires, and its amount is released a day later.
        Quote.objects.filter(pk=self.x_quote.pk).update(state="expired", closed_at=self.x_quote.expires_at)
        release_quotes(now=self.x_quote.expires_at + (QUOTE_AMOUNT_HOLD_S + 60) * SECOND)
        self.assertEqual(self.qstate(self.x_quote), "released")
        self.wednesday = T0 + 2 * DAY
        self.y = self.sent()

    def test_an_amount_released_lately_is_not_handed_out_again_while_another_is_free(self):
        yq = create_quote(self.y, self.acct, "arbitrum", "USDC", now=self.wednesday)
        self.assertEqual(yq.amount, K0 + STEP)
        # Y pays it exactly and in time: confirmed, not held up by X's released amount.
        p = self.row(self.ingest(transfer(K0 + STEP, at=self.wednesday + 300 * SECOND)))
        self.assertEqual((p.state, p.invoice_id, p.quote_id), ("confirmed", self.y.pk, yq.pk))
        self.assertEqual(self.status(self.y), "paid")

    def test_across_the_amount_group_too(self):
        # X's amount was USDC on Arbitrum; Y asks for USDT on Optimism, the same address and unit.
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "optimism"],
                                                           assets=["USDC", "USDT"])
        yq = create_quote(self.y, self.acct, "optimism", "USDT", now=self.wednesday)
        self.assertEqual(yq.amount, K0 + STEP)

    def test_an_amount_released_longer_ago_is_free_again(self):
        later = self.x_quote.expires_at + datetime.timedelta(seconds=RELEASED_MEMORY_S) + DAY
        yq = create_quote(self.y, self.acct, "arbitrum", "USDC", now=later)
        self.assertEqual(yq.amount, K0)

    def test_a_full_group_falls_back_to_a_released_amount_before_refusing(self):
        # Every other amount for 100.00 EUR is held: X's released one is all that is left.
        holder = self.sent()
        for k in range(1, amounts.MAX_SUFFIX + 1):
            self.hold(holder, K0 + k * STEP, issued=self.wednesday)
        yq = create_quote(self.y, self.acct, "arbitrum", "USDC", now=self.wednesday)
        self.assertEqual(yq.amount, K0)
        # With that one held too, the group is full and the quote is refused.
        with self.assertRaises(QuoteError) as refused:
            create_quote(self.sent(), self.acct, "arbitrum", "USDC", now=self.wednesday)
        self.assertEqual(refused.exception.code, "no_free_amount")
