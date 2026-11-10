"""The worker's crypto jobs: what is scheduled, what runs sealed, and the watcher alert."""
import datetime
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core import jobs as core_jobs, worker
from apps.invoices.models import Invoice
from apps.payments import matching
from apps.payments.models import Payment
from apps.payments.tests_matching import EVM, K0, PAYER, TX, TX2, MatchingBase, transfer
from apps.rails import health, http, jobs
from apps.rails.chains import base as chains
from apps.rails.models import PendingTransfer, RailAccount, ScanGap, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault


class RegisterTests(TestCase):
    def setUp(self):
        worker.reset()
        self.addCleanup(worker.reset)

    def test_register_all_names_and_intervals(self):
        core_jobs.register_all()
        every = {name: job["every_s"] for name, job in worker._jobs.items()}
        for name, seconds in {
            "quotes_expire": 60, "quotes_release": 3600, "chain_scan": 60, "finality_refresh": 60,
            "declarations_verify": 300, "btcpay_poll": 60, "lightning_poll": 30, "watcher_alert": 60,
            "outbox": 15, "sealed_alert": 60, "quote_capacity": 300,
        }.items():
            self.assertEqual(every.get(name), seconds, name)


class ScriptedAdapter:
    """scan() does what the test scripted for (account pk, network)."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def scan(self, account, network):
        self.calls.append((account.pk, network))
        what = self.script[(account.pk, network)]
        if isinstance(what, Exception):
            raise what
        health.ok(f"scan:evm:{network}:{account.pk}")  # the real adapters record their own health
        return what

    def transfers_in_tx(self, network, tx_hash):
        return []

    def status(self, payment):
        return "pending"


class ChainScanTests(MatchingBase):
    def test_chain_scan_ingests_and_records_health_ok(self):
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(K0)]})
        with chains.override_adapter("evm", adapter):
            jobs.chain_scan()
        p = Payment.objects.get()
        self.assertEqual((p.state, p.invoice_id), ("confirmed", self.invoice.pk))
        self.assertEqual(adapter.calls, [(self.acct.pk, "arbitrum")])
        h = WatcherHealth.objects.get(name=f"scan:evm:arbitrum:{self.acct.pk}")
        self.assertIsNone(h.failing_since)
        self.assertIsNotNone(h.last_ok_at)
        self.assertEqual(WatcherHealth.objects.count(), 1)  # the job writes no second row

    def test_chain_scan_failure_records_health_and_keeps_going(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(networks=["arbitrum", "ethereum"])
        good = RailAccount.objects.create(org=self.org, kind="evm", label="Two", address="0x" + "ee" * 20,
                                          networks=["arbitrum"], assets=["USDC"])
        RailAccount.objects.create(org=self.org, kind="evm", label="Off", address="0x" + "dd" * 20,
                                   networks=["arbitrum"], assets=["USDC"], active=False)
        adapter = ScriptedAdapter({
            (self.acct.pk, "arbitrum"): http.Unavailable("rpc", "timeout"),
            (self.acct.pk, "ethereum"): RuntimeError("secret detail 0xabc"),
            (good.pk, "arbitrum"): [transfer(1, to=good.address)],
        })
        with chains.override_adapter("evm", adapter), \
                mock.patch("apps.payments.matching.ingest") as ingest, \
                self.assertLogs("apps.rails.jobs", "ERROR") as logs:
            jobs.chain_scan()
        self.assertEqual(adapter.calls, [(self.acct.pk, "arbitrum"), (self.acct.pk, "ethereum"), (good.pk, "arbitrum")])
        ingest.assert_called_once()
        self.assertEqual(ingest.call_args.kwargs["account"], good)
        bad = WatcherHealth.objects.get(name=f"scan:evm:ethereum:{self.acct.pk}")
        self.assertEqual((bad.last_error_kind, bad.consecutive_failures), ("RuntimeError", 1))
        self.assertIsNotNone(bad.failing_since)
        # An Unavailable was already recorded by the adapter itself; the job adds nothing.
        self.assertFalse(WatcherHealth.objects.filter(name=f"scan:evm:arbitrum:{self.acct.pk}").exists())
        self.assertNotIn("secret detail", "".join(logs.output))
        self.assertIn("RuntimeError", "".join(logs.output))

    def ingest_failing(self, times):
        """`matching.ingest`, raising "database is locked" on its first `times` calls."""
        real, calls = matching.ingest, []

        def ingest(t, **kw):
            calls.append(t)
            if len(calls) <= times:
                raise OperationalError("database is locked")
            return real(t, **kw)
        return mock.patch("apps.payments.matching.ingest", side_effect=ingest)

    def ingest_health(self, account=None):
        return WatcherHealth.objects.get(name=f"ingest:evm:arbitrum:{(account or self.acct).pk}")

    def test_pending_retry_failure_does_not_abort_the_scan(self):
        row = PendingTransfer.objects.create(**{**PendingTransfer.fields_of(transfer(1, tx=TX2)),
                                                "account": self.acct, "attempts": 1})
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(K0)]})
        with chains.override_adapter("evm", adapter), \
                mock.patch.object(PendingTransfer, "delete", side_effect=OperationalError("database is locked")), \
                self.assertLogs("apps.rails.jobs", "ERROR") as logs:
            jobs.chain_scan()
        self.assertEqual(adapter.calls, [(self.acct.pk, "arbitrum")])
        self.assertEqual(self.status(), "paid")  # the scan went on and settled the invoice
        out = "".join(logs.output)
        self.assertIn("OperationalError", out)
        self.assertNotIn(TX2, out)
        self.assertTrue(PendingTransfer.objects.filter(pk=row.pk).exists())

    def test_ingest_failure_queues_the_transfer_and_keeps_the_rest(self):
        good = RailAccount.objects.create(org=self.org, kind="evm", label="Two", address="0x" + "ee" * 20,
                                          networks=["arbitrum"], assets=["USDC"])
        stray = transfer(1, tx=TX2, log_index=3, final=False, block=999)
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [stray, transfer(K0)], (good.pk, "arbitrum"): []})
        with chains.override_adapter("evm", adapter), self.ingest_failing(1), \
                self.assertLogs("apps.rails.jobs", "ERROR") as logs:
            jobs.chain_scan()
        self.assertEqual(len(adapter.calls), 2)  # the other account was still scanned
        # The transfer after the failed one was recorded, and settled the invoice.
        p = Payment.objects.get()
        self.assertEqual((p.tx_hash, p.state, p.invoice_id), (TX, "confirmed", self.invoice.pk))
        self.assertEqual(self.status(), "paid")
        # The failed one is remembered exactly, in plaintext, for the next pass.
        q = PendingTransfer.objects.get()
        self.assertEqual(
            (q.account_id, q.network, q.contract, q.to_address, q.from_address, q.amount_base_units, q.tx_hash,
             q.log_index, q.block_number, q.block_time, q.final, q.comment, q.attempts, q.last_error_kind),
            (self.acct.pk, "arbitrum", stray.contract, EVM, PAYER, "1", TX2, 3, 999, stray.block_time, False, "",
             1, "OperationalError"))
        self.assertEqual(q.transfer(), stray)
        h = self.ingest_health()
        self.assertEqual((h.last_error_kind, h.consecutive_failures), ("OperationalError", 1))
        self.assertIsNotNone(h.failing_since)
        self.assertIsNone(WatcherHealth.objects.get(name=f"scan:evm:arbitrum:{self.acct.pk}").failing_since)
        self.assertFalse(WatcherHealth.objects.filter(name=f"ingest:evm:arbitrum:{good.pk}").exists())
        out = "".join(logs.output)
        self.assertIn("OperationalError", out)
        for secret in ("0x", "database is locked", str(K0)):
            self.assertNotIn(secret, out)

    def test_pending_transfer_retried_until_ingested(self):
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(K0)]})
        with chains.override_adapter("evm", adapter):
            with self.ingest_failing(2), self.assertLogs("apps.rails.jobs", "ERROR"):
                jobs.chain_scan()  # seen, not recorded
                adapter.script[(self.acct.pk, "arbitrum")] = []  # the cursor has moved past it
                jobs.chain_scan()  # retried, still failing
            q = PendingTransfer.objects.get()
            self.assertEqual((q.attempts, q.last_error_kind), (2, "OperationalError"))
            self.assertFalse(Payment.objects.exists())
            self.assertEqual(self.ingest_health().consecutive_failures, 2)
            vault.seal()  # the retry runs sealed, like every chain path
            jobs.chain_scan()
        self.assertFalse(PendingTransfer.objects.exists())
        p = Payment.objects.only(*matching.PAYMENT_PLAIN_FIELDS).get()
        self.assertEqual((p.tx_hash, p.state, p.invoice_id), (TX, "confirmed", self.invoice.pk))
        self.assertEqual(Invoice.objects.filter(pk=self.invoice.pk).values_list("status", flat=True).get(), "paid")
        h = self.ingest_health()
        self.assertIsNone(h.failing_since)
        self.assertIsNotNone(h.last_ok_at)
        jobs.chain_scan()  # drained: nothing more is written
        self.assertEqual(WatcherHealth.objects.get(pk=h.pk).last_ok_at, h.last_ok_at)

    def test_a_queued_transfer_read_again_and_recorded_leaves_the_queue(self):
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(K0)]})  # an overlap reads it twice
        with chains.override_adapter("evm", adapter), self.ingest_failing(2), \
                self.assertLogs("apps.rails.jobs", "ERROR"):
            jobs.chain_scan()  # seen, not recorded
            jobs.chain_scan()  # the retry fails again; the scan reads it again and records it
        self.assertEqual(Payment.objects.get().tx_hash, TX)
        self.assertFalse(PendingTransfer.objects.exists())
        self.assertIsNone(self.ingest_health().failing_since)  # within the same pass

    def test_overlap_delete_failure_keeps_the_rest_of_the_batch(self):
        from django.db.models.query import QuerySet
        real_delete = QuerySet.delete

        def delete(qs):
            if qs.model is PendingTransfer:
                raise OperationalError("database is locked")
            return real_delete(qs)

        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(1, tx=TX2, log_index=3)]})
        with chains.override_adapter("evm", adapter), self.ingest_failing(2), \
                self.assertLogs("apps.rails.jobs", "ERROR"):
            jobs.chain_scan()  # TX2 seen, not recorded: queued
            # an overlap reads TX2 again, then a new transfer; the retry fails, the scan records TX2
            adapter.script[(self.acct.pk, "arbitrum")] = [transfer(1, tx=TX2, log_index=3), transfer(K0)]
            with mock.patch.object(QuerySet, "delete", delete):
                jobs.chain_scan()
        self.assertEqual(sorted(Payment.objects.values_list("tx_hash", flat=True)), sorted([TX, TX2]))
        self.assertEqual(self.status(), "paid")  # the transfer after the failed cleanup was recorded
        self.assertTrue(PendingTransfer.objects.exists())  # left for the next pass, which is idempotent

    def test_chain_scan_runs_while_sealed(self):
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): []})
        vault.seal()
        with chains.override_adapter("evm", adapter):
            jobs.chain_scan()
        self.assertEqual(len(adapter.calls), 1)


class GatedJobTests(UnsealedVaultMixin, TestCase):
    TARGETS = {
        "quotes_expire": "apps.quotes.services.expire_quotes",
        "quotes_release": "apps.quotes.services.release_quotes",
        "quote_capacity": "apps.quotes.services.check_capacity",
        "finality_refresh": "apps.payments.matching.refresh_finality",
        "declarations_verify": "apps.payments.matching.verify_open_declarations",
        "btcpay_poll": "apps.rails.gateways.btcpay.btcpay_poll",
        "lightning_poll": "apps.rails.gateways.lightning.lightning_poll",
    }

    def run_all(self):
        mocks = {}
        for name, target in self.TARGETS.items():
            patcher = mock.patch(target)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        for name in self.TARGETS:
            getattr(jobs, name)()
        return mocks

    def test_btcpay_and_lightning_jobs_skip_sealed_others_run(self):
        vault.seal()
        m = self.run_all()
        for name in ("quotes_expire", "quotes_release", "quote_capacity", "finality_refresh", "declarations_verify"):
            m[name].assert_called_once()
        m["btcpay_poll"].assert_not_called()
        m["lightning_poll"].assert_not_called()

    def test_unsealed_everything_runs(self):
        m = self.run_all()
        for name in self.TARGETS:
            m[name].assert_called_once()


@override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
class WatcherAlertTests(UnsealedVaultMixin, TestCase):
    def fail(self, name, kind, minutes):
        health.failed(name, kind)
        WatcherHealth.objects.filter(name=name).update(
            failing_since=timezone.now() - datetime.timedelta(minutes=minutes))

    def test_watcher_alert_once_per_episode(self):
        self.fail("scan:evm:arbitrum:1", "timeout", 11)
        self.fail("btcpay:2", "refused", 3)  # not long enough yet
        vault.seal()  # runs sealed
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com"])
        self.assertIn("scan:evm:arbitrum:1", m.body)
        self.assertIn("timeout", m.body)
        self.assertNotIn("btcpay:2", m.body)
        self.assertIsNotNone(WatcherHealth.objects.get(name="scan:evm:arbitrum:1").alerted_at)
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 1)  # same episode: silent
        # The second watcher crosses the line: it is told, the first is not repeated.
        WatcherHealth.objects.filter(name="btcpay:2").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=12))
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("btcpay:2", mail.outbox[1].body)
        self.assertNotIn("scan:evm", mail.outbox[1].body)
        # Recovery ends the episode; the next failure is a new one.
        health.ok("scan:evm:arbitrum:1")
        self.fail("scan:evm:arbitrum:1", "timeout", 11)
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 3)

    def test_a_send_that_fails_is_tried_again(self):
        self.fail("lightning:3", "refused", 20)
        with mock.patch("apps.rails.jobs.EmailMultiAlternatives.send", side_effect=OSError), \
                self.assertLogs("apps.rails.jobs", "ERROR"):
            jobs.watcher_alert()
        self.assertIsNone(WatcherHealth.objects.get(name="lightning:3").alerted_at)
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 1)

    def gap(self, network="arbitrum", lo=11, hi=8000, **kw):
        return ScanGap.objects.create(network=network, address=EVM, from_block=lo, to_block=hi, **kw)

    def test_watcher_alert_mails_a_gap_once(self):
        first = self.gap()
        self.gap(network="base", lo=5, hi=6, acknowledged_at=timezone.now())  # already handled by a person
        vault.seal()  # runs sealed
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com"])
        self.assertIn("arbitrum", m.body)
        self.assertIn("11", m.body)
        self.assertIn("8000", m.body)
        self.assertNotIn("base", m.body)
        self.assertNotIn(EVM, m.body)  # names and block numbers; the page shows the rest
        self.assertIn("https://pay.example/", m.body)
        self.assertIsNotNone(ScanGap.objects.get(pk=first.pk).alerted_at)
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 1)  # told once
        second = self.gap(network="optimism", lo=100, hi=200)
        with mock.patch("apps.rails.jobs.EmailMultiAlternatives.send", side_effect=OSError), \
                self.assertLogs("apps.rails.jobs", "ERROR"):
            jobs.watcher_alert()
        self.assertIsNone(ScanGap.objects.get(pk=second.pk).alerted_at)  # not sent: tried again
        jobs.watcher_alert()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("optimism", mail.outbox[1].body)
        self.assertNotIn("arbitrum", mail.outbox[1].body)
        self.assertIsNotNone(ScanGap.objects.get(pk=second.pk).alerted_at)

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_no_recipients_marks_nothing(self):
        cache.clear()
        self.fail("lightning:3", "refused", 20)
        with self.assertLogs("apps.rails.jobs", "WARNING"):
            jobs.watcher_alert()
        self.assertIsNone(WatcherHealth.objects.get(name="lightning:3").alerted_at)
        self.assertEqual(len(mail.outbox), 0)
