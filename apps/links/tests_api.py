"""Payment links in the API (spec 2.4): scopes, isolation, the one place the URL leaves, audit, schema."""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.links import services as links
from apps.links.models import PaymentLink
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin

SITE = "https://pay.example"
BODY = {"title": "Consultation", "currency": "EUR", "price_minor": 10000}


class ApiLinkBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(SITE_URL=SITE))
        self.org = Organization.objects.create(slug="eu")
        self.other = Organization.objects.create(slug="other")
        add_bank(self.org)
        add_bank(self.other)

    def client_for(self, org=None, scopes=("read", "invoices:write")):
        token, plain = ApiToken.issue(org or self.org, "t", list(scopes))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c

    def make(self, org=None, **kw):
        return links.create_link(org or self.org, **{**BODY, **kw})


class ReadTests(ApiLinkBase):
    def test_list_pages_like_the_others_and_never_carries_the_token(self):
        link, plain = self.make()
        self.make(title="Second")
        r = self.client_for().get("/api/v1/payment-links?limit=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        self.assertEqual(len(r.json()["results"]), 1)
        self.assertNotIn(plain, r.content.decode())
        self.assertNotIn("url", r.json()["results"][0])
        self.assertNotIn("token", r.content.decode().lower().replace("token_", ""))

    def test_detail_has_the_url_and_nothing_else_of_the_token(self):
        link, plain = self.make()
        r = self.client_for().get(f"/api/v1/payment-links/{link.pk}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["url"], f"{SITE}/l/{plain}/")
        self.assertEqual((body["title"], body["price_minor"], body["currency"], body["active"]),
                         ("Consultation", 10000, "EUR", True))
        self.assertNotIn(link.token_hash, r.content.decode())
        self.assertEqual(r.content.decode().count(plain), 1)

    def test_another_organizations_link_is_404_everywhere(self):
        link, _ = self.make(org=self.other)
        c = self.client_for()
        self.assertEqual(c.get(f"/api/v1/payment-links/{link.pk}").status_code, 404)
        self.assertEqual(c.patch(f"/api/v1/payment-links/{link.pk}", {"active": False}, format="json").status_code, 404)
        self.assertEqual(c.post(f"/api/v1/payment-links/{link.pk}/rotate").status_code, 404)
        self.assertEqual(c.get("/api/v1/payment-links").json()["count"], 0)
        link.refresh_from_db()
        self.assertTrue(link.active)

    def test_scopes(self):
        link, _ = self.make()
        reader = self.client_for(scopes=("read",))
        writer = self.client_for(scopes=("invoices:write",))
        self.assertEqual(reader.get("/api/v1/payment-links").status_code, 200)
        self.assertEqual(reader.post("/api/v1/payment-links", BODY, format="json").status_code, 403)
        self.assertEqual(reader.patch(f"/api/v1/payment-links/{link.pk}", {"active": False}, format="json").status_code, 403)
        self.assertEqual(reader.post(f"/api/v1/payment-links/{link.pk}/rotate").status_code, 403)
        self.assertEqual(writer.get("/api/v1/payment-links").status_code, 403)
        self.assertEqual(writer.get(f"/api/v1/payment-links/{link.pk}").status_code, 403)
        self.assertEqual(APIClient().get("/api/v1/payment-links").status_code, 401)

    def test_invoices_carry_the_link_and_coupon(self):
        link, _ = self.make()
        links.add_coupon(link, code="SAVE10", percent_bps=1000)
        invoice, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="B",
                                                         coupon_code="SAVE10"))
        ordinary = self.ordinary_invoice()
        c = self.client_for()
        body = c.get(f"/api/v1/invoices/{invoice.pk}").json()
        self.assertEqual((body["payment_link_id"], body["coupon_code"]), (link.pk, "SAVE10"))
        body = c.get(f"/api/v1/invoices/{ordinary.pk}").json()
        self.assertEqual((body["payment_link_id"], body["coupon_code"]), (None, None))
        rows = {r["id"]: r for r in c.get("/api/v1/invoices").json()["results"]}
        self.assertEqual(rows[invoice.pk]["payment_link_id"], link.pk)

    def ordinary_invoice(self):
        from apps.contacts.services import find_or_create_contact
        from apps.invoices.services import create_invoice

        contact, _ = find_or_create_contact(self.org, "x@example.com", legal_name="X")
        return create_invoice(self.org, contact, currency="EUR",
                              lines=[{"name": "A", "quantity": 1, "unit_price_minor": 100}])[0]


