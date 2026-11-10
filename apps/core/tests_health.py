import datetime
import io
from unittest import mock

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from apps.core import worker
from apps.core.models import WorkerHeartbeat
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from apps.rails import health as watcher_health, http
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault


def failed_delivery():
    org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
    endpoint = WebhookEndpoint.objects.create(org=org, url="https://hook.example/x", secret="s", events=["*"])
    event = WebhookEvent.objects.create(type="invoice.sent", org=org)
    return WebhookDelivery.objects.create(event=event, endpoint=endpoint, state="failed")


def failing_for(name, seconds):
    """Watcher `name` failing, its episode started `seconds` ago."""
    from apps.rails.models import WatcherHealth

    watcher_health.failed(name, "timeout")
    WatcherHealth.objects.filter(name=name).update(failing_since=timezone.now() - datetime.timedelta(seconds=seconds))


def beat(seconds_ago=0):
    WorkerHeartbeat.objects.update_or_create(
        pk=1, defaults={"beat_at": timezone.now() - datetime.timedelta(seconds=seconds_ago), "jobs": {}})


class HeartbeatTests(TestCase):
    def setUp(self):
        worker.reset()
        self.addCleanup(worker.reset)

    def test_heartbeat_written_each_loop(self):
        import signal
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        worker.register("tick", 10, lambda: None)
        seen = []

        def sleep(_s):
            row = WorkerHeartbeat.objects.get(pk=1)
            seen.append((row.beat_at, dict(row.jobs)))
            if len(seen) == 2:
                worker.request_stop()

        worker.serve(clock=lambda: 0, sleep=sleep)
        self.assertEqual(WorkerHeartbeat.objects.count(), 1)
        self.assertEqual(len(seen), 2)
        self.assertIn("tick", seen[0][1])
        self.assertGreaterEqual(seen[1][0], seen[0][0])

    def test_long_job_does_not_look_like_a_stale_worker(self):
        from apps.core import health

        clock = {"now": timezone.now()}
        ages = []

        def long_job():
            ages.append(health.snapshot()["worker_age_s"])  # read while the job runs
            clock["now"] += datetime.timedelta(seconds=400)

        worker.register("long", 10, long_job)
        worker.register("after", 10, lambda: ages.append(health.snapshot()["worker_age_s"]))
        with mock.patch("django.utils.timezone.now", side_effect=lambda: clock["now"]):
            worker.run_due(0)
        self.assertEqual(ages[0], 0)  # beaten just before the job started
        self.assertEqual(ages[1], 0)  # and again before the next job

    def test_a_failing_heartbeat_is_swallowed(self):
        with mock.patch.object(WorkerHeartbeat.objects, "update_or_create", side_effect=OperationalError("x")):
            with self.assertLogs("apps.core.worker", "ERROR") as logs:
                worker.beat()
        self.assertIn("OperationalError", logs.output[0])


