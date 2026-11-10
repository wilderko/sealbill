import datetime
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.audit.models import AuditEntry
from apps.orgs.models import BankAccount, FiatLink
from apps.payments.models import Payment
from apps.vault.fields import UndecryptableValue
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

LINE = {"name": "Consulting", "quantity": "1", "unit_price_minor": 10000}
BODY = {"contact": {"email": "a@example.com", "legal_name": "A"}, "currency": "EUR", "lines": [LINE]}


class ApiBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.other = Organization.objects.create(slug="other")
        self.us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        for org in (self.org, self.other):
            add_bank(org)

    def client_for(self, org=None, scopes=("read", "invoices:write", "payments:confirm"), name="t"):
        token, plain = ApiToken.issue(org or self.org, name, list(scopes))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c, token, plain

    def invoice(self, org=None, send=False):
        org = org or self.org
        contact, _ = find_or_create_contact(org, "x@example.com", legal_name="X")
        inv = create_invoice(org, contact, currency="EUR", lines=[{**LINE, "quantity": 1}])[0]
        if send:
            send_invoice(inv, email_client=False)
        return inv


class AuthTests(ApiBase):
    def test_no_token_401_revoked_token_401(self):
        self.assertEqual(APIClient().get("/api/v1/invoices").status_code, 401)
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(bad.get("/api/v1/invoices").status_code, 401)
        c, token, _ = self.client_for()
        self.assertEqual(c.get("/api/v1/invoices").status_code, 200)
        token.revoked_at = timezone.now()
        token.save()
        self.assertEqual(c.get("/api/v1/invoices").status_code, 401)

    def test_only_a_hash_is_stored(self):
        _, token, plain = self.client_for()
        self.assertNotEqual(token.token_hash, plain)
        self.assertEqual(len(token.token_hash), 64)
        self.assertFalse(token.service_user.is_active)
        self.assertEqual(token.service_user.username, "api:t")

    def test_last_used_updates_at_most_once_a_minute(self):
        c, token, _ = self.client_for()
        c.get("/api/v1/invoices")
        token.refresh_from_db()
        first = token.last_used_at
        self.assertIsNotNone(first)
        c.get("/api/v1/invoices")
        token.refresh_from_db()
        self.assertEqual(token.last_used_at, first)

    def test_command_prints_token_once(self):
        out = StringIO()
        call_command("create_api_token", org="eu", name="cli", scopes="read,invoices:write", stdout=out)
        plain = [l for l in out.getvalue().splitlines() if l.strip()][-1].strip()
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        self.assertEqual(c.get("/api/v1/contacts").status_code, 200)

    def test_throttle_per_token(self):
        c, _, _ = self.client_for()
        codes = {c.get("/api/v1/contacts").status_code for _ in range(125)}
        self.assertIn(429, codes)

    def test_sealed_returns_503(self):
        c, _, _ = self.client_for()
        vault.seal()
        r = c.get("/api/v1/invoices")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"error": "vault_sealed"})
        self.assertEqual(APIClient().get("/llms.txt").status_code, 200)
        self.assertEqual(APIClient().get("/api/v1/schema").status_code, 200)


