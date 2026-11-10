"""Regression tests from the invoicing audit of 2026-11-03 that concern quotes.

MONEY-6 (allocation): one receiving address serves every network of its family, so a unique
amount is unique per **amount group** (family, unit, address), not per network and asset. The
unit is "USD" for every USD stablecoin, whatever its decimals, and the symbol otherwise.

MONEY-7 (release): a quote that a payment still waits on (detected, or in review and not final)
is never released, for chain quotes as for gateway quotes.

The first two test classes are ported from `docs/superpowers/audit/2026-11-03-invoicing/poc/money/
poc_money_6.py` and `poc_money_7.py` in the liberation.services repository.
"""
import datetime
import logging
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments import matching
from apps.payments.models import Payment
from apps.payments.tests_matching import EVM, K0, SECOND, STEP, T0, TX, MatchingBase, transfer
from apps.portal.tests_crypto import CryptoBase
from apps.quotes import amounts
from apps.quotes import services as quotes
from apps.quotes.models import Quote
from apps.quotes.services import QUOTE_AMOUNT_HOLD_S, QUOTE_LIFETIME_S, create_quote, release_quotes
from apps.quotes.tests import fake_price_in
from apps.rails.models import RailAccount
from apps.rails.networks import NETWORKS
from apps.vault.testing import UnsealedVaultMixin

TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ETH_K0 = 4 * 10**16  # 100.00 EUR at 2500 EUR per ETH
ETH_STEP = NETWORKS["ethereum"].assets["ETH"].step
REFUSAL = "Too many payments are open right now. Please try again in a few minutes."


