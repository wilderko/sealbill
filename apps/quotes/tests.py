import datetime
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.reference import payment_reference
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.orgs.models import Organization, default_org_settings
from apps.orgs.testing import add_bank
from apps.payments.models import Payment
from apps.payments.services import apply_payments, declare_payment, record_manual_payment
from apps.quotes import amounts, groups, services
from apps.quotes.models import Quote
from apps.quotes.services import (
    QUOTE_AMOUNT_HOLD_S, QUOTE_LIFETIME_S, QuoteError, close_quotes, create_quote, expire_quotes,
    release_quotes,
)
from apps.rails import fx, http, uris
from apps.rails.fx import RatePair
from apps.rails.gateways import registry
from apps.rails.models import RailAccount, WatcherHealth
from apps.rails.networks import NETWORKS
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault
from apps.payments.testing import person

EVM = "0x" + "ab" * 20
USDC = NETWORKS["arbitrum"].assets["USDC"]
BSC_USDT = NETWORKS["bsc"].assets["USDT"]
ETH = NETWORKS["ethereum"].assets["ETH"]
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
RATES = {
    ("USDC", "EUR"): Decimal("0.9237"), ("USDC", "USD"): Decimal("1"),
    ("USDT", "EUR"): Decimal("0.9237"), ("USDT", "USD"): Decimal("1"),
    ("ETH", "EUR"): Decimal("2500"), ("ETH", "USD"): Decimal("2700"),
}
K0 = 108270000  # 100.00 EUR at 0.9237 EUR per USDC, rounded up to the cent
STEP = USDC.step


def fake_price_in(asset, currency):
    if asset == currency:
        return RatePair(Decimal(1), Decimal(1), "identity")
    rate = RATES[(asset, currency)]
    return RatePair(rate, rate * Decimal("1.001"), "kraken+coinbase")


class AmountTests(SimpleTestCase):
    def test_amount_rounded_up_never_short(self):
        self.assertEqual(amounts.base_units_for(10000, "EUR", Decimal("0.9237"), USDC), 108270000)
        # 100 / 0.9237 = 108.2602...: the client is never asked for less than the invoice is worth
        self.assertGreaterEqual(Decimal(108270000) / 10**6 * Decimal("0.9237"), Decimal(100))
        self.assertEqual(amounts.base_units_for(10000, "USD", Decimal("1"), USDC), 100_000_000)  # exact: no extra step
        self.assertEqual(amounts.base_units_for(10000, "EUR", Decimal("0.9237"), BSC_USDT), 108_270_000 * 10**12)
        self.assertEqual(amounts.base_units_for(1500, "JPY", Decimal("150"), USDC), 10_000_000)  # no minor units
        self.assertEqual(amounts.base_units_for(10000, "EUR", Decimal("2500"), ETH), 40_000_000_000_000_000)
        self.assertEqual(amounts.base_units_for(10001, "EUR", Decimal("2500"), ETH), 40_004_000_000_000_000)
        # 0.01 EUR at 3333.33 is 0.000003000003 ETH: up to the next step, 0.000004
        self.assertEqual(amounts.base_units_for(1, "EUR", Decimal("3333.33"), ETH), 4 * 10**12)
        for bad_amount, bad_rate in ((0, Decimal(1)), (-5, Decimal(1)), (100, Decimal(0)), (100, Decimal("-1"))):
            with self.assertRaises(ValueError):
                amounts.base_units_for(bad_amount, "EUR", bad_rate, USDC)

    def test_windows_use_smaller_of_1_usd_and_half_percent(self):
        s = default_org_settings()
        # 50 USDC: 0.5 % is 0.25, under the 1 USD cap
        self.assertEqual(amounts.windows(50_000_000, USDC, Decimal("1"), s), (49_750_000, 55_000_000))
        # 1000 USDC: 0.5 % would be 5.00, so the 1 USD cap applies
        self.assertEqual(amounts.windows(1_000_000_000, USDC, Decimal("1"), s), (999_000_000, 1_100_000_000))
        # 1 USD at 0.9998 USD per USDC is 1.000200040008 USDC: rounded down to 1.000200
        self.assertEqual(amounts.windows(1_000_000_000, USDC, Decimal("0.9998"), s)[0], 998_999_800)
        # 0.1 ETH at 2700 USD: 1 USD is 0.000370370370370370370 ETH, under 0.5 % (0.0005 ETH)
        self.assertEqual(amounts.windows(10**17, ETH, Decimal("2700"), s),
                         (10**17 - 370_370_370_370_370, 11 * 10**16))
        # a 10 % ceiling on 0.33 USDC rounds down
        self.assertEqual(amounts.windows(333_333, USDC, Decimal("1"), s)[1], 366_666)
        strict = {"underpay_allowance_bps": 0, "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": 0}
        self.assertEqual(amounts.windows(50_000_000, USDC, Decimal("1"), strict), (50_000_000, 50_000_000))
        self.assertEqual(amounts.windows(50_000_000, USDC, Decimal("1"), {}), (49_750_000, 55_000_000))  # defaults
        self.assertEqual(amounts.MAX_SUFFIX, 99)


class QuoteBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("apps.rails.fx.price_in", side_effect=fake_price_in)
        self.price_in = patcher.start()
        self.addCleanup(patcher.stop)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.bank = add_bank(self.org)
        self.acct = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM.upper().replace("0X", "0x"),
                                               networks=["arbitrum", "ethereum"], assets=["USDC", "ETH"])
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján")
        self.invoice = self.sent()

    def sent(self, total=10000, currency="EUR", org=None, contact=None, **kw):
        org = org or self.org
        contact = contact or self.contact
        inv = create_invoice(org, contact, currency=currency,
                             lines=[{"name": "Service", "quantity": 1, "unit_price_minor": total}], **kw)[0]
        return send_invoice(inv, email_client=False)[0]

    def quote(self, invoice=None, network="arbitrum", asset="USDC", **kw):
        return create_quote(invoice or self.invoice, self.acct, network, asset, **kw)

    def hold(self, invoice, amount, state="open", network="arbitrum", asset="USDC", address=EVM, **kw):
        """A quote row written straight to the table, as another request would have."""
        defaults = dict(invoice=invoice, rail_account=self.acct, network=network, asset=asset, address=address,
                        amount_base_units=str(amount), min_accept_base_units=str(amount),
                        ceiling_base_units=str(amount), invoice_amount_minor=10000, issued_at=T0,
                        expires_at=T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S), state=state)
        defaults.update(kw)
        return Quote.objects.create(**defaults)

    def state(self, quote):
        return Quote.objects.filter(pk=quote.pk).values_list("state", flat=True).get()


