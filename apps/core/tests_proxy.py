"""PROXY_SECRET: only the reverse proxy, which sets X-Pay-Proxy-Secret, reaches the application."""
from unittest import mock

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from apps.core.proxy import ProxySecretMiddleware
from apps.vault.testing import UnsealedVaultMixin

SECRET = "proxy-secret-for-tests"
FORGED = "203.0.113.7"  # an address the caller made up


class ForgedForwardedForMixin:
    def assert_never_counted_without_the_secret(self, module, path, data):
        with mock.patch(f"{module}.rate_limited", return_value=True) as limited:
            r = self.client.post(path, data, headers={"X-Forwarded-For": FORGED})
            self.assertEqual(r.status_code, 403)
            limited.assert_not_called()
            # Through the proxy, the same request is counted, under the LAST X-Forwarded-For entry.
            r = self.client.post(path, data, headers={"X-Forwarded-For": f"198.51.100.1, {FORGED}",
                                                      "X-Pay-Proxy-Secret": SECRET})
            self.assertEqual(r.status_code, 429)
            self.assertEqual(limited.call_args_list[0].args[1], FORGED)


@override_settings(PROXY_SECRET=SECRET)
class ProxySecretSetTests(ForgedForwardedForMixin, TestCase):
    def test_request_without_the_header_is_refused(self):
        for path in ("/healthz", "/llms.txt", "/unlock/", "/login/", "/i/some-token/", "/static/x.css"):
            with self.subTest(path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 403)
                self.assertEqual((r.content, r["Content-Type"]), (b"Forbidden\n", "text/plain"))

    def test_wrong_secret_is_refused_and_never_echoed_or_logged(self):
        for wrong in ("wrong-secret-value", SECRET + "x", SECRET[:-1], "", "sécrèt"):
            with self.subTest(wrong=wrong), self.assertLogs("django.request", "WARNING") as logs:
                r = self.client.get("/healthz", headers={"X-Pay-Proxy-Secret": wrong})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.content, b"Forbidden\n")
            if wrong:
                self.assertNotIn(wrong, "\n".join(logs.output))

    def test_right_secret_is_served(self):
        r = self.client.get("/llms.txt", headers={"X-Pay-Proxy-Secret": SECRET})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/healthz", headers={"X-Pay-Proxy-Secret": SECRET})
        self.assertNotEqual(r.status_code, 403)
        self.assertIn(b'"status"', r.content)

    def test_the_header_never_reaches_a_view(self):
        seen = {}

        def view(request):
            seen.update(request.META)
            return HttpResponse("ok")

        request = RequestFactory().get("/healthz", headers={"X-Pay-Proxy-Secret": SECRET})
        self.assertEqual(ProxySecretMiddleware(view)(request).status_code, 200)
        self.assertNotIn("HTTP_X_PAY_PROXY_SECRET", seen)
        self.assertNotIn(SECRET, repr(sorted(seen.items())))

    def test_forged_forwarded_for_never_reaches_the_unlock_limit(self):
        self.assert_never_counted_without_the_secret("apps.vault.views", "/unlock/", {"password": "guess"})

    def test_it_runs_before_every_other_middleware(self):
        self.assertEqual(settings.MIDDLEWARE[0], "apps.core.proxy.ProxySecretMiddleware")


@override_settings(PROXY_SECRET=SECRET)
class ProxySecretUnsealedTests(ForgedForwardedForMixin, UnsealedVaultMixin, TestCase):
    def test_forged_forwarded_for_never_reaches_the_staff_login_limit(self):
        self.assert_never_counted_without_the_secret("apps.staff.views", "/login/",
                                                     {"username": "staff", "password": "guess"})


@override_settings(PROXY_SECRET="")
class ProxySecretEmptyTests(TestCase):
    """Development, tests and Docker: no proxy in front, nothing checked."""

    def test_requests_are_served_with_or_without_the_header(self):
        for headers in ({}, {"X-Pay-Proxy-Secret": "anything"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get("/llms.txt", headers=headers).status_code, 200)

    def test_rate_limits_still_count_the_last_forwarded_for_entry(self):
        with mock.patch("apps.vault.views.rate_limited", return_value=True) as limited:
            r = self.client.post("/unlock/", {"password": "guess"}, headers={"X-Forwarded-For": FORGED})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(limited.call_args.args[:2], ("unlock", FORGED))

    def test_test_settings_leave_it_empty(self):
        from config.settings import test

        self.assertEqual(test.PROXY_SECRET, "")