class WriteTests(ApiLinkBase):
    def test_create_returns_the_url_and_audits_with_the_api_channel_and_no_token(self):
        r = self.client_for().post("/api/v1/payment-links", {**BODY, "description": "One hour", "max_quantity": 3,
                                                              "tax_rate_bps": 2000}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        link = PaymentLink.objects.get(pk=r.json()["id"])
        self.assertEqual(link.org, self.org)
        self.assertEqual(r.json()["url"], f"{SITE}/l/{link.token}/")
        self.assertEqual((link.max_quantity, link.tax_rate_bps), (3, 2000))
        entry = AuditEntry.objects.get(action="link.create")
        self.assertEqual(entry.detail["channel"], "api")
        self.assertNotIn(link.token, str(entry.detail))
        self.assertTrue(entry.actor_username)

    def test_create_uses_the_staff_validation(self):
        c = self.client_for()
        for bad in ({**BODY, "price_minor": 0}, {**BODY, "currency": "XXX"}, {**BODY, "title": ""},
                    {**BODY, "tax_rate_bps": 20000}, {**BODY, "kind": "nope"}, {**BODY, "max_uses": 0},
                    {**BODY, "customer_chooses": True, "min_minor": 500, "max_minor": 100},
                    {**BODY, "methods": "custom", "method_types": []}):
            r = c.post("/api/v1/payment-links", bad, format="json")
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(PaymentLink.objects.count(), 0)

    def test_organization_cannot_be_chosen(self):
        r = self.client_for().post("/api/v1/payment-links", {**BODY, "org": "other", "organization": self.other.pk},
                                   format="json")
        self.assertIn(r.status_code, (201, 400))
        self.assertEqual(PaymentLink.objects.exclude(org=self.org).count(), 0)

    def test_patch_updates_and_audits(self):
        link, plain = self.make()
        r = self.client_for().patch(f"/api/v1/payment-links/{link.pk}", {"title": "New", "price_minor": 500},
                                    format="json")
        self.assertEqual(r.status_code, 200, r.content)
        link.refresh_from_db()
        self.assertEqual((link.title, link.price_minor, link.token), ("New", 500, plain))
        entry = AuditEntry.objects.get(action="link.update")
        self.assertEqual(entry.detail["channel"], "api")
        self.assertEqual(entry.detail["fields"], ["price_minor", "title"])

    def test_active_false_deactivates_and_the_public_page_answers_unavailable(self):
        link, plain = self.make()
        r = self.client_for().patch(f"/api/v1/payment-links/{link.pk}", {"active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["active"])
        entry = AuditEntry.objects.get(action="link.deactivate")
        self.assertEqual(entry.detail["channel"], "api")
        self.assertIn(b"no longer available", self.client.get(f"/l/{plain}/").content)

    def test_patch_cannot_change_the_token_or_org(self):
        link, plain = self.make()
        self.client_for().patch(f"/api/v1/payment-links/{link.pk}",
                                {"token": "x" * 40, "token_hash": "y", "org": "other"}, format="json")
        link.refresh_from_db()
        self.assertEqual((link.token, link.org), (plain, self.org))

    def test_rotate_returns_the_new_url_kills_the_old_and_audits(self):
        link, old = self.make()
        r = self.client_for().post(f"/api/v1/payment-links/{link.pk}/rotate")
        self.assertEqual(r.status_code, 200)
        link.refresh_from_db()
        self.assertNotEqual(link.token, old)
        self.assertEqual(r.json()["url"], f"{SITE}/l/{link.token}/")
        self.assertEqual(self.client.get(f"/l/{old}/").status_code, 404)
        entry = AuditEntry.objects.get(action="link.rotate")
        self.assertEqual(entry.detail["channel"], "api")
        self.assertNotIn(old, str(entry.detail))


class SchemaTests(TestCase):
    def test_the_schema_validates_without_warnings_and_documents_the_endpoints(self):
        out, err = StringIO(), StringIO()
        call_command("spectacular", "--validate", "--fail-on-warn", "--format", "openapi-json", stdout=out, stderr=err)
        schema = out.getvalue()
        for path in ("/api/v1/payment-links", "/api/v1/payment-links/{id}", "/api/v1/payment-links/{id}/rotate"):
            self.assertIn(f'"{path}"', schema)
        self.assertIn("payment_link_id", schema)
        self.assertIn("coupon_code", schema)

    def test_llms_txt_describes_them(self):
        text = self.client.get("/llms.txt").content.decode()
        self.assertIn("/api/v1/payment-links", text)
        self.assertIn("rotate", text)
