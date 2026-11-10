"""`money_may_be_coming` on a chain quote (audit LS-2, invoicing half): true whenever the watcher could not
look for the money, with the criteria of the link-order void (`links.services._could_not_look`): the quote
still holds its amount (not released), a `scan:`/`ingest:` watcher of its account and network is failing, the
scan has not read past the quote's expiry, or blocks of that address and network were skipped and nobody has
acknowledged them. liberation.services holds its 120-minute void while any quote says so."""
import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.api.tests_crypto import ADDR, CryptoApiBase
from apps.quotes.models import Quote
from apps.rails import health
from apps.rails.models import RailAccount, ScanGap, WatcherHealth


class ChainQuoteBase(CryptoApiBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.scan = f"scan:evm:arbitrum:{self.rail.pk}"
        self.ingest = f"ingest:evm:arbitrum:{self.rail.pk}"

    def quote(self, state="released", expired_minutes_ago=60 * 25, **kw):
        expires = self.now - datetime.timedelta(minutes=expired_minutes_ago)
        amount = str(1000 + Quote.objects.count())  # an unreleased amount is unique per address
        fields = dict(invoice=self.inv, rail_account=self.rail, network="arbitrum", asset="USDC", address=ADDR,
                      amount_base_units=amount, min_accept_base_units=amount, ceiling_base_units=amount,
                      invoice_amount_minor=10000, issued_at=expires - datetime.timedelta(minutes=30),
                      expires_at=expires, state=state,
                      closed_at=None if state == "open" else expires)
        fields.update(kw)
        return Quote.objects.create(**fields)

    def healthy(self):
        health.ok(self.scan)  # read just now, so past every expiry here

    def coming(self, quote):
        r = self.client_.get(f"/api/v1/quotes/{quote.pk}")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["money_may_be_coming"]


class ChainMoneyMayBeComingTests(ChainQuoteBase):

    def test_a_released_quote_with_a_healthy_watcher_is_false(self):
        self.healthy()
        self.assertIs(self.coming(self.quote()), False)

    def test_a_quote_not_yet_released_is_true(self):
        self.healthy()
        for state, minutes in (("open", -10), ("expired", 30), ("cancelled", 60 * 23), ("expired", 60 * 30)):
            with self.subTest(state=state, minutes=minutes):
                self.assertIs(self.coming(self.quote(state=state, expired_minutes_ago=minutes)), True)

    def test_a_failing_scan_or_ingest_watcher_is_true_until_it_recovers(self):
        q = self.quote()
        for name in (self.scan, self.ingest):
            for kind in ("timeout", "backlog"):
                with self.subTest(name=name, kind=kind):
                    self.healthy()
                    health.failed(name, kind)
                    self.assertIs(self.coming(q), True)
                    health.ok(name)
                    self.assertIs(self.coming(q), False)

    def test_a_scan_that_has_not_read_past_the_expiry_is_true(self):
        q = self.quote(expired_minutes_ago=90)
        self.healthy()
        WatcherHealth.objects.filter(name=self.scan).update(last_ok_at=q.expires_at - datetime.timedelta(seconds=1))
        Quote.objects.filter(pk=q.pk).update(state="released")
        self.assertIs(self.coming(q), True)
        WatcherHealth.objects.filter(name=self.scan).update(last_ok_at=q.expires_at + datetime.timedelta(seconds=1))
        self.assertIs(self.coming(q), False)

    def test_blocks_not_scanned_are_true_until_a_person_acknowledges_them(self):
        self.healthy()
        q = self.quote()
        elsewhere = ScanGap.objects.create(network="ethereum", address=ADDR, from_block=1, to_block=9)
        self.assertIs(self.coming(q), False)
        gap = ScanGap.objects.create(network="arbitrum", address=ADDR.upper().replace("0X", "0x"), from_block=11,
                                     to_block=8000)
        self.assertIs(self.coming(q), True)
        health.acknowledge_gap(gap, get_user_model().objects.create_user("clerk", is_staff=True), "checked")
        self.assertIs(self.coming(q), False)
        self.assertIsNone(ScanGap.objects.get(pk=elsewhere.pk).acknowledged_at)

    def test_another_network_or_account_has_no_bearing(self):
        self.healthy()
        q = self.quote()
        other = RailAccount.objects.create(org=self.org, kind="evm", label="Other", address="0x" + "cd" * 20,
                                           networks=["arbitrum"], assets=["USDC"])
        health.failed(f"scan:evm:ethereum:{self.rail.pk}", "timeout")
        health.failed(f"scan:evm:arbitrum:{other.pk}", "timeout")
        self.assertIs(self.coming(q), False)

    def test_a_retired_accounts_stale_failing_row_does_not_count(self):
        """As for the link-order void: an account nothing scans any more leaves a row nothing will clear."""
        q = self.quote()
        health.failed(self.scan, "timeout")
        RailAccount.objects.filter(pk=self.rail.pk).update(active=False)
        self.assertIs(self.coming(q), False)

    def test_the_invoice_listing_says_the_same(self):
        self.healthy()
        held = self.quote(state="expired", expired_minutes_ago=30)
        done = self.quote()
        listed = {x["id"]: x["money_may_be_coming"]
                  for x in self.client_.get(f"/api/v1/invoices/{self.inv.pk}/quotes").json()["results"]}
        self.assertEqual(listed, {held.pk: True, done.pk: False})

    def test_the_link_order_void_reads_the_same_criteria(self):
        """One rule for both: the API's chain answer and the 7-day void share `quotes.services.could_not_look`."""
        from apps.links.services import _could_not_look

        self.healthy()
        q = self.quote()
        self.assertIsNone(_could_not_look(self.inv.pk))
        health.failed(self.scan, "timeout")
        self.assertEqual(_could_not_look(self.inv.pk), "watcher_failing")
        self.assertIs(self.coming(q), True)


class MoneyMayBeComingReasonTests(ChainQuoteBase):
    """`money_may_be_coming_reason` beside the flag (review S5, M-5): which "could not look" holds the void,
    so liberation.services can treat a quote that only still holds its amount apart from a watcher outage."""

    def read(self, quote):
        body = self.client_.get(f"/api/v1/quotes/{quote.pk}").json()
        self.assertIs(body["money_may_be_coming"], body["money_may_be_coming_reason"] != "")
        return body["money_may_be_coming_reason"]

    def test_each_reason(self):
        self.healthy()
        self.assertEqual(self.read(self.quote(state="expired", expired_minutes_ago=30)), "quote_held")
        released = self.quote()
        self.assertEqual(self.read(released), "")
        health.failed(self.scan, "timeout")
        self.assertEqual(self.read(released), "watcher_failing")
        health.ok(self.scan)
        WatcherHealth.objects.filter(name=self.scan).update(
            last_ok_at=released.expires_at - datetime.timedelta(seconds=1))
        self.assertEqual(self.read(released), "watcher_behind")
        self.healthy()
        ScanGap.objects.create(network="arbitrum", address=ADDR, from_block=1, to_block=9)
        self.assertEqual(self.read(released), "blocks_not_scanned")

    def test_a_gateway_quote_says_gateway(self):
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["XMR"],
                                            networks=["monero"])
        gate = Quote.objects.create(
            invoice=self.inv, rail_account=btcpay, network="monero", asset="XMR", address="",
            amount_base_units="1", min_accept_base_units="1", ceiling_base_units="1", invoice_amount_minor=10000,
            issued_at=self.now - datetime.timedelta(minutes=31), expires_at=self.now - datetime.timedelta(minutes=1),
            state="expired", read_at=None)
        self.assertEqual(self.read(gate), "gateway")
        Quote.objects.filter(pk=gate.pk).update(read_at=self.now)
        self.assertEqual(self.read(gate), "")

    def test_the_listing_carries_it_too(self):
        self.healthy()
        held = self.quote(state="expired", expired_minutes_ago=30)
        listed = self.client_.get(f"/api/v1/invoices/{self.inv.pk}/quotes").json()["results"]
        self.assertEqual({x["id"]: x["money_may_be_coming_reason"] for x in listed}, {held.pk: "quote_held"})
