"""Task 6 (API-3, API-8): ported from poc_api_3 and poc_api_8."""
import datetime
import io
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.orgs.models import Organization
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks import delivery, events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint

PUBLIC = [(2, 1, 6, "", ("93.184.215.14", 443))]


class EndpointCapTests(UnsealedVaultMixin, TestCase):
    """API-3, first half (poc_api_3 test 1)."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        _, plain = ApiToken.issue(self.org, "adm", ["admin"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")

    def test_endpoints_per_org_are_capped(self):
        with mock.patch("socket.getaddrinfo", return_value=PUBLIC):
            codes = [self.c.post("/api/v1/webhooks", {"url": f"https://tarpit.example.net/{i}"}, format="json")
                     .status_code for i in range(60)]
        self.assertLess(WebhookEndpoint.objects.filter(org=self.org).count(), 60, "no cap on endpoints per org")
        self.assertEqual(codes.count(201), 10)
        self.assertEqual(set(codes) - {201}, {400})

    def test_the_cap_counts_active_endpoints_and_one_org_does_not_use_up_another(self):
        other = Organization.objects.create(slug="other")
        with mock.patch("socket.getaddrinfo", return_value=PUBLIC):
            made = [delivery.create_endpoint(self.org, f"https://h.example.net/{i}")[0] for i in range(10)]
            with self.assertRaises(delivery.WebhookRefused) as caught:
                delivery.create_endpoint(self.org, "https://h.example.net/x")
            self.assertEqual(caught.exception.code, "endpoint_limit")
            delivery.create_endpoint(other, "https://h.example.net/y")
            WebhookEndpoint.objects.filter(pk=made[0].pk).update(active=False)
            delivery.create_endpoint(self.org, "https://h.example.net/z")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@override_settings(WEBHOOK_HTTP_ENABLED=True)
class FlushBudgetTests(UnsealedVaultMixin, TestCase):
    """API-3, second half (poc_api_3 test 2). The poc measured a real tarpit; here a fake clock
    advances 20 s per attempt, so the budget is asserted without sleeping."""

    N = 8

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        with mock.patch("socket.getaddrinfo", return_value=PUBLIC):
            for i in range(self.N):
                delivery.create_endpoint(self.org, f"https://tarpit.example.net/{i}")
        events.emit("ping", org=self.org)
        self.assertEqual(WebhookDelivery.objects.filter(state="queued").count(), self.N)
        self.clock = FakeClock()

    def _flush(self):
        def slow_post(url, body, headers):
            self.clock.now += 20
            raise delivery.DeliveryDeadline()

        with mock.patch.object(delivery, "_monotonic", self.clock), \
                mock.patch.object(delivery, "post", slow_post), \
                mock.patch("socket.getaddrinfo", return_value=PUBLIC), \
                self.assertLogs("apps.webhooks.delivery", "WARNING"):
            return delivery.flush()

    def test_one_pass_stops_starting_attempts_after_the_budget(self):
        self._flush()
        tried = WebhookDelivery.objects.filter(attempts=1).count()
        self.assertEqual(tried, 2, "attempts started at t=0 and t=20 only; t=40 is past the 30 s budget")
        untouched = WebhookDelivery.objects.filter(attempts=0)
        self.assertEqual(untouched.count(), self.N - 2)
        self.assertEqual(untouched.filter(state="queued", next_attempt_at__isnull=True).count(), self.N - 2,
                         "a delivery not attempted keeps its schedule")

    def test_the_next_run_picks_up_what_was_left(self):
        self._flush()
        WebhookDelivery.objects.filter(attempts=1).update(next_attempt_at=timezone.now() + datetime.timedelta(hours=1))
        self.clock.now += 1000
        self._flush()
        self.assertEqual(WebhookDelivery.objects.filter(attempts=1).count(), 4)

    def test_the_budget_is_thirty_seconds(self):
        self.assertEqual(delivery.FLUSH_BUDGET_S, 30)

    def test_the_destination_lookup_is_inside_the_attempt_deadline(self):
        """A lookup that eats the whole deadline makes the attempt a `deadline` failure, not a post."""
        seen = []

        def slow_check(url):
            self.clock.now += delivery.DELIVERY_DEADLINE_S + 1

        with mock.patch.object(delivery, "_monotonic", self.clock), \
                mock.patch.object(delivery, "check_destination", slow_check), \
                mock.patch.object(delivery, "post", lambda *a: seen.append(1) or 200), \
                mock.patch("socket.getaddrinfo", return_value=PUBLIC), \
                self.assertLogs("apps.webhooks.delivery", "WARNING"):
            delivery.flush(limit=1)
        self.assertEqual(seen, [])
        self.assertEqual(WebhookDelivery.objects.filter(last_error_kind="deadline").count(), 1)


class DestinationTests(SimpleTestCase):
    """API-8 (poc_api_8)."""

    def test_non_https_ports_and_own_address_refused(self):
        accepted = []
        for url in ("https://93.184.215.62:5432/", "https://93.184.215.62:22/", "https://93.184.215.62:25/",
                    "https://93.184.215.62:8080/", "https://93.184.215.62:3001/"):
            try:
                delivery.check_destination(url)
                accepted.append(url)
            except delivery.WebhookRefused:
                pass
        self.assertEqual(accepted, [])

    def test_port_443_explicit_or_implied_is_fine(self):
        delivery.check_destination("https://93.184.215.14/")
        delivery.check_destination("https://93.184.215.14:443/")

    @override_settings(WEBHOOK_ALLOW_PRIVATE=True, WEBHOOK_ALLOW_HTTP=True)
    def test_development_may_use_other_ports(self):
        delivery.check_destination("http://127.0.0.1:8000/hook")

    @override_settings(WEBHOOK_DENY_ADDRESSES=["93.184.215.62", "2001:db8::5"])
    def test_denied_addresses_are_refused_like_private_ones(self):
        for url in ("https://93.184.215.62/", "https://[2001:db8::5]/", "https://[::ffff:93.184.215.62]/"):
            with self.assertRaises(delivery.WebhookRefused, msg=url) as caught:
                delivery.check_destination(url)
            self.assertEqual(caught.exception.code, "private_address")
        delivery.check_destination("https://93.184.215.14/")

    @override_settings(WEBHOOK_DENY_ADDRESSES=["93.184.215.62"])
    def test_a_name_that_resolves_to_a_denied_address_is_refused(self):
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.215.62", 443))]):
            with self.assertRaises(delivery.WebhookRefused):
                delivery.check_destination("https://app.example.net/")

    def test_a_malformed_deny_entry_stops_the_start_instead_of_failing_open(self):
        """Final review F2: an entry that is not an address used to be logged and skipped, so a typo left
        the host's own addresses reachable. It is a system check error now: `check --deploy` and the
        migrate that starts pay-web refuse to go on."""
        from django.core import checks
        from django.core.management import call_command
        from django.core.management.base import SystemCheckError

        for bad in (["93.184.215.62", "93.184.215"], ["93.184.215.0/24"], ["host.example"], [""]):
            with override_settings(WEBHOOK_DENY_ADDRESSES=bad):
                errors = [m for m in checks.run_checks() if m.id == "webhooks.E001"]
                self.assertEqual(len(errors), 1, bad)
                self.assertTrue(errors[0].is_serious())
                self.assertNotIn("93.184", errors[0].msg)  # says which entry, never its value
                with self.assertRaises(SystemCheckError):
                    call_command("check", "--deploy", stdout=io.StringIO(), stderr=io.StringIO())
        with override_settings(WEBHOOK_DENY_ADDRESSES=["93.184.215.62", " 2001:db8::5 ", "::ffff:198.51.100.7"]):
            self.assertEqual([m for m in checks.run_checks() if m.id == "webhooks.E001"], [])
            call_command("check", "--deploy", stdout=io.StringIO(), stderr=io.StringIO())  # nothing else refuses

    @override_settings(WEBHOOK_DENY_ADDRESSES=["93.184.215"])
    def test_a_malformed_entry_that_slipped_past_the_start_refuses_rather_than_allows(self):
        with self.assertRaises(delivery.WebhookRefused), self.assertLogs("apps.webhooks.delivery", "ERROR"):
            delivery.check_destination("https://93.184.215.14/")

    @override_settings(WEBHOOK_DENY_ADDRESSES=["::ffff:93.184.215.62"])
    def test_an_ipv4_mapped_deny_entry_denies_its_ipv4_address(self):
        with self.assertRaises(delivery.WebhookRefused):
            delivery.check_destination("https://93.184.215.62/")

    def test_deny_list_defaults_to_empty(self):
        from django.conf import settings

        self.assertEqual(list(settings.WEBHOOK_DENY_ADDRESSES), [])