class IsolationTests(ApiBase):
    def test_token_cannot_see_other_org(self):
        mine = self.invoice()
        theirs = self.invoice(org=self.other)
        c, _, _ = self.client_for()
        self.assertEqual(c.get(f"/api/v1/invoices/{mine.pk}").status_code, 200)
        self.assertEqual(c.get(f"/api/v1/invoices/{theirs.pk}").status_code, 404)
        self.assertEqual(c.get(f"/api/v1/invoices/{theirs.pk}/pdf").status_code, 404)
        ids = [i["id"] for i in c.get("/api/v1/invoices").json()["results"]]
        self.assertEqual(ids, [mine.pk])
        self.assertEqual(c.post(f"/api/v1/invoices/{theirs.pk}/void", {}, format="json").status_code, 404)
        cid = theirs.contact_id
        self.assertEqual(c.get(f"/api/v1/contacts/{cid}").status_code, 404)

    def test_requester_sees_invoice_issued_by_rule(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", {**BODY, "contact": {**BODY["contact"], "us_tax_resident": "yes"}}, format="json")
        iid = r.json()["id"]
        self.assertEqual(c.get(f"/api/v1/invoices/{iid}").status_code, 200)
        us_client, _, _ = self.client_for(org=self.us)
        self.assertEqual(us_client.get(f"/api/v1/invoices/{iid}").status_code, 200)
        oc, _, _ = self.client_for(org=self.other)
        self.assertEqual(oc.get(f"/api/v1/invoices/{iid}").status_code, 404)


class ReissueLinksTests(ApiBase):
    def test_reissue_cancels_queued_letters_with_the_old_links(self):
        from apps.invoices.services import queue_invoice_letter
        from apps.notify.models import OutboxMessage

        inv = self.invoice(send=True)
        old = queue_invoice_letter(inv, "x@example.com", "OLDVIEW", "OLDPAY")
        c, _, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/reissue-links", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(OutboxMessage.objects.get(pk=old.pk).state, "cancelled")


class ContactTests(ApiBase):
    def test_patch_reaches_the_us_issuer_copy(self):
        from apps.contacts.models import Contact

        add_bank(self.us)
        c, _, _ = self.client_for()
        body = {**BODY, "contact": {"email": "us@example.com", "legal_name": "Old Name", "us_tax_resident": "yes",
                                    "address": {"line1": "1 Old St", "city": "Austin", "country": "US"}}}
        invoice = Invoice.objects.get(pk=c.post("/api/v1/invoices", body, format="json").json()["id"])
        copy = invoice.contact
        original = Contact.objects.get(org=self.org, pk=c.get("/api/v1/contacts?email=us@example.com").json()
                                       ["results"][0]["id"])
        self.assertEqual((copy.org_id, original.org_id), (self.us.pk, self.org.pk))
        r = c.patch(f"/api/v1/contacts/{original.pk}",
                    {"legal_name": "New Name LLC", "phone": "+1 555 0100", "address": {"line1": "2 New Ave"}},
                    format="json")
        self.assertEqual(r.status_code, 200)
        copy.refresh_from_db()
        self.assertEqual((copy.legal_name, copy.phone), ("New Name LLC", "+1 555 0100"))
        # the staff form's rule: the parts given merge over what the copy holds
        self.assertEqual(copy.address, {"line1": "2 New Ave", "city": "Austin", "country": "US"})
        entry = AuditEntry.objects.filter(action="contact.update", object_pk=str(copy.pk)).latest("pk")
        self.assertEqual(entry.detail["fields"], ["address", "legal_name", "phone"])
        # "unknown" is the blank of the tri-state: it never downgrades the copy's yes
        c.patch(f"/api/v1/contacts/{original.pk}", {"us_tax_resident": "unknown", "address": None}, format="json")
        copy.refresh_from_db()
        self.assertEqual((copy.us_tax_resident, copy.address), ("yes", None))
        # a contact of another organization is never touched
        stranger, _ = find_or_create_contact(self.other, "us@example.com", legal_name="Stranger")
        c.patch(f"/api/v1/contacts/{original.pk}", {"legal_name": "Newer"}, format="json")
        stranger.refresh_from_db()
        self.assertEqual(stranger.legal_name, "Stranger")

    def test_create_get_patch(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/contacts", {"email": "n@example.com", "legal_name": "N"}, format="json")
        self.assertEqual(r.status_code, 201)
        cid = r.json()["id"]
        self.assertEqual(r.json()["emails"], ["n@example.com"])
        again = c.post("/api/v1/contacts", {"email": "N@example.com"}, format="json")
        self.assertEqual((again.status_code, again.json()["id"]), (200, cid))
        p = c.patch(f"/api/v1/contacts/{cid}", {"display_name": "Nn", "preferred_language": "sk"}, format="json")
        self.assertEqual(p.status_code, 200)
        self.assertEqual(p.json()["display_name"], "Nn")
        self.assertEqual(c.get("/api/v1/contacts").json()["count"], 1)

    def test_empty_email_is_400(self):
        c, _, _ = self.client_for()
        self.assertEqual(c.post("/api/v1/contacts", {"email": ""}, format="json").status_code, 400)

    def test_read_scope_cannot_write(self):
        c, _, _ = self.client_for(scopes=["read"])
        self.assertEqual(c.post("/api/v1/contacts", {"email": "n@example.com"}, format="json").status_code, 403)


class InvoiceTests(ApiBase):
    def test_read_scope_cannot_create_invoice(self):
        c, _, _ = self.client_for(scopes=["read"])
        r = c.post("/api/v1/invoices", BODY, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_create_invoice_idempotent_by_header(self):
        c, _, _ = self.client_for()
        a = c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        b = c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(Invoice.objects.count(), 1)

    def test_create_idempotent_by_external_reference_and_filter(self):
        c, _, _ = self.client_for()
        a = c.post("/api/v1/invoices", {**BODY, "external_reference": "ord-9"}, format="json")
        b = c.post("/api/v1/invoices", {**BODY, "external_reference": "ord-9"}, format="json")
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        found = c.get("/api/v1/invoices?external_reference=ord-9").json()["results"]
        self.assertEqual([i["id"] for i in found], [a.json()["id"]])
        self.assertEqual(c.get("/api/v1/invoices?external_reference=zzz").json()["results"], [])

    def test_create_body_shape(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", {**BODY, "title": "T", "due_days": 14,
                                       "lines": [{**LINE, "tax_rate_bps": 2000}]}, format="json")
        j = r.json()
        self.assertEqual(j["status"], "draft")
        self.assertEqual(j["org"], "eu")
        self.assertEqual(j["issuer_org"], "eu")
        self.assertEqual(j["requested_by_org"], "eu")
        self.assertFalse(j["issued_by_rule"])
        self.assertEqual(j["totals"], {"subtotal": 10000, "discount": 0, "tax": 2000, "total": 12000})
        self.assertEqual(j["lines"][0]["name"], "Consulting")
        self.assertEqual(j["lines"][0]["quantity"], "1.00")
        self.assertNotIn("view_url", j)

    def test_create_reports_issuer_switch_for_us_resident(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", {**BODY, "currency": "USD",
                                       "contact": {**BODY["contact"], "us_tax_resident": "yes"}}, format="json")
        self.assertEqual(r.status_code, 201)
        j = r.json()
        self.assertTrue(j["issued_by_rule"])
        self.assertEqual(j["issuer_org"], "us")
        self.assertEqual(j["org"], "us")
        self.assertEqual(j["requested_by_org"], "eu")

    def test_business_errors_are_not_500(self):
        c, _, _ = self.client_for()
        for body in ({**BODY, "currency": "ZZZ"}, {**BODY, "lines": []}, {**BODY, "lines": [{"name": "x"}]},
                     {**BODY, "lines": [{**LINE, "quantity": "1.234"}]}, {"currency": "EUR", "lines": [LINE]}):
            r = c.post("/api/v1/invoices", body, format="json")
            self.assertEqual(r.status_code, 400, body)
            self.assertIn("error", r.json())

    def test_send_returns_links_once(self):
        inv = self.invoice()
        c, _, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/send", {"email_client": False}, format="json")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["view_url"].startswith("http"))
        self.assertIn("/p/", j["pay_url"])
        self.assertEqual(j["status"], "sent")
        self.assertTrue(j["number"])
        plain_view = j["view_url"].rstrip("/").rsplit("/", 1)[1]
        plain_pay = j["pay_url"].rstrip("/").rsplit("/", 1)[1]
        for url in (f"/api/v1/invoices/{inv.pk}", "/api/v1/invoices"):
            body = c.get(url).content.decode()
            self.assertNotIn(plain_view, body)
            self.assertNotIn(plain_pay, body)
            self.assertNotIn("view_url", body)
        again = c.post(f"/api/v1/invoices/{inv.pk}/send", {"email_client": False}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["error"], "invoice_error")

    def test_send_emails_by_default(self):
        inv = self.invoice()
        c, _, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/send", {}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_reissue_links_returns_new_urls_once(self):
        inv = self.invoice(send=True)
        c, _, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/reissue-links", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/i/", r.json()["view_url"])
        draft = self.invoice()
        self.assertEqual(c.post(f"/api/v1/invoices/{draft.pk}/reissue-links", {}, format="json").status_code, 409)

    def test_the_api_reissue_does_not_emit_links_reissued(self):
        # The caller of this endpoint has just stored the new links itself.
        from apps.webhooks.models import WebhookEvent

        inv = self.invoice(send=True)
        c, _, _ = self.client_for()
        self.assertEqual(c.post(f"/api/v1/invoices/{inv.pk}/reissue-links", {}, format="json").status_code, 200)
        self.assertFalse(WebhookEvent.objects.filter(type="invoice.links_reissued").exists())

    def test_void_uses_the_token_user_and_refuses_paid(self):
        inv = self.invoice(send=True)
        c, token, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/void", {"reason": "dup"}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "void"))
        inv.refresh_from_db()
        self.assertEqual(inv.voided_by, token.service_user)
        paid = self.invoice(send=True)
        from apps.payments.services import record_manual_payment
        from apps.payments.testing import person

        record_manual_payment(paid, amount_minor=10000, rail="bank", note="", user=person())
        self.assertEqual(c.post(f"/api/v1/invoices/{paid.pk}/void", {}, format="json").status_code, 409)

    def test_void_needs_invoices_write(self):
        inv = self.invoice(send=True)
        c, _, _ = self.client_for(scopes=["read", "payments:confirm"])
        self.assertEqual(c.post(f"/api/v1/invoices/{inv.pk}/void", {}, format="json").status_code, 403)

    def test_pdf(self):
        inv = self.invoice(send=True)
        c, _, _ = self.client_for()
        r = c.get(f"/api/v1/invoices/{inv.pk}/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_admin_scope_implies_all(self):
        c, _, _ = self.client_for(scopes=["admin"])
        self.assertEqual(c.post("/api/v1/invoices", BODY, format="json").status_code, 201)


class PaymentTests(ApiBase):
    def test_manual_payment_and_scope(self):
        inv = self.invoice(send=True)
        w, _, _ = self.client_for(scopes=["read", "invoices:write"])
        self.assertEqual(w.post(f"/api/v1/invoices/{inv.pk}/payments",
                                {"amount_minor": 10000, "rail": "bank"}, format="json").status_code, 403)
        c, token, _ = self.client_for()
        r = c.post(f"/api/v1/invoices/{inv.pk}/payments", {"amount_minor": 10000, "rail": "bank", "note": "wire"},
                   format="json")
        # Only a person records a manual payment (review S1, F1): a token asserts nothing about money.
        self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"))
        self.assertFalse(Payment.objects.exists())
        inv.refresh_from_db()
        self.assertEqual(inv.status, "sent")
        from apps.payments.services import record_manual_payment
        from apps.payments.testing import person

        record_manual_payment(inv, amount_minor=10000, rail="bank", note="wire", user=person())
        inv.refresh_from_db()
        self.assertEqual(inv.status, "paid")
        listed = c.get("/api/v1/payments").json()["results"]
        self.assertEqual(len(listed), 1)
        bad = c.post(f"/api/v1/invoices/{inv.pk}/payments", {"amount_minor": 5, "rail": "wire"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_confirm_requires_payments_confirm_scope(self):
        inv = self.invoice(send=True)
        p = Payment.objects.create(invoice=inv, rail="bank", amount_minor=10000, received_amount=100, state="detected")
        w, _, _ = self.client_for(scopes=["read", "invoices:write"])
        self.assertEqual(w.post(f"/api/v1/payments/{p.pk}/confirm", {}, format="json").status_code, 403)
        c, _, _ = self.client_for()
        r = c.post(f"/api/v1/payments/{p.pk}/confirm", {"note": "ok"}, format="json")
        self.assertEqual((r.status_code, r.json()["state"]), (200, "confirmed"))
        again = c.post(f"/api/v1/payments/{p.pk}/confirm", {}, format="json")
        self.assertEqual(again.status_code, 409)

    def test_reject_and_reverse(self):
        inv = self.invoice(send=True)
        p = Payment.objects.create(invoice=inv, rail="bank", amount_minor=10000, received_amount=100, state="detected")
        c, _, _ = self.client_for()
        self.assertEqual(c.post(f"/api/v1/payments/{p.pk}/reject", {"note": "no"}, format="json").json()["state"], "rejected")
        q = Payment.objects.create(invoice=inv, rail="bank", amount_minor=10000, received_amount=100, state="detected")
        c.post(f"/api/v1/payments/{q.pk}/confirm", {}, format="json")
        self.assertEqual(c.post(f"/api/v1/payments/{q.pk}/reverse", {}, format="json").status_code, 400)
        r = c.post(f"/api/v1/payments/{q.pk}/reverse", {"reason": "bounced"}, format="json")
        self.assertEqual(r.json()["state"], "reversed")

    def test_other_org_payment_is_404(self):
        inv = self.invoice(org=self.other, send=True)
        p = Payment.objects.create(invoice=inv, rail="bank", amount_minor=10000, received_amount=100, state="detected")
        c, _, _ = self.client_for()
        self.assertEqual(c.post(f"/api/v1/payments/{p.pk}/confirm", {}, format="json").status_code, 404)
        self.assertEqual(c.get("/api/v1/payments").json()["results"], [])

    def test_declarations(self):
        from apps.payments.services import declare_payment

        inv = self.invoice(send=True)
        d = declare_payment(inv, note="paid")
        c, _, _ = self.client_for()
        rows = c.get("/api/v1/declarations").json()["results"]
        self.assertEqual([(r["id"], r["state"]) for r in rows], [(d.pk, "open")])
        r = c.post(f"/api/v1/declarations/{d.pk}/dismiss", {"reason": "x"}, format="json")
        self.assertEqual(r.json()["state"], "dismissed")


class DocsTests(ApiBase):
    def test_schema_is_openapi_31(self):
        r = APIClient().get("/api/v1/schema")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        self.assertTrue(text.startswith("openapi: 3.1") or '"openapi": "3.1' in text)
        self.assertIn("/api/v1/invoices", text)
        self.assertIn("bearerAuth", text)

    def test_docs_page_has_no_inline_script_or_cdn(self):
        r = APIClient().get("/api/v1/docs")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn("cdn.", html)
        self.assertNotIn("onclick=", html)
        import re
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S):
            self.assertIn("src=", m.group(1))

    def test_llms_txt(self):
        r = APIClient().get("/llms.txt")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/plain"))
        text = r.content.decode()
        for needle in ("Authorization: Bearer", "Idempotency-Key", "payments:confirm", "/api/v1/invoices"):
            self.assertIn(needle, text)

    def test_llms_txt_documents_the_integration_features(self):
        text = APIClient().get("/llms.txt").content.decode()
        for needle in ("amount_paid_minor", "balance_due_minor", "client_letters", "return_url", "return_url_hosts",
                       "method_kinds", "methods_conflict", "no_methods", "name=", "endpoint_limit", "?invoice="):
            self.assertIn(needle, text)
        self.assertNotIn("void this draft", text)

    def test_llms_txt_states_what_the_export_covers(self):
        # The export is visible_invoices: requested by the token's organization (issued by the US
        # issuer on its behalf included) or issued by it. "Your organization only" was not that.
        text = APIClient().get("/llms.txt").content.decode()
        line = next(ln for ln in text.splitlines() if "/api/v1/exports/invoices.csv" in ln)
        following = text.split(line, 1)[1].split("GET  /api/v1/exports/payments.csv", 1)[0]
        scope = line + following
        self.assertNotIn("your organization only", scope)
        for needle in ("requested", "US-resident organization issued on its behalf", "issued itself"):
            self.assertIn(needle, scope, needle)


# ------------------------------------------------------------------ fix round 1
class SchemaEnvelopeTests(ApiBase):
    def test_list_responses_are_paginated_in_the_schema(self):
        schema = json.loads(APIClient().get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json").content)
        for path in ("contacts", "invoices", "payments", "declarations"):
            ref = schema["paths"][f"/api/v1/{path}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            comp = schema["components"]["schemas"][ref["$ref"].rsplit("/", 1)[1]]
            self.assertIn("results", comp["properties"], path)
            self.assertIn("count", comp["properties"], path)
        self.assertNotIn("/api/v1/redoc", schema["paths"])

    def test_redoc_route_is_gone(self):
        self.assertEqual(APIClient().get("/api/v1/redoc").status_code, 404)

    def test_docs_page_has_no_inline_style(self):
        html = APIClient().get("/api/v1/docs").content.decode()
        self.assertNotIn("<style", html)
        self.assertNotIn("style=", html)


class IdempotencyMismatchTests(ApiBase):
    def test_same_key_other_body_is_409_and_creates_no_contact(self):
        c, _, _ = self.client_for()
        a = c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k")
        other = {**BODY, "contact": {"email": "brand-new@example.com"}, "lines": [{**LINE, "unit_price_minor": 5}]}
        b = c.post("/api/v1/invoices", other, format="json", HTTP_IDEMPOTENCY_KEY="k")
        self.assertEqual(b.status_code, 409)
        self.assertEqual(b.json()["error"], "idempotency_mismatch")
        self.assertEqual(Invoice.objects.count(), 1)
        from apps.contacts.services import find_contact
        self.assertIsNone(find_contact(self.org, "brand-new@example.com"))
        # the e-mail is compared case-insensitively; the other contact fields must match
        again = c.post("/api/v1/invoices", {**BODY, "contact": {"email": "A@Example.com", "legal_name": "A"}},
                       format="json", HTTP_IDEMPOTENCY_KEY="k")
        self.assertEqual((again.status_code, again.json()["id"]), (200, a.json()["id"]))

    def test_same_external_reference_other_body_is_409(self):
        c, _, _ = self.client_for()
        c.post("/api/v1/invoices", {**BODY, "external_reference": "r"}, format="json")
        r = c.post("/api/v1/invoices", {**BODY, "currency": "USD", "external_reference": "r"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "idempotency_mismatch"))

    def test_key_longer_than_200_is_400(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k" * 201)
        self.assertEqual(r.status_code, 400)

    def test_payment_idempotency(self):
        from apps.payments.services import record_manual_payment
        from apps.payments.testing import person

        inv = self.invoice(send=True)
        c, _, _ = self.client_for()
        body = {"amount_minor": 4000, "rail": "bank", "note": "n"}
        # A person recorded it under the key; a token's replay returns that payment and writes nothing.
        a = record_manual_payment(inv, amount_minor=4000, rail="bank", note="n", user=person(), idempotency_key="p1")
        b = c.post(f"/api/v1/invoices/{inv.pk}/payments", body, format="json", HTTP_IDEMPOTENCY_KEY="p1")
        self.assertEqual((b.status_code, b.json()["id"]), (200, a.pk))
        self.assertEqual(Payment.objects.count(), 1)
        fresh = c.post(f"/api/v1/invoices/{inv.pk}/payments", body, format="json", HTTP_IDEMPOTENCY_KEY="p2")
        self.assertEqual((fresh.status_code, fresh.json()["error"]), (409, "needs_person"))
        self.assertEqual(Payment.objects.count(), 1)
        diff = c.post(f"/api/v1/invoices/{inv.pk}/payments", {**body, "amount_minor": 1}, format="json",
                      HTTP_IDEMPOTENCY_KEY="p1")
        self.assertEqual((diff.status_code, diff.json()["error"]), (409, "idempotency_mismatch"))
        self.assertEqual(Payment.objects.count(), 1)


class ErrorMappingTests(ApiBase):
    def test_undecryptable_detail_is_json_500_without_ciphertext(self):
        from unittest import mock

        inv = self.invoice()
        c, _, _ = self.client_for()
        with mock.patch("apps.api.views.visible_invoices", side_effect=UndecryptableValue("title")), \
                self.assertLogs("apps.api.views", "ERROR"):
            r = c.get(f"/api/v1/invoices/{inv.pk}")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json(), {"error": "undecryptable",
                                    "detail": "a stored value cannot be decrypted; staff have been alerted"})

    def test_sealed_during_request_is_503_json(self):
        from unittest import mock
        from apps.vault.vault import VaultSealed

        c, _, _ = self.client_for()
        with mock.patch("apps.api.views.visible_invoices", side_effect=VaultSealed()):
            r = c.get("/api/v1/invoices")
        self.assertEqual((r.status_code, r.json()), (503, {"error": "vault_sealed"}))

    def test_one_bad_row_does_not_break_the_list(self):
        good = self.invoice()
        bad = self.invoice()
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("UPDATE invoices_invoice SET title = %s WHERE id = %s", ["gAAAAAnot-a-token", bad.pk])
        c, _, _ = self.client_for()
        with self.assertLogs(level="ERROR"):
            r = c.get("/api/v1/invoices")
        self.assertEqual(r.status_code, 200)
        rows = {row["id"]: row for row in r.json()["results"]}
        self.assertEqual(rows[bad.pk], {"id": bad.pk, "error": "undecryptable"})
        self.assertEqual(rows[good.pk]["status"], "draft")
        self.assertNotIn("gAAAAA", r.content.decode())

    def test_not_found_is_json(self):
        c, _, _ = self.client_for()
        r = c.get("/api/v1/invoices/999999")
        self.assertEqual((r.status_code, r.json()["error"]), (404, "not_found"))
        self.assertEqual(c.get("/api/v1/invoices/" + "9" * 30).status_code, 404)


class ContactConflictTests(ApiBase):
    def post(self, c, resident):
        return c.post("/api/v1/invoices", {**BODY, "currency": "USD",
                                           "contact": {"email": "a@example.com", "us_tax_resident": resident}}, format="json")

    def test_upgrade_to_yes_is_applied_and_switches_issuer(self):
        c, _, _ = self.client_for()
        find_or_create_contact(self.org, "a@example.com", legal_name="A")
        r = self.post(c, "yes")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()["issued_by_rule"])
        from apps.contacts.services import find_contact
        self.assertEqual(find_contact(self.org, "a@example.com").us_tax_resident, "yes")

    def test_downgrade_from_yes_is_409(self):
        c, _, _ = self.client_for()
        contact, _ = find_or_create_contact(self.org, "a@example.com", us_tax_resident="yes")
        for value in ("no", "unknown"):
            r = self.post(c, value)
            self.assertEqual((r.status_code, r.json()["error"]), (409, "contact_conflict"))
        contact.refresh_from_db()
        self.assertEqual(contact.us_tax_resident, "yes")
        self.assertEqual(Invoice.objects.count(), 0)


class AuditAndBoundsTests(ApiBase):
    def test_void_is_audited(self):
        inv = self.invoice(send=True)
        c, token, _ = self.client_for()
        c.post(f"/api/v1/invoices/{inv.pk}/void", {"reason": "dup"}, format="json")
        e = AuditEntry.objects.get(action="invoice.void")
        self.assertEqual((e.actor_username, e.object_pk), ("api:t", str(inv.pk)))

    def test_bounds_never_500(self):
        c, _, _ = self.client_for()
        bad = [
            {**BODY, "due_days": 3651},
            {**BODY, "due_days": -1},
            {**BODY, "lines": [{**LINE, "unit_price_minor": 10**12 + 1}]},
            {**BODY, "lines": [{**LINE, "discount_amount_minor": 10**12 + 1}]},
            {**BODY, "lines": [{**LINE, "quantity": "12345678901"}]},
            {**BODY, "lines": [{**LINE, "quantity": "1.005"}]},
            {**BODY, "lines": [{**LINE, "quantity": "NaN"}]},
            {**BODY, "contact_id": 10**30, "contact": None},
        ]
        for body in bad:
            body = {k: v for k, v in body.items() if v is not None}
            self.assertEqual(c.post("/api/v1/invoices", body, format="json").status_code, 400, body)
        inv = self.invoice(send=True)
        r = c.post(f"/api/v1/invoices/{inv.pk}/payments", {"amount_minor": 10**12 + 1, "rail": "bank"}, format="json")
        self.assertEqual(r.status_code, 400)
        ok = c.post("/api/v1/invoices", {**BODY, "due_days": 3650,  # the largest quantity, total under the cap
                                         "lines": [{**LINE, "quantity": "9999999999.99", "unit_price_minor": 100}]},
                    format="json")
        self.assertEqual(ok.status_code, 201, ok.content)


class OfferedMethodTests(ApiBase):
    def test_methods_validated_against_issuer(self):
        c, _, _ = self.client_for()
        mine = BankAccount.objects.create(org=self.org, currency="EUR", active=True)
        off = BankAccount.objects.create(org=self.org, currency="EUR", active=False)
        theirs = BankAccount.objects.create(org=self.other, currency="EUR")
        fiat = FiatLink.objects.create(org=self.org, kind="wise", currencies=["EUR"])
        ok = c.post("/api/v1/invoices", {**BODY, "offered_methods": [f"bank:{mine.pk}", f"fiat:{fiat.pk}"]}, format="json")
        self.assertEqual(ok.status_code, 201)
        for m in (f"bank:{off.pk}", f"bank:{theirs.pk}", "bank:999999", "bank:\u00b2", "bank:1\n", "cash:1", "bank:"):
            r = c.post("/api/v1/invoices", {**BODY, "offered_methods": [m]}, format="json")
            self.assertEqual(r.status_code, 400, m)
            self.assertEqual(r.json()["error"], "invalid_offered_method", m)

    def test_rail_methods_validated_against_issuer(self):
        from apps.rails.models import RailAccount

        c, _, _ = self.client_for()
        evm = dict(kind="evm", label="EVM", address="0x" + "ab" * 20, networks=["arbitrum"], assets=["USDC"])
        mine = RailAccount.objects.create(org=self.org, **evm)
        off = RailAccount.objects.create(org=self.org, active=False, **evm)
        theirs = RailAccount.objects.create(org=self.other, **evm)
        ok = c.post("/api/v1/invoices", {**BODY, "currency": "CZK", "offered_methods": [f"rail:{mine.pk}"]},
                    format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["offered_methods"], [f"rail:{mine.pk}"])
        for m in (f"rail:{off.pk}", f"rail:{theirs.pk}", "rail:999999", "rail:", "rail:1x", "rail:" + "1" * 13):
            r = c.post("/api/v1/invoices", {**BODY, "offered_methods": [m]}, format="json")
            self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid_offered_method"), m)

    def test_methods_checked_in_the_us_issuer_after_the_rule(self):
        c, _, _ = self.client_for()
        eu = BankAccount.objects.create(org=self.org, currency="USD")
        us = BankAccount.objects.create(org=self.us, currency="USD")
        body = {**BODY, "currency": "USD", "contact": {**BODY["contact"], "us_tax_resident": "yes"}}
        self.assertEqual(c.post("/api/v1/invoices", {**body, "offered_methods": [f"bank:{eu.pk}"]}, format="json").status_code, 400)
        self.assertEqual(c.post("/api/v1/invoices", {**body, "offered_methods": [f"bank:{us.pk}"]}, format="json").status_code, 201)


class TokenAdminTests(ApiBase):
    def test_revoke_command(self):
        c, token, _ = self.client_for(name="gone")
        call_command("revoke_api_token", "gone", stdout=StringIO())
        self.assertEqual(c.get("/api/v1/invoices").status_code, 401)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("revoke_api_token", "gone", stdout=StringIO())

    def test_token_without_service_user_is_refused(self):
        c, token, _ = self.client_for()
        ApiToken.objects.filter(pk=token.pk).update(service_user=None)
        self.assertEqual(c.get("/api/v1/invoices").status_code, 401)

    def test_service_user_is_protected_inactive_non_staff(self):
        from django.db.models import ProtectedError

        _, token, _ = self.client_for()
        u = token.service_user
        self.assertFalse(u.has_usable_password())
        self.assertFalse(u.is_active)
        self.assertFalse(u.is_staff or u.is_superuser)
        with self.assertRaises(ProtectedError):
            u.delete()

    def test_admin_lists_tokens_without_hash_editing(self):
        from django.contrib.admin.sites import site
        from apps.api.admin import ApiTokenAdmin

        _, token, _ = self.client_for()
        ma = ApiTokenAdmin(ApiToken, site)
        self.assertIn("token_hash", ma.readonly_fields)
        self.assertIn("revoked_at", ma.list_display)


class CrossOrgWriteTests(ApiBase):
    def test_every_write_on_another_orgs_objects_is_404(self):
        inv = self.invoice(org=self.other, send=True)
        draft = self.invoice(org=self.other)
        pay = Payment.objects.create(invoice=inv, rail="bank", amount_minor=1, received_amount=1, state="detected")
        from apps.payments.services import declare_payment
        decl = declare_payment(inv, note="x")
        c, _, _ = self.client_for()
        calls = [
            ("patch", f"/api/v1/contacts/{inv.contact_id}", {"display_name": "x"}),
            ("post", f"/api/v1/invoices/{draft.pk}/send", {}),
            ("post", f"/api/v1/invoices/{inv.pk}/reissue-links", {}),
            ("post", f"/api/v1/invoices/{inv.pk}/payments", {"amount_minor": 1, "rail": "bank"}),
            ("post", f"/api/v1/payments/{pay.pk}/confirm", {}),
            ("post", f"/api/v1/payments/{pay.pk}/reject", {}),
            ("post", f"/api/v1/payments/{pay.pk}/reverse", {"reason": "x"}),
            ("post", f"/api/v1/declarations/{decl.pk}/dismiss", {}),
        ]
        for method, url, body in calls:
            self.assertEqual(getattr(c, method)(url, body, format="json").status_code, 404, url)
        pay.refresh_from_db()
        self.assertEqual(pay.state, "detected")


class FingerprintContactTests(ApiBase):
    """C2: a replay whose inline contact changed is a different request, never the old invoice."""

    def test_replay_that_now_declares_us_residency_is_409(self):
        c, _, _ = self.client_for()
        first = c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k-us")
        self.assertEqual(first.status_code, 201)
        body = {**BODY, "contact": {**BODY["contact"], "us_tax_resident": "yes"}}
        r = c.post("/api/v1/invoices", body, format="json", HTTP_IDEMPOTENCY_KEY="k-us")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "idempotency_mismatch")
        self.assertEqual(Invoice.objects.count(), 1)

    def test_every_inline_contact_field_counts(self):
        c, _, _ = self.client_for()
        self.assertEqual(c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k").status_code, 201)
        changes = {"legal_name": "B", "display_name": "D", "tax_ids": "VAT 1", "address": {"city": "Bratislava"},
                   "phone": "+1", "notes": "n", "preferred_language": "sk", "email": "other@example.com"}
        for field, value in changes.items():
            body = {**BODY, "contact": {**BODY["contact"], field: value}}
            r = c.post("/api/v1/invoices", body, format="json", HTTP_IDEMPOTENCY_KEY="k")
            self.assertEqual(r.status_code, 409, field)
        self.assertEqual(c.post("/api/v1/invoices", BODY, format="json", HTTP_IDEMPOTENCY_KEY="k").status_code, 200)


class PaymentMethodsApiTests(ApiBase):
    """I3: defaults, currency checks and the issuer-aware listing."""

    def setUp(self):
        super().setUp()
        BankAccount.objects.filter(org=self.org).delete()  # only the accounts below
        self.eur = BankAccount.objects.create(org=self.org, currency="EUR", label="Main EUR")
        self.usd = BankAccount.objects.create(org=self.org, currency="USD", label="Main USD")
        self.wise = FiatLink.objects.create(org=self.org, kind="wise", label="Wise", currencies=["EUR"])
        self.us_usd = BankAccount.objects.create(org=self.us, currency="USD", label="US USD")

    def test_create_without_methods_stores_the_defaults(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", BODY, format="json")
        self.assertEqual(r.json()["offered_methods"], [f"bank:{self.eur.pk}", f"fiat:{self.wise.pk}"])

    def test_bank_account_in_another_currency_is_refused(self):
        c, _, _ = self.client_for()
        r = c.post("/api/v1/invoices", {**BODY, "offered_methods": [f"bank:{self.usd.pk}"]}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid_offered_method"))

    def test_sending_an_invoice_that_offers_nothing_is_409(self):
        c, _, _ = self.client_for()
        iid = c.post("/api/v1/invoices", {**BODY, "offered_methods": []}, format="json").json()["id"]
        r = c.post(f"/api/v1/invoices/{iid}/send", {"email_client": False}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "invoice_error"))
        self.assertIn("no payment method offered", r.json()["detail"])

    def test_listing_names_the_issuer_and_its_methods(self):
        c, _, _ = self.client_for(scopes=["read"])
        r = c.get("/api/v1/payment-methods?currency=eur")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual((j["currency"], j["issuer_org"], j["issued_by_rule"]), ("EUR", "eu", False))
        self.assertEqual([m["id"] for m in j["methods"]], [f"bank:{self.eur.pk}", f"fiat:{self.wise.pk}"])
        self.assertEqual(j["methods"][0], {"id": f"bank:{self.eur.pk}", "kind": "bank", "label": "Main EUR",
                                           "currencies": ["EUR"]})
        find_or_create_contact(self.org, "us@example.com", us_tax_resident="yes")
        j = c.get("/api/v1/payment-methods?currency=USD&contact_email=US@example.com").json()
        self.assertEqual((j["issuer_org"], j["issued_by_rule"]), ("us", True))
        self.assertEqual([m["id"] for m in j["methods"]], [f"bank:{self.us_usd.pk}"])
        unknown = c.get("/api/v1/payment-methods?currency=USD&contact_email=nobody@example.com").json()
        self.assertEqual(unknown["issuer_org"], "eu")

    def test_listing_includes_active_rails(self):
        from apps.rails.models import RailAccount

        evm = RailAccount.objects.create(org=self.org, kind="evm", label="Stablecoins", address="0x" + "ab" * 20,
                                         networks=["arbitrum"], assets=["USDC"])
        RailAccount.objects.create(org=self.org, kind="evm", label="old", address="0x" + "ab" * 20,
                                   networks=["arbitrum"], assets=["USDC"], active=False)
        c, _, _ = self.client_for(scopes=["read"])
        j = c.get("/api/v1/payment-methods?currency=czk").json()
        self.assertEqual(j["methods"], [{"id": f"rail:{evm.pk}", "kind": "rail", "label": "Stablecoins",
                                         "currencies": ["CZK"],
                                         "options": [{"network": "arbitrum", "asset": "USDC"}]}])
        ids = [m["id"] for m in c.get("/api/v1/payment-methods?currency=EUR").json()["methods"]]
        self.assertEqual(ids, [f"bank:{self.eur.pk}", f"fiat:{self.wise.pk}", f"rail:{evm.pk}"])

    def test_listing_validates_the_currency(self):
        c, _, _ = self.client_for(scopes=["read"])
        for query in ("", "?currency=", "?currency=ZZZ"):
            r = c.get(f"/api/v1/payment-methods{query}")
            self.assertEqual(r.status_code, 400, query)
        self.assertEqual(APIClient().get("/api/v1/payment-methods?currency=EUR").status_code, 401)


class ApiAuditTests(ApiBase):
    """I5: changes made through the API are audited as the token's service user."""

    def test_create_patch_send_reissue_are_audited(self):
        c, token, _ = self.client_for()
        j = c.post("/api/v1/invoices", BODY, format="json").json()
        actions = lambda: list(AuditEntry.objects.filter(actor=token.service_user).values_list("action", flat=True))
        self.assertEqual(sorted(actions()), ["contact.create", "invoice.create"])
        r = c.patch(f"/api/v1/contacts/{j['contact_id']}", {"us_tax_resident": "no"}, format="json")
        self.assertEqual(r.status_code, 200)
        e = AuditEntry.objects.get(action="contact.update")
        self.assertEqual((e.actor, e.detail["us_tax_resident"]), (token.service_user, ["unknown", "no"]))
        c.post(f"/api/v1/invoices/{j['id']}/send", {"email_client": False}, format="json")
        c.post(f"/api/v1/invoices/{j['id']}/reissue-links", {}, format="json")
        self.assertIn("invoice.send", actions())
        self.assertIn("invoice.reissue_links", actions())

    def test_inline_upgrade_to_us_resident_is_audited(self):
        c, token, _ = self.client_for()
        contact, _ = find_or_create_contact(self.org, "a@example.com")
        c.post("/api/v1/invoices", {**BODY, "currency": "USD", "contact": {**BODY["contact"], "us_tax_resident": "yes"}},
               format="json")
        e = AuditEntry.objects.get(action="contact.update", object_pk=str(contact.pk))
        self.assertEqual((e.actor, e.detail["us_tax_resident"]), (token.service_user, ["unknown", "yes"]))


class UnmatchedPathTests(ApiBase):
    def test_unknown_api_paths_are_json_404(self):
        c, _, _ = self.client_for()
        for path in ("/api/v1/nope", "/api/v1/invoices/", "/api/v1/invoices/abc", "/api/v1/invoices/1/frobnicate",
                     "/api/v1/", "/api/v1/" + "9" * 30):
            for client in (c, APIClient()):
                r = client.get(path)
                self.assertEqual(r.status_code, 404, path)
                self.assertEqual(r["Content-Type"], "application/json", path)
                self.assertEqual(r.json()["error"], "not_found", path)
        self.assertEqual(c.post("/api/v1/nope", {}, format="json").json()["error"], "not_found")


class InvoiceTotalCapTests(ApiBase):
    def test_total_above_ten_to_the_twelve_is_refused(self):
        c, _, _ = self.client_for()
        line = {"name": "Big", "quantity": "2", "unit_price_minor": 10**12}
        r = c.post("/api/v1/invoices", {**BODY, "lines": [line]}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invoice_error"))
        self.assertIn("10^12", r.json()["detail"])
        self.assertEqual(Invoice.objects.count(), 0)
        ok = {"name": "Big", "quantity": "1", "unit_price_minor": 10**12}
        self.assertEqual(c.post("/api/v1/invoices", {**BODY, "lines": [ok]}, format="json").status_code, 201)
