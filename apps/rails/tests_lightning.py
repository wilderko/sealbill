import asyncio
import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.services import INVOICE_PLAIN_FIELDS, create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.payments.models import Payment
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails import fx, http
from apps.rails.gateways import lightning, registry
from apps.rails.gateways.lightning import nwc
from apps.rails.gateways.lightning.base import FractionalSats
from apps.rails.gateways.lightning.cln import ClnBackend
from apps.rails.gateways.lightning.lnbits import LnbitsBackend
from apps.rails.gateways.lightning.lnd import LndBackend
from apps.rails.gateways.lightning.nwc import NwcBackend
from apps.rails.models import RailAccount, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

HASH = "ab" * 32
BOLT = "lnbc1000n1pexample"


class FakeNwc:
    """Stands in for `NostrWalletConnect`: only the two read-side calls exist."""

    def __init__(self):
        self.made, self.looked, self.state = [], [], "PENDING"
        self.amount = 0

    async def make_invoice(self, req):
        self.made.append(req)
        return SimpleNamespace(invoice=BOLT, payment_hash=HASH)

    async def lookup_invoice(self, req):
        self.looked.append(req)
        return SimpleNamespace(state=SimpleNamespace(name=self.state), amount=self.amount, payment_hash=HASH)


class Base(UnsealedVaultMixin, TestCase):
    backend = "nwc"

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.fake = FakeNwc()
        p = mock.patch.object(nwc, "_run", side_effect=lambda url, what: asyncio.run(what(self.fake)))
        p.start()
        self.addCleanup(p.stop)
        self.acct = RailAccount.objects.create(
            org=self.org, kind="lightning", label="LN", assets=["BTC"],
            config={"backend": "nwc", "url": ""}, credentials={"nwc_url": "nostr+walletconnect://secret"})
        contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján Novák", preferred_language="sk")
        inv = create_invoice(self.org, contact, currency="EUR", title="T", notes="N",
                             lines=[{"name": "S", "quantity": 1, "unit_price_minor": 10000}])[0]
        self.invoice = send_invoice(inv, email_client=False)[0]

    def unsaved(self, currency_invoice=None):
        plain = Invoice.objects.only(*INVOICE_PLAIN_FIELDS).get(pk=(currency_invoice or self.invoice).pk)
        return Quote(invoice=plain, rail_account=self.acct, network="lightning", asset="BTC", address="",
                     invoice_amount_minor=10000, issued_at=timezone.now(),
                     expires_at=timezone.now() + datetime.timedelta(hours=3))

    def quote(self, sats=100000, ext=HASH, **kw):
        return Quote.objects.create(
            invoice=self.invoice, rail_account=self.acct, network="lightning", asset="BTC", address="",
            amount_base_units=str(sats), min_accept_base_units=str(sats), ceiling_base_units=str(sats),
            invoice_amount_minor=10000, issued_at=timezone.now(), external_id=ext, bolt11=BOLT,
            expires_at=timezone.now() + datetime.timedelta(minutes=30), **kw)

    def poll(self, quote):
        registry.for_account(RailAccount.objects.get(pk=self.acct.pk)).poll(Quote.objects.get(pk=quote.pk))


class NwcTests(Base):
    def test_nwc_accepted_is_not_settled(self):
        backend = NwcBackend("x")
        self.fake.state, self.fake.amount = "ACCEPTED", 100_000_000
        self.assertEqual(backend.invoice_state(HASH), ("open", 0))
        self.fake.state = "SETTLED"
        self.assertEqual(backend.invoice_state(HASH), ("settled", 100_000_000))
        self.fake.state = "EXPIRED"
        self.assertEqual(backend.invoice_state(HASH), ("expired", 0))

    def test_nwc_refuses_fractional_sats(self):
        with self.assertRaises(FractionalSats):
            NwcBackend("x").create_invoice(1500, "d", 1800)
        self.assertEqual(self.fake.made, [])

    def test_no_pay_method_exists(self):
        for cls in (NwcBackend, LndBackend, ClnBackend, LnbitsBackend, lightning.LightningGateway):
            names = [n for n in dir(cls) if "pay" in n.lower().replace("payment_hash", "") or "send" in n.lower()]
            self.assertEqual(names, [], cls.__name__)
        self.assertEqual([n for n in dir(nwc) if "pay" in n.lower()], [])