class AllocationTests(QuoteBase):
    def test_chain_quote_fields(self):
        q = self.quote(now=T0)
        self.assertEqual(self.invoice.offered_methods, [f"bank:{self.bank.pk}", f"rail:{self.acct.pk}"])
        self.assertEqual((q.network, q.asset, q.address, q.state), ("arbitrum", "USDC", EVM, "open"))
        self.assertEqual((q.amount_base_units, q.amount), (str(K0), K0))
        self.assertEqual((q.min_accept_base_units, q.ceiling_base_units), (str(K0 - 541350), str(K0 + 10827000)))
        self.assertEqual((q.min_accept, q.ceiling), (K0 - 541350, K0 + 10827000))
        self.assertEqual(q.invoice_amount_minor, 10000)
        self.assertEqual((q.rate, q.rate_second, q.rate_source), ("0.9237", "0.9246237", "kraken+coinbase"))
        self.assertEqual((q.issued_at, q.expires_at), (T0, T0 + datetime.timedelta(seconds=1800)))
        self.assertIsNone(q.closed_at)
        self.assertEqual(q.payment_uri, uris.payment_uri("arbitrum", "USDC", EVM, K0, payment_reference(self.invoice)))
        self.assertEqual((q.external_id, q.checkout_url, q.bolt11, q.check_soon), ("", "", "", False))
        self.assertEqual(Quote.objects.get(pk=q.pk).amount_base_units, str(K0))
        entry = AuditEntry.objects.get(action="quote.create")
        self.assertEqual(entry.system_detail["quote"], q.pk)
        self.assertEqual(entry.system_detail["invoice"], self.invoice.pk)

    def test_two_quotes_same_amount_get_distinct_unique_amounts(self):
        second = self.sent()
        q1 = self.quote(now=T0)
        q2 = self.quote(second, now=T0)
        self.assertEqual((q1.amount, q2.amount), (K0, K0 + STEP))
        # the window moves with the unique amount
        self.assertEqual(q2.ceiling, (K0 + STEP) + (K0 + STEP) // 10)
        # amounts are unique per amount group (the address) across organizations
        other = Organization.objects.create(slug="us")
        add_bank(other)
        theirs = RailAccount.objects.create(org=other, kind="evm", label="EVM", address=EVM,
                                            networks=["arbitrum"], assets=["USDC"])
        contact, _ = find_or_create_contact(other, "b@example.com", legal_name="B")
        q3 = create_quote(self.sent(org=other, contact=contact), theirs, "arbitrum", "USDC", now=T0)
        self.assertEqual(q3.amount, K0 + 2 * STEP)
        # ... and across the networks of that address: a payment on the wrong one must not match
        # another client's quote (MONEY-6), so another network does not start again at k = 0
        q4 = self.quote(self.sent(), network="ethereum", now=T0)
        self.assertEqual(q4.amount, K0 + 3 * STEP)

    def test_concurrent_allocation_retries_on_integrity_error(self):
        # Another request took k = 0 after this one looked for free amounts.
        self.hold(self.sent(), K0)
        with mock.patch.object(services, "_held_amounts", return_value=set()) as stale:
            q = self.quote(now=T0)
        stale.assert_called_once()
        self.assertEqual(q.amount, K0 + STEP)
        # the savepoint kept the outer transaction usable
        self.assertEqual(Quote.objects.filter(amount_base_units=str(K0 + STEP)).count(), 1)
        self.assertEqual(Quote.objects.count(), 2)

    def test_integrity_error_for_another_reason_is_not_retried(self):
        calls = []

        def failing(quote, *args, **kw):
            calls.append(quote.amount_base_units)
            raise IntegrityError("something else")

        with mock.patch.object(Quote, "save", autospec=True, side_effect=failing):
            with self.assertRaises(IntegrityError):
                self.quote(now=T0)
        self.assertEqual(calls, [str(K0)])  # one attempt: the amount was free, so this is not a race

    def test_no_free_amount_after_100_held(self):
        other = self.sent()
        states = ["open", "expired", "cancelled", "paid"]
        for k in range(amounts.MAX_SUFFIX + 1):
            self.hold(other, K0 + k * STEP, state=states[k % 4])
        with self.assertRaises(QuoteError) as ctx:
            self.quote(now=T0)
        self.assertEqual(ctx.exception.code, "no_free_amount")
        self.assertFalse(Quote.objects.filter(invoice=self.invoice).exists())
        Quote.objects.filter(amount_base_units=str(K0 + 7 * STEP)).update(state="released")
        self.assertEqual(self.quote(now=T0).amount, K0 + 7 * STEP)

    def test_gateway_and_empty_address_never_collide(self):
        # BTCPay and Lightning quotes have no address and are matched by their gateway id
        self.hold(self.invoice, K0, address="", external_id="a")
        self.hold(self.invoice, K0, address="", external_id="b")
        self.hold(self.invoice, K0, state="released")
        self.hold(self.invoice, K0, state="released")
        self.hold(self.invoice, K0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.hold(self.invoice, K0, state="expired")

    def test_external_id_unique_per_account_when_set(self):
        self.hold(self.invoice, 1, address="", external_id="x")
        self.hold(self.invoice, 1, address="", external_id="")
        self.hold(self.invoice, 1, address="", external_id="")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.hold(self.invoice, 2, address="", external_id="x")

    def test_amounts_are_canonical_digit_strings(self):
        for bad in ("012", "1.5", "-1", "", "0", "1e5", "1" * 79):
            with self.assertRaises(ValidationError, msg=bad):
                self.hold(self.invoice, bad)
        with self.assertRaises(ValidationError):
            self.hold(self.invoice, 5, min_accept_base_units="")
        with self.assertRaises(ValidationError):
            self.hold(self.invoice, 5, min_accept_base_units="6")  # the window holds the amount
        with self.assertRaises(ValidationError):
            self.hold(self.invoice, 5, ceiling_base_units="4")
        self.assertEqual(self.hold(self.invoice, 5, min_accept_base_units="0").min_accept, 0)

    def test_payment_points_at_its_quote(self):
        q = self.quote(now=T0)
        p = Payment.objects.create(invoice=self.invoice, rail="evm", asset="USDC", amount_minor=10000,
                                   received_amount="108.27", quote=q, state="needs_review",
                                   review_reason="after_expiry")
        self.assertEqual(list(q.payments.all()), [p])
        with self.assertRaises(ProtectedError):
            q.delete()


class ReuseTests(QuoteBase):
    def test_open_quote_reused_unless_refresh(self):
        q1 = self.quote(now=T0)
        self.price_in.reset_mock()
        again = self.quote(now=T0 + datetime.timedelta(seconds=60))
        self.assertEqual((again.pk, again.amount, again.expires_at), (q1.pk, q1.amount, q1.expires_at))
        self.price_in.assert_not_called()  # a reload costs no exchange-rate request
        self.assertEqual(Quote.objects.count(), 1)

        q2 = self.quote(now=T0 + datetime.timedelta(seconds=60), refresh=True)
        self.assertNotEqual(q2.pk, q1.pk)
        q1.refresh_from_db()
        self.assertEqual(q1.state, "cancelled")
        self.assertIsNotNone(q1.closed_at)
        self.assertEqual(q2.amount, K0 + STEP)  # the cancelled quote's amount stays held

        # exactly 300 s left: still reused; 299 s left: a new quote
        at_300 = q2.expires_at - datetime.timedelta(seconds=300)
        self.assertEqual(self.quote(now=at_300).pk, q2.pk)
        q3 = self.quote(now=at_300 + datetime.timedelta(seconds=1))
        self.assertNotEqual(q3.pk, q2.pk)
        self.assertEqual(self.state(q2), "open")  # it runs out on its own; a payment in flight still counts

    def test_different_key_or_remainder_is_a_new_quote(self):
        q1 = self.quote(now=T0)
        q_eth = self.quote(network="ethereum", asset="ETH", now=T0)
        self.assertNotEqual(q_eth.pk, q1.pk)
        self.assertEqual(q_eth.amount, 40_000_000_000_000_000)  # 100 EUR at 2500 EUR per ETH
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="part", user=person())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "partially_paid")
        q2 = self.quote(now=T0 + datetime.timedelta(seconds=10))
        self.assertNotEqual(q2.pk, q1.pk)
        self.assertEqual(q2.invoice_amount_minor, 6000)


