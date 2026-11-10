"""Task 6 (API-2, API-6, API-7): ported from poc_api_2/6/7, with the assertions kept."""
import time
from io import StringIO
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.management import call_command
from django.test import Client, TestCase
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.orgs.models import FiatLink, Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks import delivery

BANK_FORM = {
    "beneficiary_name": "Seller", "currency": "EUR", "label": "EUR account", "active": "on",
    "instructions": "null", "beneficiary_address": "", "bank_name": "", "bank_address": "", "bank_swift": "",
    "intermediary_bank_name": "", "intermediary_swift": "", "intermediary_account": "",
}


class HeadScopeTests(UnsealedVaultMixin, TestCase):
    """API-2 (poc_api_2)."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.215.14", 443))]):
            self.ep, _ = delivery.create_endpoint(self.org, "https://hooks.example.com/in?token=abc", events=["*"])
        _, plain = ApiToken.issue(self.org, "reader", ["read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")

    def test_get_is_refused_for_a_read_token(self):
        self.assertEqual(self.c.get("/api/v1/webhooks").status_code, 403)
        self.assertEqual(self.c.get(f"/api/v1/webhooks/{self.ep.pk}/deliveries").status_code, 403)

    def test_head_is_refused_for_a_read_token(self):
        calls = []
        from apps.api import views

        real = views.WebhookList.get

        def spy(self_, request):
            calls.append(request.method)
            return real(self_, request)

        with mock.patch.object(views.WebhookList, "get", spy):
            r = self.c.head("/api/v1/webhooks")
        self.assertEqual(self.c.head("/api/v1/webhooks/999999").status_code, 403)
        self.assertEqual(self.c.head(f"/api/v1/webhooks/{self.ep.pk}").status_code, 403)
        self.assertEqual(calls, [], "the admin-only view ran")
        self.assertEqual(r.status_code, 403)

    def test_options_follows_get(self):
        self.assertEqual(self.c.options("/api/v1/webhooks").status_code, 403)
        self.assertEqual(self.c.options("/api/v1/me").status_code, 200)

    def test_head_on_a_read_view_still_works_for_a_read_token(self):
        self.assertEqual(self.c.head("/api/v1/me").status_code, 200)

    def test_an_admin_token_may_head_the_admin_view(self):
        _, plain = ApiToken.issue(self.org, "adm", ["admin"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        self.assertEqual(c.head("/api/v1/webhooks").status_code, 200)


class SchemaThrottleTests(TestCase):
    """API-6 (poc_api_6): the poc timed the rebuild; here the throttle and the cache are asserted."""

    def setUp(self):
        caches["ratelimit"].clear()
        from apps.api import views

        views._SCHEMA_CACHE.clear()

    def test_schema_is_throttled_per_address(self):
        c = Client()
        codes = [c.get("/api/v1/schema").status_code for _ in range(32)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertIn(429, codes)
        self.assertEqual(Client().get("/api/v1/schema", HTTP_X_FORWARDED_FOR="203.0.113.9").status_code, 200)

    def test_docs_are_throttled_too(self):
        c = Client()
        codes = [c.get("/api/v1/docs").status_code for _ in range(32)]
        self.assertIn(429, codes)

    def test_schema_is_built_once_per_process(self):
        from drf_spectacular.generators import SchemaGenerator

        c = Client()
        with mock.patch.object(SchemaGenerator, "get_schema", autospec=True,
                               side_effect=SchemaGenerator.get_schema) as built:
            first = c.get("/api/v1/schema")
            second = c.get("/api/v1/schema")
        self.assertEqual(built.call_count, 1, "the schema was rebuilt")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)


class TokenAuditTests(UnsealedVaultMixin, TestCase):
    """API-7 (poc_api_7)."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.bank = add_bank(self.org, account_number="SK1111111111111111111111")
        self.staff = get_user_model().objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(self.staff)
        session = self.client.session
        session["staff_verified_at"] = int(time.time())  # a fresh second factor: admin changes need it
        session.save()

    def _admin(self):
        # admin.site is a LazyObject and the staff app set `has_permission` on the wrapped site as an
        # instance attribute; patch.object would delete it on exit (the poc did, breaking later tests).
        return mock.patch.dict(admin.site._wrapped.__dict__, {"has_permission": lambda request: True})

    def test_token_lifecycle_is_audited(self):
        before = AuditEntry.objects.count()
        call_command("create_api_token", org="eu", name="shop", scopes="read", stdout=StringIO())
        token = ApiToken.objects.get(name="shop")
        with self._admin():
            r = self.client.post(f"/hidden-admin-xyz/api/apitoken/{token.pk}/change/", {
                "scopes": '["read", "invoices:write", "payments:confirm", "admin"]',
                "revoked_at_0": "", "revoked_at_1": ""})
        token.refresh_from_db()
        call_command("revoke_api_token", "shop", stdout=StringIO())
        self.assertIn("admin", token.scopes)
        self.assertGreaterEqual(AuditEntry.objects.count() - before, 3, "token lifecycle is not in the audit log")
        actions = sorted(AuditEntry.objects.filter(model_label="api.apitoken").values_list("action", flat=True))
        self.assertEqual(actions, ["api_token.create", "api_token.revoke", "api_token.scopes"])
        self.assertEqual(r.status_code, 302)

    def test_entries_name_scopes_and_channel_never_the_token(self):
        token, plain = ApiToken.issue(self.org, "shop", ["read"])
        entry = AuditEntry.objects.get(action="api_token.create")
        text = repr(entry.detail) + repr(entry.system_detail) + entry.object_pk + entry.actor_username
        self.assertNotIn(plain, text)
        self.assertNotIn(token.token_hash, text)
        detail = entry.detail or entry.system_detail
        self.assertEqual(detail["scopes"], ["read"])
        self.assertIn("channel", detail)

    def test_admin_refuses_a_string_for_scopes(self):
        token, _ = ApiToken.issue(self.org, "shop", ["read"])
        with self._admin():
            r = self.client.post(f"/hidden-admin-xyz/api/apitoken/{token.pk}/change/", {
                "scopes": '"admin"', "revoked_at_0": "", "revoked_at_1": ""})
        self.assertEqual(r.status_code, 200)  # the form again, with an error
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["read"])
        with self._admin():
            r = self.client.post(f"/hidden-admin-xyz/api/apitoken/{token.pk}/change/", {
                "scopes": '["read", "root"]', "revoked_at_0": "", "revoked_at_1": ""})
        self.assertEqual(r.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["read"])

    def test_bank_account_change_is_audited(self):
        before = AuditEntry.objects.count()
        with self._admin():
            r = self.client.post(f"/hidden-admin-xyz/orgs/bankaccount/{self.bank.pk}/change/", {
                **BANK_FORM, "org": self.org.pk, "account_number": "GB00ATTACKER0000000000"})
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.account_number, "GB00ATTACKER0000000000")
        self.assertGreaterEqual(AuditEntry.objects.count() - before, 1, "a bank-detail change is not audited")
        entry = AuditEntry.objects.filter(model_label="orgs.bankaccount").latest("pk")
        detail = entry.detail
        self.assertIn("account_number", detail["fields"])
        self.assertNotIn("ATTACKER", repr(detail) + repr(entry.system_detail))
        self.assertEqual(entry.actor_id, self.staff.pk)
        self.assertEqual(r.status_code, 302)

    def test_organization_change_is_audited_with_field_names_only(self):
        with self._admin():
            r = self.client.get(f"/hidden-admin-xyz/orgs/organization/{self.org.pk}/change/")
            self.assertEqual(r.status_code, 200)
        self.org.is_us_resident_issuer = True
        form = {"slug": "eu", "default_language": self.org.default_language, "is_us_resident_issuer": "on"}
        # Drive the admin's own save path: the changed field names are what is recorded.
        ma = admin.site._registry[Organization]
        request = mock.Mock(user=self.staff)

        class F:
            changed_data = ["is_us_resident_issuer"]

        ma.save_model(request, self.org, F(), True)
        entry = AuditEntry.objects.get(action="organization.update")
        self.assertEqual(entry.detail["fields"], ["is_us_resident_issuer"])
        self.assertNotIn("True", repr(entry.detail))
        del form

    def test_fiat_link_change_is_audited(self):
        link = FiatLink.objects.create(org=self.org, kind="paypal", label="PP")
        ma = admin.site._registry[FiatLink]

        class F:
            changed_data = ["url"]

        ma.save_model(mock.Mock(user=self.staff), link, F(), True)
        entry = AuditEntry.objects.get(action="fiatlink.update")
        self.assertEqual(entry.detail["fields"], ["url"])
