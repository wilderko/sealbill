"""Final review I4: every per-address rate-limit bucket counts an IPv6 address by its /64
(`address_bucket`), which one subscriber is handed whole and can rotate through at will. A bucket
keyed on the full address gave each of 2**64 addresses an allowance of its own."""
import re
from unittest import mock

from django.core.cache import caches
from django.test import TestCase

from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.keys import services as keys
from apps.links import services as links
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault import ratelimit
from apps.vault.testing import UnsealedVaultMixin

ONE = "2001:db8:a:b::1"
SAME_64 = "2001:db8:a:b:ffff:1:2:3"
NET = "2001:db8:a:b::/64"
# A literal IPv6 address in a key: one address counted on its own.
FULL_V6 = re.compile(r"[0-9a-f]{0,4}(:[0-9a-f]{0,4}){2,7}(?<!:)(?!/)", re.I)


class AddressBucketBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        caches["ratelimit"].clear()
        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        bank = add_bank(org)
        contact, _ = find_or_create_contact(org, "a@example.com", legal_name="Client")
        inv, _ = create_invoice(org, contact, currency="EUR", offered_methods=[f"bank:{bank.pk}"],
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        _inv, self.view, self.pay = send_invoice(inv, email_client=False)
        _link, self.link_token = links.create_link(org, title="T", currency="EUR", price_minor=10000)
        self.contact = contact

    def pages(self):
        """(name, method, path) of every endpoint whose rate limits count the caller's address."""
        return [
            ("portal page", "get", f"/i/{self.view}/"),
            ("portal miss", "get", "/i/" + "x" * 43 + "/"),
            ("link page", "get", f"/l/{self.link_token}/"),
            ("link miss", "get", "/l/" + "y" * 43 + "/"),
            ("key confirm miss", "get", "/k/" + "z" * 43 + "/"),
            ("key cancel miss", "get", "/k/" + "z" * 43 + "/cancel/"),
            ("btcpay hook", "post", "/hooks/btcpay/999999/"),
            ("api schema", "get", "/api/v1/schema"),
            ("staff login", "post", "/login/"),
            ("vault unlock", "post", "/unlock/"),
        ]


class EveryAddressBucketIsA64Tests(AddressBucketBase):
    def test_no_bucket_counts_one_ipv6_address_on_its_own(self):
        seen = []
        real = ratelimit.count_attempt

        def spy(bucket, ident, window_s):
            seen.append((bucket, ident))
            return real(bucket, ident, window_s)

        with mock.patch.object(ratelimit, "count_attempt", side_effect=spy):
            for name, method, path in self.pages():
                getattr(self.client, method)(path, REMOTE_ADDR=ONE)
        buckets = {bucket for bucket, _ident in seen}
        for expected in ("portal", "portal-miss", "link-page", "link-miss", "key-confirm-miss", "btcpay-hook",
                         "api-schema", "staff-login"):
            self.assertIn(expected, buckets)
        for bucket, ident in seen:
            self.assertNotIn(ONE, ident, bucket)
            self.assertIsNone(FULL_V6.search(ident.replace(NET, "")), (bucket, ident))
        by_address = [ident for bucket, ident in seen if bucket not in ("staff-login-user", "staff-login-device")]
        self.assertTrue(all(ident.startswith(NET) for ident in by_address), by_address)


class TwoAddressesOfOne64ShareABucketTests(AddressBucketBase):
    def exhaust_then_try(self, module, constant, path, method="get", status=429):
        with mock.patch.object(module, constant, 2):
            for _ in range(2):
                self.assertNotEqual(getattr(self.client, method)(path, REMOTE_ADDR=ONE).status_code, 429, path)
            self.assertEqual(getattr(self.client, method)(path, REMOTE_ADDR=SAME_64).status_code, status, path)
            self.assertNotEqual(getattr(self.client, method)(path, REMOTE_ADDR="2001:db8:a:c::1").status_code, 429,
                                path)

    def test_the_portal(self):
        from apps.portal import views

        self.exhaust_then_try(views, "RATE_LIMIT", f"/i/{self.view}/")
        self.exhaust_then_try(views, "MISS_LIMIT", "/i/" + "x" * 43 + "/")

    def test_payment_links(self):
        from apps.links import views

        self.exhaust_then_try(views, "PAGE_LIMIT", f"/l/{self.link_token}/")
        self.exhaust_then_try(views, "MISS_LIMIT", "/l/" + "y" * 43 + "/")

    def test_key_confirmation(self):
        from apps.keys import views

        self.exhaust_then_try(views, "MISS_LIMIT", "/k/" + "z" * 43 + "/")

    def test_btcpay_hook_and_api_schema(self):
        from apps.api import views as api
        from apps.rails import views as rails

        self.exhaust_then_try(rails, "RATE_PER_MINUTE", "/hooks/btcpay/999999/", method="post")
        self.exhaust_then_try(api, "SCHEMA_PER_MINUTE", "/api/v1/schema")

    def test_staff_login(self):
        for i in range(10):
            self.client.post("/login/", {"username": f"nobody{i}", "password": "x"}, REMOTE_ADDR=ONE)
        r = self.client.post("/login/", {"username": "nobody-else", "password": "x"}, REMOTE_ADDR=SAME_64)
        self.assertEqual(r.status_code, 429)
