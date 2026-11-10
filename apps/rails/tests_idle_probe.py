"""A gateway rail with nothing to poll is still asked whether it answers (`idle_probe`).

On 2026-11-10 Alby Hub came back from an update locked; lightning:4 was marked failing, its last
quote left the 24-hour poll window before the hub was unlocked, and the rail stayed "failing" with
nothing left to ask, so the health check mailed every ten minutes about a working node."""
import datetime
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.utils import timezone

from apps.quotes.models import Quote
from apps.rails import health, http
from apps.rails.gateways import btcpay, lightning
from apps.rails.gateways.lightning import nwc
from apps.rails.models import RailAccount, WatcherHealth
from apps.rails.tests_btcpay import Base as BTCPayBase
from apps.rails.tests_lightning import Base as LightningBase
from apps.vault.vault import vault


def failing(name):
    return WatcherHealth.objects.filter(name=name, failing_since__isnull=False).exists()


def aged(quote):
    """Closed two days ago: outside the poll window, so the poller no longer reads it."""
    two_days = timezone.now() - datetime.timedelta(days=2)
    Quote.objects.filter(pk=quote.pk).update(expires_at=two_days, closed_at=two_days,
                                             read_at=two_days + datetime.timedelta(days=1))
    return quote


class LightningIdleProbeTests(LightningBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.name = f"lightning:{self.acct.pk}"
        self.infos = 0

        async def get_info():
            self.infos += 1
            return SimpleNamespace(methods=["make_invoice", "lookup_invoice", "get_info"])

        self.fake.get_info = get_info

    def test_a_failing_rail_with_nothing_to_poll_is_cleared_once_the_node_answers(self):
        aged(self.quote(state="expired"))
        health.failed(self.name, "nwc")
        self.assertEqual(lightning.lightning_poll(), 0)  # no quote was looked at
        self.assertFalse(failing(self.name))
        self.assertEqual(self.infos, 1)
        self.assertEqual(self.fake.looked, [])  # asked with get_info, not by reading an old invoice

    def test_a_rail_that_stops_answering_with_no_quotes_is_marked_failing(self):
        with mock.patch.object(nwc, "_run", side_effect=http.Unavailable("lightning", "nwc")):
            lightning.lightning_poll()
        h = WatcherHealth.objects.get(name=self.name)
        self.assertEqual((h.last_error_kind, h.consecutive_failures), ("nwc", 1))
        self.assertIsNotNone(h.failing_since)

    def test_an_idle_rail_is_asked_at_most_every_probe_seconds(self):
        lightning.lightning_poll()
        lightning.lightning_poll()
        self.assertEqual(self.infos, 1)
        cache.clear()  # the window has passed
        lightning.lightning_poll()
        self.assertEqual(self.infos, 2)

    def test_a_rail_polled_in_this_pass_is_not_probed_as_well(self):
        self.quote()  # open: the poller reads it
        self.assertEqual(lightning.lightning_poll(), 1)
        self.assertEqual(self.infos, 0)
        self.assertEqual(len(self.fake.looked), 1)

    def test_a_poll_counts_as_asking_for_probe_seconds(self):
        q = self.quote(state="expired")  # inside the 24-hour window: polled
        self.assertEqual(lightning.lightning_poll(), 1)
        aged(q)  # its quote has now left the poll window
        lightning.lightning_poll()
        self.assertEqual(self.infos, 0)  # asked by the poll a moment ago
        cache.clear()
        lightning.lightning_poll()
        self.assertEqual(self.infos, 1)

    def test_nothing_is_asked_while_sealed(self):
        health.failed(self.name, "nwc")
        vault.seal()
        self.assertEqual(lightning.lightning_poll(), 0)
        self.assertEqual(self.infos, 0)
        self.assertTrue(failing(self.name))

    def test_an_inactive_rail_without_unreleased_quotes_is_not_asked(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(active=False)
        lightning.lightning_poll()
        self.assertEqual(self.infos, 0)
        self.assertFalse(WatcherHealth.objects.exists())

    def test_an_unexpected_error_is_recorded_and_does_not_stop_the_pass(self):
        with mock.patch.object(nwc, "_run", side_effect=RuntimeError("boom")), \
                self.assertLogs("apps.rails.gateways.idle_probe", "ERROR"):
            lightning.lightning_poll()
        self.assertEqual(WatcherHealth.objects.get(name=self.name).last_error_kind, "error")


class LightningWithoutGetInfoTests(LightningBase):
    """LND, Core Lightning and LNbits have no info call here: the probe reads the rail's newest
    invoice, the one call its credential is known to allow."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.name = f"lightning:{self.acct.pk}"
        RailAccount.objects.filter(pk=self.acct.pk).update(
            config={"backend": "lnd", "url": "https://lnd.example"}, credentials={"macaroon": "ab" * 8})

    def test_reads_the_newest_invoice(self):
        q = aged(self.quote(state="expired"))
        health.failed(self.name, "unreachable")
        with mock.patch("apps.rails.gateways.lightning.lnd.LndBackend.invoice_state",
                        return_value=("expired", 0)) as state:
            lightning.lightning_poll()
        state.assert_called_once_with(q.external_id)
        self.assertFalse(failing(self.name))

    def test_with_no_invoice_to_read_nothing_is_recorded(self):
        with mock.patch("apps.rails.gateways.lightning.lnd.LndBackend.invoice_state") as state:
            lightning.lightning_poll()
        state.assert_not_called()
        self.assertFalse(WatcherHealth.objects.exists())


class BTCPayIdleProbeTests(BTCPayBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.name = f"btcpay:{self.acct.pk}"
        self.patch_http()

    def test_a_failing_store_with_nothing_to_poll_is_cleared_once_btcpay_answers(self):
        aged(self.quote(state="expired"))
        health.failed(self.name, "unreachable")
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertFalse(failing(self.name))
        self.assertEqual([c[0] for c in self.calls], ["GET"])  # one read-only call

    def test_a_store_that_stops_answering_is_marked_failing(self):
        aged(self.quote(state="expired"))
        self.status = http.Unavailable("btcpay", "unreachable")
        btcpay.btcpay_poll()
        self.assertEqual(WatcherHealth.objects.get(name=self.name).last_error_kind, "unreachable")

    def test_an_answer_does_not_clear_a_speed_policy_that_makes_no_quote(self):
        aged(self.quote(state="expired"))
        RailAccount.objects.filter(pk=self.acct.pk).update(
            config={**self.acct.config, "speed_policy": "HighSpeed"})
        btcpay.btcpay_poll()
        self.assertEqual(WatcherHealth.objects.get(name=self.name).last_error_kind, btcpay.SPEED_POLICY_KIND)

    def test_a_store_with_no_invoice_to_read_is_not_asked(self):
        btcpay.btcpay_poll()
        self.assertEqual(self.calls, [])
        self.assertFalse(WatcherHealth.objects.exists())