class CrossNetworkCollisionTests(MatchingBase):
    """poc_money_6: two clients quoted the same price at the same time used to get the same amount
    on different networks of one address, so paying on the wrong network settled the other
    client's invoice. The allocator now hands out amounts unique across the address's group.

    The PoC's payment half (X pays K0 on Base) is not here: with Y quoted K0 + STEP, X's K0 still
    falls inside Y's window, and only the matching change (Task 2) makes that ambiguous."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in))
        # The account watches Arbitrum and Base with the same address (RailAccount allows it).
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "base"])

    def test_the_same_unique_amount_is_not_handed_out_on_two_networks_of_one_address(self):
        x_quote = self.quote  # X: K0 USDC on Arbitrum
        y_invoice = self.sent()
        # The PoC wrote Y's quote straight to the table, K0 USDC on Base, as the allocator handed
        # it out. The database refuses that row now...
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.hold(y_invoice, K0, network="base")
        # ...and the allocator quotes Y another amount on the same address.
        y_quote = create_quote(y_invoice, self.acct, "base", "USDC", now=T0)
        self.assertEqual((x_quote.address, y_quote.address), (EVM, EVM))
        self.assertEqual(y_quote.amount, K0 + STEP)

    def test_an_amount_held_on_another_network_of_the_same_address_is_held_there_too(self):
        held_on_arbitrum = {int(a) for a in Quote.objects.filter(address=EVM, asset="USDC", network="arbitrum")
                            .values_list("amount_base_units", flat=True)}
        held_on_base = quotes._held_amounts("base", "USDC", EVM, sorted(held_on_arbitrum))
        self.assertEqual(held_on_base, {K0})  # K0, held on Arbitrum, is not free to give out on Base

    def test_usd_stablecoins_share_one_group_whatever_their_decimals_and_eth_has_its_own(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "base", "bsc"],
                                                           assets=["USDC", "USDT", "ETH"])

        def quote(network, asset):
            return create_quote(self.sent(), self.acct, network, asset, now=T0)

        self.assertEqual(quote("base", "USDC").amount, K0 + STEP)
        self.assertEqual(quote("arbitrum", "USDT").amount, K0 + 2 * STEP)  # USDT and USDC: one unit
        bsc = quote("bsc", "USDT")  # 18 decimals: the same value is a different number of base units
        self.assertEqual(bsc.amount, (K0 + 3 * STEP) * 10**12)
        self.assertEqual(bsc.amount_group, f"evm:USD:{EVM}")
        self.assertEqual(quote("base", "ETH").amount, ETH_K0)  # ETH is a group of its own...
        self.assertEqual(quote("arbitrum", "ETH").amount, ETH_K0 + ETH_STEP)  # ...across every EVM network
        self.assertEqual(Quote.objects.get(pk=self.quote.pk).amount_group, f"evm:USD:{EVM}")


class ReleasedWhileDetectedTests(MatchingBase):
    """poc_money_7: a chain quote whose (on-time) payment was still waiting for finality was
    released after 24 h like any other, and the payment, once final, was judged "after expiry"."""

    def test_an_on_time_payment_waiting_for_finality_keeps_its_quote(self):
        p = self.row(self.ingest(transfer(K0, final=False, at=T0 + 60 * SECOND)))  # paid in time
        self.assertEqual((p.state, p.quote_id), ("detected", self.quote.pk))
        # The finality check cannot answer for a day (RPC outage, finality stall)...
        Quote.objects.filter(pk=self.quote.pk).update(state="expired", closed_at=self.quote.expires_at)
        released = release_quotes(now=self.quote.expires_at + (QUOTE_AMOUNT_HOLD_S + 60) * SECOND)
        # ...then the chain answers "final".
        after = self.row(matching.settle_if_final(p, "final"))
        self.assertEqual(
            after.state, "confirmed",
            f"released {released} quote(s); the on-time payment is now {after.state}/{after.review_reason}, "
            f"invoice {self.status()!r}")

    def test_a_quote_is_kept_while_its_payment_in_review_is_not_final(self):
        Quote.objects.filter(pk=self.quote.pk).update(state="expired", closed_at=self.quote.expires_at)
        p = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=0,
                                   received_amount="108.27", amount_base_units=str(K0), tx_hash=TX,
                                   quote=self.quote, state="needs_review", review_reason="ambiguous", final=False)
        later = self.quote.expires_at + (QUOTE_AMOUNT_HOLD_S + 60) * SECOND
        self.assertEqual(release_quotes(now=later), 0)
        self.assertEqual(self.qstate(self.quote), "expired")
        Payment.objects.filter(pk=p.pk).update(final=True)  # in review, and the chain has settled it
        self.assertEqual(release_quotes(now=later), 1)
        self.assertEqual(self.qstate(self.quote), "released")


class FullGroupPayPageTests(CryptoBase):
    """Review Focus 1: amounts shared across networks run out sooner. A full group is a refusal
    the pay page explains, never a 500."""

    def test_quote_refused_cleanly_when_the_group_is_full(self):
        other = create_invoice(self.org, self.contact, currency="EUR", offered_methods=[f"rail:{self.evm.pk}"],
                               idempotency_key="other",
                               lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0]
        send_invoice(other, email_client=False)
        now = timezone.now()
        # Every unique amount for 100.00 EUR is held on the address, by quotes on another network
        # and in another stablecoin than the one about to be asked for.
        for k in range(amounts.MAX_SUFFIX + 1):
            network, asset = (("ethereum", "USDC"), ("arbitrum", "USDT"))[k % 2]
            amount = str(K0 + k * STEP)
            Quote.objects.create(invoice=other, rail_account=self.evm, network=network, asset=asset, address=EVM,
                                 amount_base_units=amount, min_accept_base_units=amount, ceiling_base_units=amount,
                                 invoice_amount_minor=10000, issued_at=now,
                                 expires_at=now + datetime.timedelta(seconds=QUOTE_LIFETIME_S))
        r = self.ask(HTTP_HX_REQUEST="true")  # the button on the pay page
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, REFUSAL)
        self.assertNotContains(r, "no_free_amount")
        r = self.ask()  # without JavaScript: the whole page, with the sentence
        self.assertContains(r, REFUSAL, status_code=409)
        self.assertContains(r, "<html", status_code=409)
        self.assertFalse(Quote.objects.filter(invoice=self.invoice).exists())
        self.assertEqual(self.client.get(f"/p/{self.pay}/?lang=en").status_code, 200)


BEFORE = ("quotes", "0002_quote_read_at")


class AmountGroupMigrationTests(UnsealedVaultMixin, TransactionTestCase):
    """Review Focus 2 and 4: the widened constraint migrates a database that already holds quotes,
    open ones included, without changing what any client was told to send."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in))
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.evm = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                              networks=["arbitrum", "base", "bsc", "ethereum", "optimism"],
                                              assets=["USDC", "USDT", "ETH"])
        self.tron = RailAccount.objects.create(org=self.org, kind="tron", label="Tron", address=TRON,
                                               networks=["tron"], assets=["USDT"])
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján")
        self.addCleanup(self.migrate_to_latest)

    def sent(self, key):
        inv = create_invoice(self.org, self.contact, currency="EUR", idempotency_key=key,
                             lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0]
        return send_invoice(inv, email_client=False)[0]

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_backfills_amount_group_on_existing_quotes(self):
        x, y = self.sent("x"), self.sent("y")
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        Old = executor.loader.project_state([BEFORE]).apps.get_model("quotes", "Quote")

        def legacy(invoice, network, asset, amount, *, address=EVM, account=None, state="open", **kw):
            return Old.objects.create(
                invoice_id=invoice.pk, rail_account_id=(account or self.evm).pk, network=network, asset=asset,
                address=address, amount_base_units=str(amount), min_accept_base_units=str(amount),
                ceiling_base_units=str(amount), invoice_amount_minor=10000, issued_at=T0,
                expires_at=T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S), state=state, **kw).pk

        a = legacy(x, "arbitrum", "USDC", K0)
        b = legacy(y, "base", "USDC", K0)  # the same amount on another network: the old rule allowed it
        d = legacy(y, "bsc", "USDT", K0 * 10**12)  # the same value at 18 decimals
        e = legacy(x, "ethereum", "ETH", ETH_K0)
        t = legacy(x, "tron", "USDT", K0, address=TRON, account=self.tron)  # another family and address
        g = legacy(x, "bitcoin", "BTC", 123, address="", external_id="btcpay-1")  # a gateway quote
        r = legacy(y, "optimism", "USDC", K0, state="released")  # released: it holds nothing
        before = {pk: row for pk, *row in Old.objects.values_list("pk", "network", "asset", "address",
                                                                  "amount_base_units", "state")}

        with self.assertLogs("apps.quotes.migrations", logging.WARNING) as logs:
            self.migrate_to_latest()

        usd, scaled = f"evm:USD:{EVM}", str(K0 * 10**12)
        rows = {q.pk: q for q in Quote.objects.all()}
        self.assertEqual({pk: (q.amount_group, q.group_amount, q.shares_amount) for pk, q in rows.items()}, {
            a: (usd, scaled, False),  # the first of the colliding quotes stays under the constraint
            b: (usd, scaled, True),  # the others are kept as they were, outside it, until released
            d: (usd, scaled, True),
            e: (f"evm:ETH:{EVM}", str(ETH_K0), False),
            t: (f"tron:USD:{TRON}", scaled, False),
            g: ("", "", False),
            r: (usd, scaled, False),
        })
        self.assertEqual({pk: [q.network, q.asset, q.address, q.amount_base_units, q.state]
                          for pk, q in rows.items()}, before)  # nobody's amount or state moved
        self.assertEqual(len(logs.records), 1)
        self.assertIn("2 held quote(s)", logs.output[0])
        self.assertNotIn(EVM, logs.output[0])  # counts only: no address, no amount

        # After the deploy the allocator sees K0 as held across the whole group...
        self.assertEqual(create_quote(self.sent("z"), self.evm, "ethereum", "USDT", now=T0).amount, K0 + STEP)
        # ...and the database refuses another K0 in it.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Quote.objects.create(invoice=x, rail_account=self.evm, network="optimism", asset="USDT", address=EVM,
                                 amount_base_units=str(K0), min_accept_base_units=str(K0),
                                 ceiling_base_units=str(K0), invoice_amount_minor=10000, issued_at=T0,
                                 expires_at=T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S))