class GatewayTests(Base):
    def price(self, rate="50000"):
        return mock.patch("apps.rails.fx.price_in", return_value=fx.RatePair(Decimal(rate), Decimal(rate), "test"))

    def test_create_target_whole_sats_from_eur(self):
        q = self.unsaved()
        with self.price("60000"):
            registry.for_account(self.acct).create_target(q)
        # 100.00 EUR / 60000 = 0.00166666..., rounded up to 166667 sats
        self.assertEqual(q.amount_base_units, "166667")
        self.assertEqual(self.fake.made[0].amount, 166_667_000)
        self.assertEqual(self.fake.made[0].expiry, 1800)
        self.assertEqual((q.external_id, q.bolt11, q.payment_uri), (HASH, BOLT, f"lightning:{BOLT}"))
        self.assertEqual((q.rate, q.rate_second, q.rate_source), ("60000", "60000", "test"))
        desc = self.fake.made[0].description
        self.assertEqual(desc, f"eu {self.invoice.number or 'P' + str(self.invoice.pk)}")
        self.assertNotIn("Ján", desc)

    def test_sats_round_up_not_to_nearest(self):
        q = self.unsaved()
        with self.price("70000"):  # 100 / 70000 BTC = 142857.14 sats
            registry.for_account(self.acct).create_target(q)
        self.assertEqual(q.amount_base_units, "142858")

    def test_expires_at_is_shortened_to_thirty_minutes(self):
        q = self.unsaved()
        before = timezone.now()
        with self.price():
            registry.for_account(self.acct).create_target(q)
        self.assertLessEqual(q.expires_at, timezone.now() + datetime.timedelta(minutes=30))
        self.assertGreater(q.expires_at, before + datetime.timedelta(minutes=29))
        short = self.unsaved()
        short.expires_at = timezone.now() + datetime.timedelta(minutes=5)
        keep = short.expires_at
        with self.price():
            registry.for_account(self.acct).create_target(short)
        self.assertEqual(short.expires_at, keep)  # never lengthened

    def test_open_and_unknown_leave_quote_open(self):
        q = self.quote()
        for state in ("PENDING", "ACCEPTED", "FAILED"):
            self.fake.state, self.fake.amount = state, 100_000_000
            self.poll(q)
            self.assertEqual(Quote.objects.get(pk=q.pk).state, "open", state)
        self.assertEqual(Payment.objects.count(), 0)

    def test_btc_invoice_uses_remainder_as_sats(self):
        q = self.unsaved()
        q.invoice.currency = "BTC"
        q.invoice_amount_minor = 123456
        with mock.patch("apps.rails.fx.price_in", side_effect=AssertionError("no rate needed")):
            registry.for_account(self.acct).create_target(q)
        self.assertEqual((q.amount_base_units, q.rate), ("123456", "1"))
        self.assertEqual(self.fake.made[0].amount, 123_456_000)

    def test_rates_errors_are_quote_errors(self):
        from apps.quotes.services import create_quote

        for exc, code in ((fx.RatesUnavailable("x"), "rates_unavailable"), (fx.RatesDiverge(150), "rates_diverge")):
            with mock.patch("apps.rails.fx.price_in", side_effect=exc):
                with self.assertRaises(type(exc)):  # the gateway passes them on; the quote maps them
                    registry.for_account(self.acct).create_target(self.unsaved())
                with self.assertLogs("apps.quotes.services", "WARNING"), self.assertRaises(QuoteError) as cm:
                    create_quote(self.invoice, self.acct, "lightning", "BTC")
            self.assertEqual(cm.exception.code, code)

    def test_sealed_vault_refuses_quote(self):
        acct = RailAccount.objects.only("id", "kind", "config").get(pk=self.acct.pk)
        vault.seal()
        with self.assertRaises(QuoteError) as cm:
            registry.for_account(acct)
        self.assertEqual(cm.exception.code, "rail_unavailable")

    def test_settled_records_confirmed_payment_once(self):
        q = self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        self.poll(q)
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.amount_minor, p.network, p.rail), ("confirmed", 10000, "lightning", "lightning"))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

    def test_overpaid_confirms_at_invoice_amount_with_refund(self):
        q = self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 100_500_000
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.amount_minor, p.refund_due_base_units), ("confirmed", 10000, "500"))

    def test_overpaid_beyond_the_ceiling_goes_to_review(self):
        q = self.quote()  # 100000 sats due; the ceiling is 10 % over
        self.fake.state, self.fake.amount = "SETTLED", 110_000_000
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.refund_due_base_units), ("confirmed", "10000"))  # at the ceiling
        Payment.objects.all().delete()
        Quote.objects.filter(pk=q.pk).update(state="open", closed_at=None)
        Invoice.objects.filter(pk=self.invoice.pk).update(status="sent", paid_at=None)
        self.fake.amount = 110_001_000
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.review_reason, p.amount_minor, p.refund_due_base_units),
                         ("needs_review", "overpaid_beyond_ceiling", 10000, "10001"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")

    def test_tiny_overpayment_stores_no_refund(self):
        q = self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 100_000_999
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.refund_due_base_units), ("confirmed", ""))

    def test_underpaid_credit_floors(self):
        q = self.quote()  # 100000 sats due; 33333333 msat is 0.333333 of 10000 = 3333.33
        self.fake.state, self.fake.amount = "SETTLED", 33_333_333
        self.poll(q)
        self.assertEqual(Payment.objects.get(external_id=f"ln:{HASH}").amount_minor, 3333)

    def test_underpaid_goes_to_review_proportionally(self):
        q = self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 40_000_000
        self.poll(q)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.amount_minor), ("needs_review", 4000))

    def test_expired_unpaid_expires_quote(self):
        q = self.quote()
        self.fake.state = "EXPIRED"
        self.poll(q)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "expired")
        self.assertEqual(Payment.objects.count(), 0)