class RefusalTests(QuoteBase):
    def code(self, *args, **kw):
        with self.assertRaises(QuoteError) as ctx:
            create_quote(*args, **kw)
        return ctx.exception.code

    def test_method_not_offered_refused(self):
        bank_only = self.sent(offered_methods=[f"bank:{self.bank.pk}"])
        self.assertEqual(self.code(bank_only, self.acct, "arbitrum", "USDC"), "method_not_offered")
        other = Organization.objects.create(slug="us")
        theirs = RailAccount.objects.create(org=other, kind="evm", label="EVM", address=EVM,
                                            networks=["arbitrum"], assets=["USDC"])
        named = self.sent(offered_methods=[f"bank:{self.bank.pk}", f"rail:{theirs.pk}"])
        self.assertEqual(self.code(named, theirs, "arbitrum", "USDC"), "method_not_offered")
        RailAccount.objects.filter(pk=self.acct.pk).update(active=False)
        self.assertEqual(self.code(self.invoice, self.acct, "arbitrum", "USDC"), "method_not_offered")
        self.assertFalse(Quote.objects.exists())
        self.price_in.assert_not_called()  # refused before asking anybody for a rate

    def test_network_and_asset_must_be_enabled(self):
        for network, asset in (("base", "USDC"),      # network not chosen for this account
                               ("arbitrum", "USDT"),  # asset not chosen
                               ("polygon", "ETH"),    # not on that network
                               ("tron", "USDT"),      # another family
                               ("nowhere", "USDC"),
                               ("bitcoin", "BTC")):
            self.assertEqual(self.code(self.invoice, self.acct, network, asset), "asset_not_enabled", network)
        self.assertFalse(Quote.objects.exists())

    def test_quote_refused_for_draft_void_and_paid(self):
        draft = create_invoice(self.org, self.contact, currency="EUR",
                               lines=[{"name": "S", "quantity": 1, "unit_price_minor": 10000}])[0]
        self.assertEqual(self.code(draft, self.acct, "arbitrum", "USDC"), "not_payable")
        voided = void_invoice(self.sent(), "mistake", None)
        self.assertEqual(self.code(voided, self.acct, "arbitrum", "USDC"), "not_payable")
        paid = self.sent()
        record_manual_payment(paid, amount_minor=10000, rail="bank", note="", user=person())
        self.assertEqual(self.code(paid, self.acct, "arbitrum", "USDC"), "not_payable")
        overpaid = self.sent()
        record_manual_payment(overpaid, amount_minor=12000, rail="bank", note="", user=person())
        self.assertEqual(self.code(overpaid, self.acct, "arbitrum", "USDC"), "not_payable")
        self.assertFalse(Quote.objects.exists())

    def test_quotable_states(self):
        declared = self.sent()
        declare_payment(declared, note="paid yesterday")
        self.assertEqual(declared.status, "declared")
        self.assertEqual(self.quote(declared, now=T0).invoice_amount_minor, 10000)
        overdue = self.sent()
        Invoice.objects.filter(pk=overdue.pk).update(status="overdue")
        self.assertEqual(self.quote(overdue, now=T0).state, "open")

    def test_nothing_due(self):
        # confirmed money not yet applied: the status still says sent, but nothing is owed
        Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=10000,
                               received_amount="100", state="confirmed", confirmed_at=timezone.now())
        self.assertEqual(self.code(self.invoice, self.acct, "arbitrum", "USDC"), "nothing_due")

    def test_rate_errors_map_to_codes(self):
        with self.assertLogs("apps.quotes.services", "WARNING") as logs:
            for exc, code in ((fx.RatesUnavailable("down"), "rates_unavailable"), (fx.RatesDiverge(150), "rates_diverge")):
                self.price_in.side_effect = exc
                self.assertEqual(self.code(self.invoice, self.acct, "arbitrum", "USDC"), code)
        self.assertFalse(Quote.objects.exists())
        # codes and error type names only: no amount, no address, no exception text
        self.assertEqual([r.getMessage() for r in logs.records], [
            "quote refused: rates_unavailable (RatesUnavailable)", "quote refused: rates_diverge (RatesDiverge)"])

    def test_quote_error_codes_are_known(self):
        self.assertEqual(QuoteError("nothing_due").code, "nothing_due")
        self.assertEqual(set(services.QUOTE_ERROR_CODES), {
            "not_payable", "method_not_offered", "asset_not_enabled", "nothing_due", "rates_unavailable",
            "rates_diverge", "no_free_amount", "too_many_quotes", "rail_unavailable"})
        with self.assertRaises(ValueError):
            QuoteError("oops")

    def test_rates_are_fetched_before_the_invoice_lock(self):
        events = []
        self.price_in.side_effect = lambda a, c: (events.append("rate"), fake_price_in(a, c))[1]
        real = services.lock_invoice
        with mock.patch.object(services, "lock_invoice", side_effect=lambda pk: (events.append("lock"), real(pk))[1]):
            self.quote(now=T0)
        self.assertEqual(events, ["rate", "rate", "lock"])


class FakeGateway:
    def __init__(self, account, events, fail=None):
        self.account, self.events, self.fail = account, events, fail

    def create_target(self, quote):
        self.events.append("target")
        if self.fail:
            raise self.fail
        quote.external_id = f"btcpay:s1:inv{len(self.events)}"
        quote.amount_base_units = "163300"
        quote.payment_uri = "bitcoin:bc1qexample?amount=0.001633"
        quote.checkout_url = "https://btcpay.example/i/abc"
        quote.rate, quote.rate_source = "61234.50", "btcpay"

    def poll(self, quote):
        pass


