"""The agent API for the crypto rails: quotes, the review-queue actions and the payment fields."""
import json
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from apps.api.tests import ApiBase
from apps.audit.models import AuditEntry
from apps.payments.models import Payment
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails.models import RailAccount

ADDR = "0x" + "ab" * 20
TX = "0x" + "1f" * 32


class CryptoApiBase(ApiBase):
    def setUp(self):
        super().setUp()
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=ADDR,
                                               networks=["arbitrum"], assets=["USDC"])
        self.inv = self.invoice(send=True)
        self.client_, self.token, _ = self.client_for()

    def post(self, path, body=None, key=None, client=None):
        extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return (client or self.client_).post(path, json.dumps(body or {}), content_type="application/json", **extra)

    def quote_body(self, **kw):
        return {"method": self.rail.method_key, "network": "arbitrum", "asset": "USDC", **kw}

    def review(self, **kw):
        fields = dict(rail="evm", network="arbitrum", asset="USDC", amount_minor=0, received_amount="108.27",
                      tx_hash=TX, state="needs_review", review_reason="no_matching_invoice",
                      amount_base_units="108270000", to_address=ADDR, rail_account=self.rail, final=True)
        fields.update(kw)
        return Payment.objects.create(**fields)


FX = {"EUR": 0.9237}


class QuoteEndpointTests(CryptoApiBase):
    def fake_rates(self):
        from decimal import Decimal
        from apps.rails import fx
        pair = fx.RatePair(rate=Decimal("0.9237"), second=Decimal("0.9237"), source="test")
        return mock.patch("apps.quotes.services.fx.price_in", return_value=pair)

    def test_quote_endpoint_returns_amount_and_uri(self):
        with self.fake_rates():
            r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body())
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        quote = Quote.objects.get()
        self.assertEqual(d["id"], quote.pk)
        self.assertEqual((d["rail"], d["network"], d["asset"]), (self.rail.method_key, "arbitrum", "USDC"))
        self.assertEqual(d["amount_base_units"], quote.amount_base_units)
        self.assertEqual(Decimal(d["amount"]) * 10**6, Decimal(d["amount_base_units"]))
        self.assertEqual(d["address"], ADDR)
        self.assertTrue(d["payment_uri"].startswith("ethereum:"))
        for k in ("bolt11", "checkout_url", "invoice_amount_minor", "rate", "rate_source", "expires_at", "state"):
            self.assertIn(k, d)
        self.assertEqual((d["state"], d["invoice_amount_minor"]), ("open", 10000))
        # the same call again hands the open quote back
        with self.fake_rates():
            again = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body())
        self.assertEqual(again.json()["id"], d["id"])

    def test_quote_read_endpoints(self):
        with self.fake_rates():
            q = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body()).json()
        r = self.client_.get(f"/api/v1/invoices/{self.inv.pk}/quotes")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["id"] for x in r.json()["results"]], [q["id"]])
        self.assertEqual(self.client_.get(f"/api/v1/quotes/{q['id']}").json()["id"], q["id"])
        # another organization's token does not see it
        other, _, _ = self.client_for(self.other, name="o")
        self.assertEqual(other.get(f"/api/v1/quotes/{q['id']}").status_code, 404)
        self.assertEqual(other.get(f"/api/v1/invoices/{self.inv.pk}/quotes").status_code, 404)
        # a read-only token cannot create one
        ro, _, _ = self.client_for(scopes=("read",), name="ro")
        self.assertEqual(self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body(), client=ro).status_code, 403)
        self.assertEqual(ro.get(f"/api/v1/quotes/{q['id']}").status_code, 200)

    def test_money_may_be_coming_for_an_open_chain_quote_and_an_unread_gateway_quote(self):
        import datetime
        from django.utils import timezone

        with self.fake_rates():
            q = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body()).json()
        self.assertIs(q["money_may_be_coming"], True)  # it holds its amount until released (audit LS-2)
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["XMR"],
                                            networks=["monero"])
        now = timezone.now()
        gate = Quote.objects.create(
            invoice=self.inv, rail_account=btcpay, network="monero", asset="XMR", address="",
            amount_base_units="1", min_accept_base_units="1", ceiling_base_units="1", invoice_amount_minor=10000,
            issued_at=now - datetime.timedelta(minutes=31), expires_at=now - datetime.timedelta(minutes=1),
            state="expired", read_at=None)
        got = self.client_.get(f"/api/v1/quotes/{gate.pk}").json()
        self.assertIs(got["money_may_be_coming"], True)
        Quote.objects.filter(pk=gate.pk).update(read_at=now)  # a good read after the expiry
        self.assertIs(self.client_.get(f"/api/v1/quotes/{gate.pk}").json()["money_may_be_coming"], False)
        listed = self.client_.get(f"/api/v1/invoices/{self.inv.pk}/quotes").json()["results"]
        self.assertTrue(all("money_may_be_coming" in x for x in listed))

    def test_quote_endpoint_waits_at_most_five_seconds_on_the_exchange_rates(self):
        import requests

        from apps.rails import http

        budgets, seen = [], []

        def timing_out(*args, timeout=None, **kw):
            budgets.append(http._deadline.get())
            seen.append(timeout)
            raise requests.Timeout()

        with override_settings(RAILS_HTTP_ENABLED=True), mock.patch("apps.rails.http.requests.get",
                                                                    side_effect=timing_out):
            with self.assertLogs("apps.quotes.services", "WARNING"), self.assertLogs("apps.rails.http", "WARNING"):
                r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body())
        self.assertEqual((r.status_code, r.json()["error"]), (503, "rates_unavailable"))
        self.assertEqual((seen, budgets), ([5], [5.0]))
        self.assertIsNone(http._deadline.get())  # the budget ends with the request

    def test_quote_endpoint_rates_unavailable_503(self):
        with mock.patch("apps.api.views.create_quote", side_effect=QuoteError("rates_unavailable")):
            r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body())
        self.assertEqual((r.status_code, r.json()["error"]), (503, "rates_unavailable"))

    def test_quote_refusals_are_409_with_the_code(self):
        r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body(asset="ETH"))
        self.assertEqual((r.status_code, r.json()["error"]), (409, "asset_not_enabled"))
        r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", self.quote_body(method="rail:999"))
        self.assertEqual((r.status_code, r.json()["error"]), (409, "method_not_offered"))
        r = self.post(f"/api/v1/invoices/{self.inv.pk}/quotes", {"method": "bank:1"})
        self.assertEqual(r.status_code, 400)