class HealthzTests(UnsealedVaultMixin, TestCase):
    def get(self):
        return self.client.get("/healthz")

    def test_ok_when_everything_is_quiet(self):
        beat(5)
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(r.json()["db"], "ok")

    def test_healthz_reports_degraded_when_worker_stale(self):
        beat(301)
        r = self.get()
        self.assertEqual((r.status_code, r.json()["status"]), (200, "degraded"))
        self.assertGreaterEqual(r.json()["worker_age_s"], 301)
        WorkerHeartbeat.objects.all().delete()
        j = self.get().json()
        self.assertEqual((j["status"], j["worker_age_s"]), ("degraded", None))

    def test_degraded_when_sealed_or_a_count_is_above_zero(self):
        beat()
        OutboxMessage.objects.create(state="failed")
        self.assertEqual(self.get().json()["outbox_failed"], 1)
        self.assertEqual(self.get().json()["status"], "degraded")
        OutboxMessage.objects.all().delete()
        delivery = failed_delivery()
        j = self.get().json()
        self.assertEqual((j["status"], j["webhooks_failed"]), ("degraded", 1))
        delivery.delete()
        failing_for("chain:secret-name", 601)
        j = self.get().json()
        self.assertEqual((j["status"], j["failing_watchers"]), ("degraded", 1))
        watcher_health.ok("chain:secret-name")
        self.assertEqual(self.get().json()["status"], "ok")
        vault.seal()
        r = self.get()
        self.assertEqual((r.status_code, r.json()["status"], r.json()["vault"]), (200, "degraded", "sealed"))

    def test_a_watcher_counts_once_it_has_failed_as_long_as_the_alert_waits(self):
        # One failed scan is a blip the next pass usually clears; the alert waits ten minutes, so
        # does the health check (else it pages for what the alert deliberately stays quiet about).
        from apps.rails.jobs import WATCHER_ALERT_AFTER_S

        beat()
        watcher_health.failed("scan:evm:arbitrum:1", "timeout")
        j = self.get().json()
        self.assertEqual((j["status"], j["failing_watchers"]), ("ok", 0))
        failing_for("scan:evm:arbitrum:1", WATCHER_ALERT_AFTER_S - 30)
        self.assertEqual(self.get().json()["failing_watchers"], 0)
        failing_for("scan:evm:arbitrum:1", WATCHER_ALERT_AFTER_S + 1)
        j = self.get().json()
        self.assertEqual((j["status"], j["failing_watchers"]), ("degraded", 1))

    def test_healthz_error_503_when_db_unreadable(self):
        with mock.patch("apps.core.health.WorkerHeartbeat") as model:
            model.objects.filter.side_effect = OperationalError("boom")
            with self.assertLogs("apps.core.views", "ERROR"):
                r = self.get()
        self.assertEqual(r.status_code, 503)
        body = r.json()
        self.assertEqual((body["status"], body["db"]), ("error", "error"))
        self.assertEqual([body[k] for k in ("worker_age_s", "failing_watchers", "outbox_failed", "webhooks_failed")],
                         [None] * 4)

    def test_healthz_has_no_client_data(self):
        beat()
        watcher_health.failed("chain:secret-name", "http")
        OutboxMessage.objects.create(state="failed")
        body = self.get().content.decode()
        self.assertNotIn("secret-name", body)
        self.assertEqual(sorted(self.get().json()), sorted(
            ["status", "vault", "db", "worker_age_s", "failing_watchers", "outbox_failed", "webhooks_failed"]))


class HealthcheckCommandTests(UnsealedVaultMixin, TestCase):
    def run_cmd(self, *args):
        out = io.StringIO()
        try:
            call_command("healthcheck", *args, stdout=out)
        except SystemExit as exit_:
            return exit_.code, out.getvalue()
        return 0, out.getvalue()

    def test_healthcheck_exit_codes(self):
        beat(5)
        self.assertEqual(self.run_cmd("--no-network"), (0, ""))
        beat(400)
        code, text = self.run_cmd("--no-network")
        self.assertEqual(code, 1)
        self.assertIn("worker_stale 400", text)
        self.assertEqual(self.run_cmd("--no-network", "--max-worker-age", "600"), (0, ""))
        WorkerHeartbeat.objects.all().delete()
        vault.seal()
        OutboxMessage.objects.create(state="failed")
        code, text = self.run_cmd("--no-network")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(text.split("\n")[:-1]), ["outbox_failed 1", "vault_sealed 1", "worker_missing 1"])
        OutboxMessage.objects.all().delete()
        vault.unseal(TEST_VAULT_PASSWORD)
        failed_delivery()
        beat()
        self.assertEqual(self.run_cmd("--no-network"), (1, "webhooks_failed 1\n"))

    def test_healthcheck_fx_probe_patched(self):
        beat()
        with mock.patch("apps.rails.fx.price_in") as price:
            self.assertEqual(self.run_cmd(), (0, ""))
        price.assert_called_once_with("BTC", "EUR", use_cache=False)
        with mock.patch("apps.rails.fx.price_in", side_effect=http.Unavailable("fx", "kraken")), \
                self.assertLogs("apps.core.management.commands.healthcheck", "WARNING"):
            self.assertEqual(self.run_cmd(), (1, "fx_unavailable 1\n"))
        with mock.patch("apps.rails.fx.price_in") as price:
            self.run_cmd("--no-network")
        price.assert_not_called()

    def test_a_cached_good_rate_does_not_hide_failing_sources(self):
        from decimal import Decimal

        from django.core.cache import cache

        from apps.rails import fx

        beat()
        cache.set("fx:BTC:EUR", fx.RatePair(Decimal(50000), Decimal(50000), "cached"), timeout=600)
        with mock.patch.object(fx, "_compute", side_effect=http.Unavailable("fx", "kraken")), \
                self.assertLogs("apps.core.management.commands.healthcheck", "WARNING"):
            self.assertEqual(self.run_cmd(), (1, "fx_unavailable 1\n"))
        self.assertEqual(fx.price_in("BTC", "EUR").second, Decimal(50000))  # ordinary callers still use the cache