class GatewayQuoteTests(QuoteBase):
    def setUp(self):
        super().setUp()
        self.btcpay = RailAccount.objects.create(
            org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
            config={"url": "https://btcpay.example", "store_id": "s1"}, credentials={"api_key": "k"})
        self.ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})
        self.invoice = self.sent()
        self.events = []

    def gateways(self, fail=None):
        return mock.patch.dict(registry._FACTORIES, {
            "btcpay": lambda acct: FakeGateway(acct, self.events, fail),
            "lightning": lambda acct: FakeGateway(acct, self.events, fail),
        })

    def test_no_gateway_registered_is_rail_unavailable(self):
        with mock.patch.dict(registry._FACTORIES, clear=True):  # whatever the app registers at start-up
            with self.assertRaises(QuoteError) as ctx:
                registry.for_account(self.btcpay)
            self.assertEqual(ctx.exception.code, "rail_unavailable")
            with self.assertRaises(QuoteError) as ctx:
                create_quote(self.invoice, self.btcpay, "bitcoin", "BTC")
            self.assertEqual(ctx.exception.code, "rail_unavailable")
        self.assertFalse(Quote.objects.exists())

    def test_register_refuses_chain_kinds(self):
        with self.assertRaises(ValueError):
            registry.register("evm", lambda acct: None)

    def test_gateway_fills_the_quote_outside_the_lock(self):
        real = services.lock_invoice
        with self.gateways(), mock.patch.object(
                services, "lock_invoice", side_effect=lambda pk: (self.events.append("lock"), real(pk))[1]):
            q = create_quote(self.invoice, self.btcpay, "bitcoin", "BTC", now=T0)
        self.assertEqual(self.events, ["target", "lock"])
        self.price_in.assert_not_called()  # the gateway prices its own target
        q = Quote.objects.get(pk=q.pk)
        self.assertEqual((q.address, q.external_id, q.amount_base_units), ("", "btcpay:s1:inv1", "163300"))
        self.assertEqual((q.min_accept_base_units, q.ceiling_base_units), ("163300", "163300"))
        self.assertEqual((q.checkout_url, q.rate, q.rate_source), ("https://btcpay.example/i/abc", "61234.50", "btcpay"))
        self.assertEqual((q.invoice_amount_minor, q.state, q.expires_at), (10000, "open", T0 + datetime.timedelta(seconds=1800)))
        # reused like any other quote; a refresh asks the gateway again
        with self.gateways():
            self.assertEqual(create_quote(self.invoice, self.btcpay, "bitcoin", "BTC", now=T0).pk, q.pk)
            fresh = create_quote(self.invoice, self.btcpay, "bitcoin", "BTC", now=T0, refresh=True)
        self.assertEqual(self.state(q), "cancelled")
        self.assertNotEqual(fresh.external_id, q.external_id)

    def test_gateway_network_must_match_the_asset(self):
        with self.gateways():
            for account, network, asset in ((self.btcpay, "monero", "BTC"), (self.btcpay, "zcash", "ZEC"),
                                            (self.btcpay, "arbitrum", "USDC"), (self.ln, "bitcoin", "BTC"),
                                            (self.ln, "lightning", "XMR")):
                with self.assertRaises(QuoteError, msg=(network, asset)) as ctx:
                    create_quote(self.invoice, account, network, asset)
                self.assertEqual(ctx.exception.code, "asset_not_enabled")
            self.assertEqual(create_quote(self.invoice, self.btcpay, "monero", "XMR").network, "monero")
            self.assertEqual(create_quote(self.invoice, self.ln, "lightning", "BTC").network, "lightning")

    def test_gateway_failures_map_to_codes(self):
        for fail, code in ((http.Unavailable("btcpay", "timeout"), "rail_unavailable"),
                           (http.Refused("btcpay", 403), "rail_unavailable"),
                           (fx.RatesUnavailable("x"), "rates_unavailable"), (fx.RatesDiverge(300), "rates_diverge")):
            with self.gateways(fail=fail), self.assertLogs("apps.quotes.services", "WARNING") as logs:
                with self.assertRaises(QuoteError) as ctx:
                    create_quote(self.invoice, self.ln, "lightning", "BTC")
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual([r.getMessage() for r in logs.records],
                             [f"quote refused: {code} ({type(fail).__name__})"])
        self.assertFalse(Quote.objects.exists())

    def test_gateway_quote_refused_when_the_invoice_closed_meanwhile(self):
        def void_meanwhile(acct):
            gw = FakeGateway(acct, self.events)
            original = gw.create_target

            def target(quote):
                original(quote)
                void_invoice(self.invoice, "gone", None)
            gw.create_target = target
            return gw

        with mock.patch.dict(registry._FACTORIES, {"btcpay": void_meanwhile}), self.assertRaises(QuoteError) as ctx:
            create_quote(self.invoice, self.btcpay, "bitcoin", "BTC")
        self.assertEqual(ctx.exception.code, "not_payable")
        self.assertFalse(Quote.objects.exists())