class HttpBackendTests(Base):
    def test_lnd_cln_lnbits_request_shapes(self):
        calls = []

        def post(url, payload, *, headers=None, service, timeout=10, guard=None):
            calls.append(("POST", url, payload, headers))
            if url.endswith("/v1/invoices"):  # LND
                return {"r_hash": "q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s=", "payment_request": BOLT}
            if url.endswith("/v1/invoice"):  # CLN
                return {"bolt11": BOLT, "payment_hash": HASH}
            if url.endswith("/v1/listinvoices"):
                return {"invoices": [{"status": "paid", "amount_msat": 5000, "amount_received_msat": 6000}]}
            return {"payment_request": BOLT, "payment_hash": HASH}  # LNbits

        def get(url, *, params=None, headers=None, service, timeout=10, guard=None):
            calls.append(("GET", url, None, headers))
            if "/v1/invoice/" in url:
                return {"state": "SETTLED", "amt_paid_msat": "7000"}
            return {"paid": True, "details": {"amount": 8000}}

        with mock.patch("apps.rails.http.post_json", side_effect=post), mock.patch("apps.rails.http.get_json", side_effect=get):
            lnd = LndBackend("https://lnd.example:8080/", "MAC")
            self.assertEqual(lnd.create_invoice(5000, "memo", 1800), (BOLT, "ab" * 32))
            self.assertEqual(lnd.invoice_state(HASH), ("settled", 7000))
            cln = ClnBackend("https://cln.example", "RUNE")
            self.assertEqual(cln.create_invoice(5000, "memo", 1800), (BOLT, HASH))
            self.assertEqual(cln.invoice_state(HASH), ("settled", 6000))
            lnb = LnbitsBackend("https://lnb.example", "IKEY")
            self.assertEqual(lnb.create_invoice(5000, "memo", 1800), (BOLT, HASH))
            self.assertEqual(lnb.invoice_state(HASH), ("settled", 8000))
        by = {(m, u): (p, h) for m, u, p, h in calls}
        p, h = by[("POST", "https://lnd.example:8080/v1/invoices")]
        self.assertEqual((p, h["Grpc-Metadata-macaroon"]), ({"value": "5", "memo": "memo", "expiry": "1800"}, "MAC"))
        self.assertIn(("GET", f"https://lnd.example:8080/v1/invoice/{HASH}"), by)
        p, h = by[("POST", "https://cln.example/v1/invoice")]
        self.assertEqual((p["amount_msat"], p["description"], p["expiry"], h["Rune"]), (5000, "memo", 1800, "RUNE"))
        self.assertTrue(p["label"].startswith("q"))
        self.assertEqual(by[("POST", "https://cln.example/v1/listinvoices")][0], {"payment_hash": HASH})
        p, h = by[("POST", "https://lnb.example/api/v1/payments")]
        self.assertEqual((p, h["X-Api-Key"]), ({"out": False, "amount": 5, "memo": "memo", "expiry": 1800}, "IKEY"))
        self.assertIn(("GET", f"https://lnb.example/api/v1/payments/{HASH}"), by)

    def test_only_settled_is_settled(self):
        lnd, cln, lnb = LndBackend("https://x", "M"), ClnBackend("https://x", "R"), LnbitsBackend("https://x", "K")
        now = int(timezone.now().timestamp())
        cases = [
            (lnd, "get", {"state": "ACCEPTED", "amt_paid_msat": "9000"}, ("unknown", 0)),
            (lnd, "get", {"state": "OPEN", "creation_date": str(now), "expiry": "1800"}, ("open", 0)),
            (lnd, "get", {"state": "OPEN", "creation_date": str(now - 4000), "expiry": "1800"}, ("expired", 0)),
            (lnd, "get", {"state": "CANCELED"}, ("expired", 0)),
            (cln, "post", {"invoices": [{"status": "unpaid", "amount_msat": 5000}]}, ("open", 0)),
            (cln, "post", {"invoices": [{"status": "expired", "amount_msat": 5000}]}, ("expired", 0)),
            (cln, "post", {"invoices": []}, ("unknown", 0)),
            (lnb, "get", {"paid": False, "details": {"amount": 5000}}, ("open", 0)),
        ]
        for backend, verb, answer, want in cases:
            with mock.patch(f"apps.rails.http.{verb}_json", return_value=answer):
                self.assertEqual(backend.invoice_state(HASH), want, (type(backend).__name__, answer))

    def test_garbage_is_unavailable(self):
        with mock.patch("apps.rails.http.get_json", return_value={"nope": 1}):
            with self.assertRaises(http.Unavailable):
                LndBackend("https://x", "M").invoice_state(HASH)

    def test_factory_selects_backend(self):
        acct = RailAccount.objects.create(org=self.org, kind="lightning", label="L2", assets=["BTC"],
                                          config={"backend": "lnd", "url": "https://lnd.example"},
                                          credentials={"macaroon": "M"})
        self.assertIsInstance(registry.for_account(acct).backend, LndBackend)