class AssignEndpointTests(CryptoApiBase):
    def test_assign_requires_payments_confirm_scope(self):
        p = self.review(invoice=None)
        weak, _, _ = self.client_for(scopes=("read", "invoices:write"), name="weak")
        body = {"invoice_id": self.inv.pk, "amount_minor": 10000, "note": "n"}
        self.assertEqual(self.post(f"/api/v1/payments/{p.pk}/assign", body, key="k", client=weak).status_code, 403)
        self.assertEqual(self.post(f"/api/v1/payments/{p.pk}/acknowledge-refund", {"note": "n"}, client=weak).status_code, 403)
        # a payment with no invoice is visible to an admin token only
        self.assertEqual(self.post(f"/api/v1/payments/{p.pk}/assign", body, key="k").status_code, 404)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        self.assertEqual(self.post(f"/api/v1/payments/{p.pk}/assign", body, client=admin).status_code, 400)  # no key
        r = self.post(f"/api/v1/payments/{p.pk}/assign", body, key="k1", client=admin)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["state"], r.json()["invoice_id"]), ("confirmed", self.inv.pk))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, "paid")
        entry = AuditEntry.objects.get(action="payment.assign")
        self.assertEqual(entry.actor.username, "api:adm")
        # a replay with the same key answers the same payment; another body is a conflict
        again = self.post(f"/api/v1/payments/{p.pk}/assign", body, key="k1", client=admin)
        self.assertEqual((again.status_code, again.json()["id"]), (200, p.pk))
        bad = self.post(f"/api/v1/payments/{p.pk}/assign", {**body, "amount_minor": 5}, key="k1", client=admin)
        self.assertEqual((bad.status_code, bad.json()["error"]), (409, "idempotency_mismatch"))

    def test_assign_never_reaches_another_organization(self):
        theirs = self.invoice(org=self.other, send=True)
        p = self.review(invoice=None)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        r = self.post(f"/api/v1/payments/{p.pk}/assign",
                      {"invoice_id": theirs.pk, "amount_minor": 10000, "note": "n"}, key="k", client=admin)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")

    def test_assign_business_refusal_is_409(self):
        p = self.review(invoice=None, final=False)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        r = self.post(f"/api/v1/payments/{p.pk}/assign",
                      {"invoice_id": self.inv.pk, "amount_minor": 10000, "note": "n"}, key="k", client=admin)
        self.assertEqual((r.status_code, r.json()["error"]), (409, "invoice_error"))

    def test_acknowledge_refund_endpoint(self):
        p = self.review(invoice=self.inv, refund_due_base_units="5000")
        r = self.post(f"/api/v1/payments/{p.pk}/acknowledge-refund", {"note": "sent back"})
        self.assertEqual(r.status_code, 200, r.content)
        p.refresh_from_db()
        self.assertIsNotNone(p.refund_acknowledged_at)
        self.assertEqual(p.refund_acknowledged_by.username, "api:t")
        again = self.post(f"/api/v1/payments/{p.pk}/acknowledge-refund", {"note": "again"})
        self.assertEqual((again.status_code, again.json()["error"]), (409, "invoice_error"))
        none = self.review(invoice=self.inv, tx_hash="0x" + "2f" * 32)
        self.assertEqual(self.post(f"/api/v1/payments/{none.pk}/acknowledge-refund", {"note": "n"}).status_code, 409)