class QuoteTimeHealthTests(GatewayQuoteTests):
    """A quote that could not be made because a source could not answer says so in the watcher
    health, under the gateway's own name or `fx`; a quote that was made clears it (m5)."""

    def health(self, name):
        return WatcherHealth.objects.filter(name=name).values_list("consecutive_failures", "last_error_kind",
                                                                   "failing_since").first()

    def refuse(self, account, network, asset, code):
        with self.assertLogs("apps.quotes.services", "WARNING"), self.assertRaises(QuoteError) as ctx:
            create_quote(self.invoice, account, network, asset, refresh=True)
        self.assertEqual(ctx.exception.code, code)

    def test_exchange_rates_that_cannot_answer_are_recorded_under_fx(self):
        self.price_in.side_effect = fx.RatesUnavailable("kraken unavailable")
        self.refuse(self.acct, "arbitrum", "USDC", "rates_unavailable")
        failures, kind, since = self.health("fx")
        self.assertEqual((failures, kind), (1, "kraken unavailable"))
        self.assertIsNotNone(since)
        self.price_in.side_effect = fx.RatesDiverge(150)  # they answered, and disagree: not "could not look"
        self.refuse(self.acct, "arbitrum", "USDC", "rates_diverge")
        self.assertEqual(self.health("fx")[0], 1)
        self.price_in.side_effect = fake_price_in
        create_quote(self.invoice, self.acct, "arbitrum", "USDC")
        self.assertEqual(self.health("fx")[:2], (0, ""))
        self.assertIsNone(self.health("fx")[2])

    def test_a_gateway_that_cannot_answer_is_recorded_under_its_own_name(self):
        name = f"btcpay:{self.btcpay.pk}"
        with self.gateways(fail=http.Unavailable("btcpay", "timeout")):
            self.refuse(self.btcpay, "bitcoin", "BTC", "rail_unavailable")
        self.assertEqual(self.health(name)[:2], (1, "timeout"))
        with self.gateways():
            create_quote(self.invoice, self.btcpay, "bitcoin", "BTC")
        self.assertEqual(self.health(name)[:2], (0, ""))
        self.assertIsNone(self.health(name)[2])
        # Lightning: the node's own name, and the exchange rate it is priced at under fx.
        with self.gateways(fail=http.Unavailable("lightning", "nwc")):
            self.refuse(self.ln, "lightning", "BTC", "rail_unavailable")
        self.assertEqual(self.health(f"lightning:{self.ln.pk}")[:2], (1, "nwc"))
        with self.gateways(fail=fx.RatesUnavailable("coinbase unavailable")):
            self.refuse(self.ln, "lightning", "BTC", "rates_unavailable")
        self.assertEqual(self.health("fx")[:2], (1, "coinbase unavailable"))

    def test_lightning_rates_failure_reaches_the_quote_as_rates_unavailable(self):
        acct = RailAccount.objects.get(pk=self.ln.pk)
        acct.config, acct.credentials = {"backend": "nwc"}, {"nwc_url": "nostr+walletconnect://x"}
        acct.save()
        self.price_in.side_effect = fx.RatesUnavailable("kraken unavailable")
        self.refuse(acct, "lightning", "BTC", "rates_unavailable")
        self.assertEqual(self.health("fx")[:2], (1, "kraken unavailable"))


