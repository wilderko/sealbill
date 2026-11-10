"""Webhooks never reach this host's own address (audit SEC-5): WEBHOOK_DENY_ADDRESSES lists it, and
WEBHOOK_ALLOW_URL_PREFIXES names the one receiver delivered to anyway, by URL prefix (review S5, M-1): the
same host's other paths, `/server-status` among them, stay refused."""
import io
import os
import socket
from unittest import mock

from django.core import checks
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import SimpleTestCase, override_settings

from apps.webhooks import delivery

HOST = "93.184.215.62"
PREFIX = "https://liberation.services/payments/webhook/invoicing/"
PLAN = {"WEBHOOK_DENY_ADDRESSES": [HOST], "WEBHOOK_ALLOW_URL_PREFIXES": [PREFIX]}


def resolving(*addresses):
    return mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443))
                                                          for a in addresses])


@override_settings(**PLAN)
class AllowUrlPrefixTests(SimpleTestCase):
    def refused(self, url):
        with self.assertRaises(delivery.WebhookRefused) as caught:
            delivery.check_destination(url)
        return caught.exception.code

    def test_the_receiver_is_delivered_to_on_the_denied_address(self):
        with resolving(HOST):
            delivery.check_destination(PREFIX)
            delivery.check_destination(PREFIX + "?source=pay")
            delivery.check_destination("https://LIBERATION.Services./payments/webhook/invoicing/")
            delivery.check_destination("https://liberation.services:443/payments/webhook/invoicing/")

    def test_another_path_on_the_same_host_is_refused(self):
        for url in ("https://liberation.services/server-status", "https://liberation.services/server-info",
                    "https://liberation.services/", "https://liberation.services/payments/webhook/",
                    "https://liberation.services/payments/webhook/invoicing"):
            with self.subTest(url), resolving(HOST):
                self.assertEqual(self.refused(url), "private_address")

    def test_look_alike_prefixes_are_refused(self):
        for url in ("https://liberation.services/payments/webhook/invoicing/../x",
                    "https://liberation.services/payments/webhook/invoicing/../../../server-status",
                    "https://liberation.services/payments/webhook/invoicing/%2e%2e/server-status",
                    "https://liberation.services/payments/webhook/invoicing/%2E%2E%2Fserver-status",
                    "https://liberation.services/payments/webhook/invoicing/./x",
                    "https://liberation.services/payments/webhook/invoicingX",
                    "https://liberation.services/payments/webhook/invoicingX/",
                    "https://liberation.services.evil.example/payments/webhook/invoicing/",
                    "https://sub.liberation.services/payments/webhook/invoicing/",
                    "https://liberation.services:8443/payments/webhook/invoicing/",
                    f"https://{HOST}/payments/webhook/invoicing/"):
            with self.subTest(url), resolving(HOST):
                self.assertIn(self.refused(url), ("private_address", "port"))

    def test_it_lifts_the_deny_list_only_never_the_private_rule(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254"):
            with self.subTest(address), resolving(HOST, address):
                self.assertEqual(self.refused(PREFIX), "private_address")

    def test_the_connection_applies_it_too(self):
        """The socket opens only to an address checked at connection time, with the URL's own allowance."""
        with override_settings(WEBHOOK_HTTP_ENABLED=True), resolving(HOST), \
                mock.patch("urllib3.util.connection.create_connection", side_effect=ConnectionRefusedError()) as c:
            with self.assertRaises(Exception):
                delivery.post(PREFIX, b"{}", {})
        self.assertEqual(c.call_args.args[0], (HOST, 443))
        with override_settings(WEBHOOK_HTTP_ENABLED=True), resolving(HOST), \
                mock.patch("urllib3.util.connection.create_connection") as c:
            with self.assertRaises(delivery.WebhookRefused):
                delivery.post("https://liberation.services/server-status", b"{}", {})
        c.assert_not_called()

    @override_settings(WEBHOOK_ALLOW_URL_PREFIXES=[])
    def test_without_it_the_receiver_is_refused_like_anything_else_on_the_address(self):
        with resolving(HOST):
            self.assertEqual(self.refused(PREFIX), "private_address")

    @override_settings(WEBHOOK_DENY_ADDRESSES=[])
    def test_with_no_deny_list_it_changes_nothing(self):
        with resolving("93.184.216.34"):
            delivery.check_destination("https://hooks.example.com/")
        with resolving("127.0.0.1"):
            self.assertEqual(self.refused(PREFIX), "private_address")


class AllowUrlPrefixCheckTests(SimpleTestCase):
    def errors(self, check_id="webhooks.E002"):
        return [m for m in checks.run_checks() if m.id == check_id]

    def test_an_entry_must_be_an_https_prefix_ending_in_a_slash(self):
        for bad in ([f"https://{HOST}/x/"], ["https://liberation.services/payments/webhook/invoicing"],
                    ["http://liberation.services/x/"], ["https://*.example/x/"], ["liberation.services/x/"],
                    ["https://liberation.services/x/?a=1"], ["https://user@liberation.services/x/"],
                    ["https://liberation.services/a/../x/"], ["https://liberation.services:8443/x/"], [""]):
            with self.subTest(bad), override_settings(WEBHOOK_ALLOW_URL_PREFIXES=bad):
                errors = self.errors()
                self.assertEqual(len(errors), 1, bad)
                self.assertTrue(errors[0].is_serious())
                self.assertNotIn("93.184", errors[0].msg)
                with self.assertRaises(SystemCheckError):
                    call_command("check", "--deploy", stdout=io.StringIO(), stderr=io.StringIO())

    def test_good_prefixes_pass(self):
        with override_settings(WEBHOOK_ALLOW_URL_PREFIXES=[PREFIX, "https://Shop.Example./hooks/"],
                               WEBHOOK_DENY_ADDRESSES=[HOST]):
            self.assertEqual(self.errors(), [])

    def test_the_old_per_host_setting_refuses_to_start(self):
        with mock.patch.dict(os.environ, {"WEBHOOK_ALLOW_HOSTS": "liberation.services"}):
            errors = self.errors("webhooks.E003")
        self.assertEqual(len(errors), 1)
        self.assertIn("WEBHOOK_ALLOW_URL_PREFIXES", errors[0].msg + (errors[0].hint or ""))
        self.assertEqual(self.errors("webhooks.E003"), [])

    def test_defaults_to_empty(self):
        from django.conf import settings

        self.assertEqual(list(settings.WEBHOOK_ALLOW_URL_PREFIXES), [])
        self.assertFalse(hasattr(settings, "WEBHOOK_ALLOW_HOSTS"))
