import datetime
import os
import signal
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.contacts.services import find_or_create_contact
from apps.core import jobs, worker
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault

LINE = {"name": "Service", "quantity": 1, "unit_price_minor": 133700}


class WorkerRegistryTests(TestCase):
    def setUp(self):
        worker.reset()
        self.addCleanup(worker.reset)

    def test_run_due_respects_intervals(self):
        calls = []
        worker.register("fast", 10, lambda: calls.append("fast"))
        worker.register("slow", 60, lambda: calls.append("slow"))
        self.assertEqual(worker.run_due(100), ["fast", "slow"])
        self.assertEqual(worker.run_due(105), [])
        self.assertEqual(worker.run_due(110), ["fast"])
        self.assertEqual(worker.run_due(160), ["fast", "slow"])
        self.assertEqual(calls, ["fast", "slow", "fast", "fast", "slow"])

    def test_a_failing_job_is_logged_and_does_not_stop_the_others(self):
        calls = []

        def boom():
            raise RuntimeError("secret detail")

        worker.register("boom", 10, boom)
        worker.register("ok", 10, lambda: calls.append("ok"))
        with self.assertLogs("apps.core.worker", "ERROR") as logs:
            self.assertEqual(worker.run_due(0), ["boom", "ok"])
        self.assertIn("boom", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("secret detail", logs.output[0])
        self.assertEqual(calls, ["ok"])
        self.assertEqual(worker.run_due(5), [])  # the failure is rescheduled, not retried at once
        with self.assertLogs("apps.core.worker", "ERROR"):
            self.assertEqual(worker.run_due(10), ["boom", "ok"])

    def test_sigterm_finishes_current_job(self):
        done = []

        def job():
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while the job is running
            done.append("finished")

        worker.register("job", 1, job)
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        sleeps = []
        worker.serve(clock=lambda: 0, sleep=sleeps.append)
        self.assertEqual(done, ["finished"])
        self.assertEqual(sleeps, [])  # no further loop after the job in hand


class JobTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "c@example.com", legal_name="Client", preferred_language="en")

    def sent(self):
        return send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])[0]

    def test_overdue_job_marks_past_due_sent_invoices(self):
        late, fresh = self.sent(), self.sent()
        Invoice.objects.filter(pk=late.pk).update(due_date=datetime.date.today() - datetime.timedelta(days=1))
        Invoice.objects.filter(pk=fresh.pk).update(due_date=datetime.date.today())
        vault.seal()  # touches no encrypted field, so it runs sealed too
        jobs.overdue()
        status = dict(Invoice.objects.values_list("pk", "status"))  # read as a sealed reader must
        self.assertEqual(status[late.pk], "overdue")
        self.assertEqual(status[fresh.pk], "sent")

    def test_overdue_job_uses_the_local_date(self):
        from unittest import mock

        with mock.patch("apps.core.jobs.timezone.localdate", return_value=datetime.date(2031, 5, 6)), \
                mock.patch("apps.core.jobs.mark_overdue") as mark:
            jobs.overdue()
        mark.assert_called_once_with(datetime.date(2031, 5, 6))

    def test_outbox_job_flushes(self):
        self.sent()
        jobs.outbox()
        self.assertEqual(OutboxMessage.objects.get(kind="invoice").state, "sent")

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_sealed_alert_once_per_episode(self):
        self.sent()
        mail.outbox.clear()
        jobs.sealed_alert()
        self.assertEqual(len(mail.outbox), 0)  # unsealed: nothing to say
        vault.seal()
        with self.assertLogs("apps.core.jobs", "WARNING") as logs:
            jobs.sealed_alert()
            jobs.sealed_alert()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com"])
        self.assertIn("https://pay.example/unlock/", m.body)
        self.assertIn("1", m.subject)
        self.assertTrue(m.alternatives)
        vault.unseal(TEST_VAULT_PASSWORD)
        jobs.sealed_alert()  # episode over, flag cleared
        vault.seal()
        with self.assertLogs("apps.core.jobs", "WARNING"):
            jobs.sealed_alert()
        self.assertEqual(len(mail.outbox), 2)

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_sealed_alert_without_recipients_warns_once(self):
        vault.seal()
        with self.assertLogs("apps.core.jobs", "WARNING") as logs:
            jobs.sealed_alert()
            jobs.sealed_alert()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(mail.outbox), 0)


class RunWorkerCommandTests(TestCase):
    def setUp(self):
        worker.reset()
        self.addCleanup(worker.reset)
        # The sealed-episode flag is module state; whether an earlier test left it set decided
        # whether this test printed "nobody was told" to the console. Start every run fresh.
        jobs._end_episode()
        self.addCleanup(jobs._end_episode)

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_once_runs_every_job_and_exits(self):
        out = StringIO()
        vault.seal()
        with self.assertLogs("apps.core.jobs", "WARNING") as logs:
            call_command("run_worker", "--once", stdout=out)
        self.assertTrue(any("vault is sealed" in line for line in logs.output))
        self.assertEqual(set(out.getvalue().split()), {
            "outbox", "overdue", "sealed_alert", "cache_purge", "outbox_failed_alert",
            "quotes_expire", "quotes_release", "chain_scan", "finality_refresh", "declarations_verify",
            "btcpay_poll", "lightning_poll", "watcher_alert", "webhooks_flush", "webhooks_failed_alert",
            "accounting_rates", "link_orders", "quote_capacity", "invoice_reminders",
        })


class SealedAlertCacheRecoveryTests(UnsealedVaultMixin, TestCase):
    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_sealed_alert_once_when_cache_fails_then_recovers(self):
        from unittest import mock

        jobs._sealed_announced_locally = False
        mail.outbox.clear()
        vault.seal()
        real_add = jobs.cache.add
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("no cache table yet")
            return real_add(*a, **kw)

        with mock.patch.object(jobs.cache, "add", side_effect=flaky), self.assertLogs("apps.core.jobs"):
            for _ in range(3):
                jobs.sealed_alert()
        self.assertEqual(len(mail.outbox), 1)
        jobs._end_episode()