class LifecycleTests(QuoteBase):
    def test_expired_quote_holds_amount_24h_then_released(self):
        q = self.quote(now=T0)
        self.assertEqual(expire_quotes(T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S)), 0)  # not yet past
        expired_at = T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S + 1)
        self.assertEqual(expire_quotes(expired_at), 1)
        q.refresh_from_db()
        self.assertEqual((q.state, q.closed_at), ("expired", expired_at))
        self.assertEqual(expire_quotes(expired_at), 0)
        # held: the next quote for the same amount gets the next suffix
        self.assertEqual(self.quote(self.sent(), now=expired_at).amount, K0 + STEP)
        held_until = expired_at + datetime.timedelta(seconds=QUOTE_AMOUNT_HOLD_S)
        self.assertEqual(release_quotes(held_until - datetime.timedelta(seconds=1)), 0)
        self.assertEqual(release_quotes(held_until + datetime.timedelta(seconds=1)), 1)
        self.assertEqual(self.state(q), "released")
        # No longer held, but matching still counts it as the first client's for a month (audit
        # finding MONEY-3): it is handed out only when nothing else is free...
        self.assertEqual(self.quote(self.sent(), now=held_until).amount, K0 + 2 * STEP)
        # ...and is free again once that month is over.
        forgotten = expired_at + datetime.timedelta(seconds=groups.RELEASED_MEMORY_S + 1)
        self.assertEqual(self.quote(self.sent(), now=forgotten).amount, K0)

    def test_release_takes_every_closed_state_and_never_an_open_quote(self):
        old = T0 - datetime.timedelta(days=3)
        other = self.sent()
        quotes = {s: self.hold(other, K0 + i * STEP, state=s, closed_at=old)
                  for i, s in enumerate(("expired", "cancelled", "paid"))}
        still_open = self.hold(other, K0 + 5 * STEP, issued_at=old, expires_at=old)
        self.assertEqual(release_quotes(T0), 3)
        self.assertEqual({s: self.state(q) for s, q in quotes.items()},
                         {"expired": "released", "cancelled": "released", "paid": "released"})
        self.assertEqual(self.state(still_open), "open")
        self.assertEqual(QUOTE_AMOUNT_HOLD_S, 86400)
        self.assertEqual(QUOTE_LIFETIME_S, 1800)

    def test_void_cancels_open_quotes(self):
        q1 = self.quote(now=T0)
        q2 = self.quote(network="ethereum", asset="ETH", now=T0)
        expired = self.hold(self.invoice, K0 + 9 * STEP, state="expired", closed_at=T0)
        void_invoice(self.invoice, "mistake", None)
        for q in (q1, q2):
            q.refresh_from_db()
            self.assertEqual(q.state, "cancelled")
            self.assertIsNotNone(q.closed_at)
        expired.refresh_from_db()
        self.assertEqual((expired.state, expired.closed_at), ("expired", T0))
        entry = AuditEntry.objects.get(action="quote.close")
        self.assertEqual(entry.system_detail, {"invoice": self.invoice.pk, "paid": None,
                                               "cancelled": sorted([q1.pk, q2.pk])})

    def test_close_quotes_marks_the_paid_one_and_cancels_only_open_ones(self):
        paid = self.quote(now=T0)
        other_open = self.quote(network="ethereum", asset="ETH", now=T0)
        expired = self.hold(self.invoice, K0 + 9 * STEP, state="expired", closed_at=T0)
        already_paid = self.hold(self.invoice, K0 + 8 * STEP, state="paid", closed_at=T0)
        unrelated = self.quote(self.sent(), now=T0)
        self.assertEqual(close_quotes(self.invoice, paid_quote=paid), 1)
        self.assertEqual(self.state(paid), "paid")
        self.assertIsNotNone(Quote.objects.get(pk=paid.pk).closed_at)
        self.assertEqual(self.state(other_open), "cancelled")
        self.assertEqual(Quote.objects.filter(pk=expired.pk).values_list("state", "closed_at").get(), ("expired", T0))
        self.assertEqual(Quote.objects.filter(pk=already_paid.pk).values_list("state", "closed_at").get(), ("paid", T0))
        self.assertEqual(self.state(unrelated), "open")
        self.assertEqual(close_quotes(self.invoice), 0)  # idempotent

    def test_close_quotes_marks_an_expired_quote_paid_but_never_a_released_one(self):
        month_ago = timezone.now() - datetime.timedelta(days=30)
        late = self.hold(self.invoice, K0, state="expired", closed_at=month_ago)
        close_quotes(self.invoice, paid_quote=late)
        late.refresh_from_db()
        self.assertEqual(late.state, "paid")
        self.assertGreater(late.closed_at, month_ago)  # held for 24 h from the payment
        gone = self.hold(self.invoice, K0 + STEP, state="released", closed_at=T0)
        close_quotes(self.invoice, paid_quote=gone)
        self.assertEqual(self.state(gone), "released")
        foreign = self.quote(self.sent(), now=T0)
        close_quotes(self.invoice, paid_quote=foreign)  # another invoice's quote is not this invoice's to close
        self.assertEqual(self.state(foreign), "open")

    def test_settlement_closes_quotes_partial_does_not(self):
        q = self.quote(now=T0)
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=person())
        self.assertEqual(self.state(q), "open")
        record_manual_payment(self.invoice, amount_minor=6000, rail="bank", note="", user=person())
        self.assertEqual(self.state(q), "cancelled")

    def test_expire_release_and_close_work_sealed(self):
        q_open = self.quote(now=T0)
        q_eth = self.quote(network="ethereum", asset="ETH", now=T0 + datetime.timedelta(hours=1))
        vault.seal()
        self.assertEqual(expire_quotes(T0 + datetime.timedelta(seconds=QUOTE_LIFETIME_S + 1)), 1)
        Quote.objects.filter(pk=q_open.pk).update(closed_at=T0)
        self.assertEqual(release_quotes(T0 + datetime.timedelta(days=2)), 1)
        Payment.objects.create(invoice_id=self.invoice.pk, rail="manual", asset="EUR", amount_minor=10000,
                               received_amount="0", state="confirmed", confirmed_at=timezone.now())
        self.assertEqual(apply_payments(self.invoice), "paid")
        self.assertEqual(self.state(q_eth), "cancelled")