class PaymentJsonTests(CryptoApiBase):
    def test_payment_json_has_chain_fields(self):
        q = Quote.objects.create(invoice=self.inv, rail_account=self.rail, network="arbitrum", asset="USDC",
                                 address=ADDR, amount_base_units="108270000", min_accept_base_units="1",
                                 ceiling_base_units="999999999", invoice_amount_minor=10000,
                                 issued_at=self.inv.created_at, expires_at=self.inv.created_at)
        self.review(invoice=self.inv, quote=q, log_index=3, refund_due_base_units="12",
                    needs_staff_confirmation=True)
        d = self.client_.get("/api/v1/payments").json()["results"][0]
        self.assertEqual((d["rail"], d["network"], d["asset"], d["amount_base_units"]),
                         ("evm", "arbitrum", "USDC", "108270000"))
        self.assertEqual((d["tx_hash"], d["log_index"], d["final"]), (TX, 3, True))
        self.assertEqual((d["review_reason"], d["refund_due_base_units"], d["needs_staff_confirmation"], d["quote"]),
                         ("no_matching_invoice", "12", True, q.pk))

    def test_an_invoice_id_beyond_the_bigint_range_is_an_empty_result(self):
        self.review(invoice=self.inv)
        for value in ("99999999999999999999", "-99999999999999999999", str(2**63)):
            r = self.client_.get("/api/v1/payments", {"invoice": value})
            self.assertEqual(r.status_code, 200, (value, r.content))
            self.assertEqual(r.json()["results"], [])
        self.assertEqual(len(self.client_.get("/api/v1/payments", {"invoice": self.inv.pk}).json()["results"]), 1)

    def test_payment_methods_list_networks_and_assets(self):
        d = self.client_.get("/api/v1/payment-methods?currency=EUR").json()
        rail = next(m for m in d["methods"] if m["kind"] == "rail")
        self.assertEqual(rail["id"], self.rail.method_key)
        self.assertEqual(rail["options"], [{"network": "arbitrum", "asset": "USDC"}])