class PollTests(Base):
    def test_poll_skipped_while_sealed(self):
        self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        vault.seal()
        self.assertEqual(lightning.lightning_poll(), 0)
        self.assertEqual(self.fake.looked, [])
        self.assertFalse(WatcherHealth.objects.exists())

    def test_poll_confirms_and_records_health(self):
        q = self.quote()
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        self.assertEqual(lightning.lightning_poll(), 1)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")
        self.assertTrue(WatcherHealth.objects.filter(name=f"lightning:{self.acct.pk}").exists())

    def test_unreachable_node_changes_nothing(self):
        q = self.quote()
        with mock.patch.object(nwc, "_run", side_effect=http.Unavailable("lightning", "nwc")):
            self.assertEqual(lightning.lightning_poll(), 0)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")
        self.assertEqual(Payment.objects.count(), 0)

    def test_cancelled_quote_polled_for_24h(self):
        now = timezone.now()
        recent = self.quote(state="cancelled", closed_at=now - datetime.timedelta(hours=1))
        old = self.quote(ext="old", state="cancelled", closed_at=now - datetime.timedelta(days=2))
        Quote.objects.filter(pk=old.pk).update(expires_at=now - datetime.timedelta(days=2),
                                               read_at=now - datetime.timedelta(days=1))  # read after expiry
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        self.assertEqual(lightning.lightning_poll(), 1)
        self.assertEqual(Quote.objects.get(pk=recent.pk).state, "paid")
        self.assertEqual(Quote.objects.get(pk=old.pk).state, "cancelled")

    def test_unavailable_records_health(self):
        self.quote()
        with mock.patch.object(nwc, "_run", side_effect=http.Unavailable("lightning", "nwc")):
            lightning.lightning_poll()
        h = WatcherHealth.objects.get(name=f"lightning:{self.acct.pk}")
        self.assertEqual((h.consecutive_failures, h.last_error_kind), (1, "nwc"))
        self.assertIsNotNone(h.failing_since)

    def test_dead_hub_is_asked_three_times_then_the_pass_stops(self):
        for i in range(5):
            self.quote(ext=f"h{i}")
        with mock.patch.object(nwc, "_run", side_effect=http.Unavailable("lightning", "nwc")) as run:
            self.assertEqual(lightning.lightning_poll(), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(WatcherHealth.objects.get(name=f"lightning:{self.acct.pk}").consecutive_failures, 1)

    def test_one_failing_quote_does_not_hide_a_later_paid_one(self):
        self.quote(ext="bad")
        good = self.quote(ext="good")
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        real_state = lightning.registry.for_account

        class Gateway:
            def __init__(self, inner):
                self.inner = inner

            def poll(self, quote):
                if quote.external_id == "bad":
                    raise http.Unavailable("lightning", "nwc")
                return self.inner.poll(quote)

        with mock.patch.object(lightning.registry, "for_account", side_effect=lambda a: Gateway(real_state(a))):
            self.assertEqual(lightning.lightning_poll(), 1)
        self.assertEqual(Quote.objects.get(pk=good.pk).state, "paid")
        self.assertEqual(WatcherHealth.objects.get(name=f"lightning:{self.acct.pk}").consecutive_failures, 1)

    def test_three_failing_quotes_cannot_hide_a_paid_one_for_good(self):
        """The pass stops at the third failure in a row, and quotes run in pk order: three
        lookups that always fail at the front used to hide every later quote for ever."""
        for ext in ("bad1", "bad2", "bad3"):
            self.quote(ext=ext)
        good = self.quote(ext="good")
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        real_state = lightning.registry.for_account
        asked = []

        class Gateway:
            def __init__(self, inner):
                self.inner = inner

            def poll(self, quote):
                asked.append(quote.external_id)
                if quote.external_id.startswith("bad"):
                    raise http.Unavailable("lightning", "nwc")
                return self.inner.poll(quote)

        start = timezone.now()
        passes = 0
        with mock.patch.object(lightning.registry, "for_account", side_effect=lambda a: Gateway(real_state(a))):
            while Quote.objects.get(pk=good.pk).state != "paid" and passes < 4:
                now = start + datetime.timedelta(seconds=30 * passes)
                with mock.patch.object(timezone, "now", return_value=now):
                    asked.clear()
                    lightning.lightning_poll()
                self.assertLessEqual(len([a for a in asked if a.startswith("bad")]), 3)
                passes += 1
        self.assertEqual(Quote.objects.get(pk=good.pk).state, "paid")

    def test_old_closed_quotes_are_not_polled(self):
        q = self.quote()
        Quote.objects.filter(pk=q.pk).update(state="expired", expires_at=timezone.now() - datetime.timedelta(days=2),
                                             read_at=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(lightning.lightning_poll(), 0)
        Quote.objects.filter(pk=q.pk).update(check_soon=True)
        self.assertEqual(lightning.lightning_poll(), 1)


class StrandedQuoteTests(Base):
    """A Lightning quote not read since it expired is polled whatever its age and never released."""

    def test_settled_while_nobody_could_look_is_still_found(self):
        from apps.quotes import services as quotes

        long_ago = timezone.now() - datetime.timedelta(hours=25)
        q = self.quote(state="expired", closed_at=long_ago)  # the hub was down, or the vault sealed, all day
        Quote.objects.filter(pk=q.pk).update(expires_at=long_ago)
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)
        self.fake.state, self.fake.amount = "SETTLED", 100_000_000
        self.assertEqual(lightning.lightning_poll(), 1)
        p = Payment.objects.get(external_id=f"ln:{HASH}")
        self.assertEqual((p.state, p.amount_minor), ("confirmed", 10000))
        self.assertEqual(Invoice.objects.filter(pk=self.invoice.pk).values_list("status", flat=True).get(), "paid")

    def test_an_unknown_answer_is_not_a_read(self):
        from apps.quotes import services as quotes

        long_ago = timezone.now() - datetime.timedelta(hours=25)
        q = self.quote(state="expired", closed_at=long_ago)
        Quote.objects.filter(pk=q.pk).update(expires_at=long_ago)
        self.fake.state = "SOMETHING_NEW"
        self.assertEqual(lightning.lightning_poll(), 1)
        self.assertIsNone(Quote.objects.get(pk=q.pk).read_at)
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)
        self.fake.state = "EXPIRED"
        lightning.lightning_poll()
        self.assertIsNotNone(Quote.objects.get(pk=q.pk).read_at)
        self.assertEqual(quotes.release_quotes(timezone.now()), 1)