class QuoteCapacityTests(QuoteBase):
    """Final review I3: public links must not exhaust the amount slots. One invoice holds at most
    `QUOTES_PER_INVOICE` unreleased quotes, and staff are told once per episode when an amount group
    has fewer than `FEW_FREE_AMOUNTS` of its 100 amounts free."""

    def amounts_watcher(self):
        return WatcherHealth.objects.filter(name=f"amounts:evm:USD:{self.acct.pk}").first()

    def test_an_invoice_holds_at_most_twenty_unreleased_quotes(self):
        """Residual R1: 20, not 5 (a client switching networks or refreshing a few times must not be shut
        out for a day), refused with its own code and an honest sentence, not "try again in a few minutes"."""
        self.assertEqual((services.QUOTES_PER_INVOICE, amounts.MAX_SUFFIX + 1), (20, 100))
        made = [self.quote(now=T0)]
        for i in range(19):
            made.append(self.quote(now=T0 + datetime.timedelta(seconds=i + 1), refresh=True))
        with self.assertRaises(QuoteError) as caught:
            self.quote(now=T0 + datetime.timedelta(seconds=30), refresh=True)
        self.assertEqual(caught.exception.code, "too_many_quotes")
        self.assertEqual(Quote.objects.filter(invoice=self.invoice).count(), 20)
        self.assertEqual(self.state(made[-1]), "open")  # the refused refresh cancelled nothing
        self.assertEqual(self.quote(now=T0 + datetime.timedelta(seconds=31)).pk, made[-1].pk)  # reuse still works
        for network, asset in (("ethereum", "USDC"), ("ethereum", "ETH")):  # another key is no way out
            with self.assertRaises(QuoteError):
                self.quote(network=network, asset=asset, now=T0 + datetime.timedelta(seconds=32))
        Quote.objects.filter(pk=made[0].pk).update(state="released")
        self.assertEqual(self.quote(now=T0 + datetime.timedelta(seconds=33), refresh=True).invoice_id, self.invoice.pk)

    def check(self):
        """The periodic capacity job (residual R2); a no-op where it does not exist yet."""
        from apps.rails import jobs

        getattr(jobs, "quote_capacity", lambda: None)()

    def test_fewer_than_twenty_free_amounts_is_one_staff_item_per_episode(self):
        """Residual R2: the worst window of the group is measured by a periodic job, so a quote at another
        price in the same group no longer ends the episode, and staff are told once per episode."""
        from apps.rails import jobs

        other = self.sent()
        for k in range(79):  # 79 held: this quote leaves 20 free, which is still enough
            self.hold(other, K0 + k * STEP, state="expired")
        self.quote(now=T0)
        self.check()
        self.assertIsNone(self.amounts_watcher())
        self.hold(other, K0 + 80 * STEP, state="expired")
        self.quote(self.sent(), now=T0)  # 82 held now: 18 free in that window
        self.check()
        watcher = self.amounts_watcher()
        self.assertIsNotNone(watcher.failing_since)
        self.assertEqual(watcher.last_error_kind, "few_free_amounts")
        self.assertIn("unique amounts", health_explanation(watcher.name).lower())
        self.quote(self.sent(total=50000), now=T0)  # another price in the same group: plenty free there
        self.assertIsNotNone(self.amounts_watcher().failing_since, "a quote at another price ended the episode")
        self.check()
        self.assertIsNotNone(self.amounts_watcher().failing_since)
        WatcherHealth.objects.filter(pk=watcher.pk).update(
            failing_since=timezone.now() - datetime.timedelta(seconds=jobs.WATCHER_ALERT_AFTER_S + 1))
        with self.settings(STAFF_NOTIFY_EMAILS=["staff@seller.example"]):
            from django.core import mail

            jobs._alert_failing_watchers()
            for total in (50000, 70000):  # quotes elsewhere in the group, and the job, between two alert passes
                self.quote(self.sent(total=total), now=T0)
                self.check()
            jobs._alert_failing_watchers()
            letters = [m for m in mail.outbox if f"amounts:evm:USD:{self.acct.pk}" in m.body]
        self.assertEqual(len(letters), 1)
        self.assertNotIn(EVM, letters[0].body.lower())  # the wallet by its id, never its address
        from apps.staff.views import watcher_words

        self.assertEqual(watcher_words(watcher.name), "Unique amounts for USD stablecoins on “EVM”")
        Quote.objects.filter(invoice=other).update(state="released", closed_at=T0 - datetime.timedelta(days=40))
        self.check()  # every window has enough free again: the episode ends
        self.assertIsNone(self.amounts_watcher().failing_since)

    def test_an_allocation_never_writes_the_capacity_watcher(self):
        other = self.sent()
        for k in range(85):
            self.hold(other, K0 + k * STEP, state="expired")
        self.quote(now=T0)
        self.assertIsNone(self.amounts_watcher())
        self.check()
        self.assertEqual(self.amounts_watcher().last_error_kind, "few_free_amounts")

    def test_a_full_group_is_an_item_too_and_released_groups_recover(self):
        other = self.sent()
        for k in range(amounts.MAX_SUFFIX + 1):
            self.hold(other, K0 + k * STEP, state="expired")
        with self.assertRaises(QuoteError):
            self.quote(now=T0)
        self.check()
        self.assertEqual(self.amounts_watcher().last_error_kind, "few_free_amounts")
        Quote.objects.filter(invoice=other).update(state="released")
        self.check()  # no unreleased quote left in the group at all
        self.assertIsNone(self.amounts_watcher().failing_since)


def health_explanation(name):
    from apps.rails import health

    return health.explanation(name, "few_free_amounts") or ""