class DocsTests(CryptoApiBase):
    def test_schema_lists_quote_endpoints(self):
        schema = json.loads(self.client_.get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json").content)
        paths = schema["paths"]
        self.assertIn("post", paths["/api/v1/invoices/{id}/quotes"])
        self.assertIn("get", paths["/api/v1/invoices/{id}/quotes"])
        self.assertIn("get", paths["/api/v1/quotes/{id}"])
        self.assertIn("post", paths["/api/v1/payments/{id}/assign"])
        self.assertIn("post", paths["/api/v1/payments/{id}/acknowledge-refund"])

    def test_llms_txt_mentions_quotes_and_assign(self):
        text = self.client_.get("/llms.txt").content.decode()
        for needle in ("/quotes", "/assign", "acknowledge-refund", "needs_review", "rates_unavailable"):
            self.assertIn(needle, text)


class AdminScopeTests(CryptoApiBase):
    def test_admin_token_never_sees_another_orgs_unattributed_payment(self):
        theirs_rail = RailAccount.objects.create(org=self.other, kind="evm", label="Theirs", address="0x" + "cd" * 20,
                                                 networks=["arbitrum"], assets=["USDC"])
        theirs = self.review(invoice=None, rail_account=theirs_rail, to_address=theirs_rail.address,
                             tx_hash="0x" + "3f" * 32)
        mine = self.review(invoice=None)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        ids = [x["id"] for x in admin.get("/api/v1/payments").json()["results"]]
        self.assertIn(mine.pk, ids)
        self.assertNotIn(theirs.pk, ids)
        body = {"invoice_id": self.inv.pk, "amount_minor": 10000, "note": "n"}
        for path, payload, key in (("assign", body, "k"), ("confirm", {"note": "n"}, None),
                                   ("reject", {"note": "n"}, None), ("reverse", {"reason": "n"}, None),
                                   ("acknowledge-refund", {"note": "n"}, None)):
            r = self.post(f"/api/v1/payments/{theirs.pk}/{path}", payload, key=key, client=admin)
            self.assertEqual(r.status_code, 404, (path, r.content))
        self.assertEqual(Payment.objects.get(pk=theirs.pk).state, "needs_review")
        # a wallet shared by both organizations is visible to both
        RailAccount.objects.create(org=self.other, kind="evm", label="Shared", address=ADDR,
                                   networks=["arbitrum"], assets=["USDC"])
        other_admin, _, _ = self.client_for(self.other, scopes=("admin",), name="oadm")
        self.assertIn(mine.pk, [x["id"] for x in other_admin.get("/api/v1/payments").json()["results"]])

    def test_assign_idempotency_key_is_per_organization(self):
        a = self.review(invoice=None)
        b = self.review(invoice=None, tx_hash="0x" + "4f" * 32)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        body = {"invoice_id": self.inv.pk, "amount_minor": 10000, "note": "n"}
        self.assertEqual(self.post(f"/api/v1/payments/{a.pk}/assign", body, key="same", client=admin).status_code, 200)
        r = self.post(f"/api/v1/payments/{b.pk}/assign", {**body, "amount_minor": 5}, key="same", client=admin)
        self.assertEqual((r.status_code, r.json()["error"]), (409, "idempotency_mismatch"))
        self.assertEqual(Payment.objects.get(pk=b.pk).state, "needs_review")
        # a replay with another note is a different request
        r = self.post(f"/api/v1/payments/{a.pk}/assign", {**body, "note": "other"}, key="same", client=admin)
        self.assertEqual((r.status_code, r.json()["error"]), (409, "idempotency_mismatch"))
        self.assertEqual(self.post(f"/api/v1/payments/{a.pk}/assign", body, key="same", client=admin).status_code, 200)


class LlmsTxtReviewFixesTests(CryptoApiBase):
    def test_llms_txt_states_the_reviewed_facts(self):
        text = self.client_.get("/llms.txt").content.decode()
        for needle in ("while a payment is detected", "return_url, method_kinds", "vault_unlock_failures",
                       "payment_invalid_after_confirmation", "floored at 0", "refund_due_base_units",
                       '"final": true'):
            self.assertIn(needle, text)