class NwcBudgetTests(TestCase):
    """Inside a web request the NWC wait is capped by `http.request_budget`, like every HTTP call."""

    def fake_sdk(self):
        nwc_client = mock.Mock(spec=[])  # no `client`: nothing to shut down
        self.enterContext(override_settings(RAILS_HTTP_ENABLED=True))
        self.enterContext(mock.patch("nostr_sdk.NostrWalletConnectUri.parse", return_value=object()))
        self.enterContext(mock.patch("nostr_sdk.NostrWalletConnect", return_value=nwc_client))

    def test_nwc_wait_is_capped_by_the_request_budget(self):
        self.fake_sdk()
        waits = []
        real = asyncio.wait_for

        async def recording(what, timeout):
            waits.append(timeout)
            return await real(what, timeout)

        async def answer(_nwc):
            return "ok"

        with mock.patch.object(nwc.asyncio, "wait_for", side_effect=recording):
            self.assertEqual(nwc._run("nostr+walletconnect://x", answer), "ok")
            with http.request_budget(5):
                self.assertEqual(nwc._run("nostr+walletconnect://x", answer), "ok")
        self.assertEqual(waits, [nwc.TIMEOUT_S, 5])

    def test_a_hub_that_does_not_answer_inside_the_budget_is_unavailable(self):
        self.fake_sdk()

        async def hang(_nwc):
            await asyncio.sleep(30)

        started = timezone.now()
        with http.request_budget(0.2), self.assertLogs("apps.rails.gateways.lightning.nwc", "WARNING"):
            with self.assertRaises(http.Unavailable) as cm:
                nwc._run("nostr+walletconnect://x", hang)
        self.assertEqual(cm.exception.kind, "nwc")
        self.assertLess((timezone.now() - started).total_seconds(), 5)


class NetworkGuardTests(TestCase):
    @override_settings(RAILS_HTTP_ENABLED=False)
    def test_nwc_refuses_under_the_test_runner(self):
        async def never(_nwc):
            raise AssertionError("must not connect")

        with mock.patch("nostr_sdk.NostrWalletConnectUri.parse", side_effect=AssertionError("opened")) as parse:
            with self.assertRaises(RuntimeError):
                nwc._run("nostr+walletconnect://x", never)
        parse.assert_not_called()
