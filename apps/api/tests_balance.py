from apps.api.tests import ApiBase
from apps.invoices import money
from apps.payments.models import Payment


class BalanceTests(ApiBase):
    def pay(self, inv, minor, state="confirmed", **kw):
        return Payment.objects.create(invoice=inv, rail="bank", amount_minor=minor, received_amount=minor / 100,
                                      state=state, **kw)

    def get(self, inv):
        c, _, _ = self.client_for(scopes=("read",))
        return c.get(f"/api/v1/invoices/{inv.pk}").json()

    def test_unpaid_invoice_owes_the_total(self):
        inv = self.invoice(send=True)
        body = self.get(inv)
        self.assertEqual(body["amount_paid_minor"], 0)
        self.assertEqual(body["balance_due_minor"], money.stored_total(inv))
        self.assertGreater(body["balance_due_minor"], 0)

    def test_partial_payment(self):
        inv = self.invoice(send=True)
        total = money.stored_total(inv)
        self.pay(inv, 100)
        self.pay(inv, 5000, state="detected")  # not confirmed: does not count
        body = self.get(inv)
        self.assertEqual(body["amount_paid_minor"], 100)
        self.assertEqual(body["balance_due_minor"], total - 100)

    def test_overpaid_balance_is_zero(self):
        inv = self.invoice(send=True)
        total = money.stored_total(inv)
        self.pay(inv, total + 500)
        body = self.get(inv)
        self.assertEqual(body["amount_paid_minor"], total + 500)
        self.assertEqual(body["balance_due_minor"], 0)

    def test_list_carries_the_fields_too(self):
        inv = self.invoice(send=True)
        c, _, _ = self.client_for(scopes=("read",))
        row = c.get("/api/v1/invoices").json()["results"][0]
        self.assertEqual(row["balance_due_minor"], money.stored_total(inv))


class PaymentsByInvoiceTests(ApiBase):
    def pay(self, inv, minor=100, state="confirmed"):
        return Payment.objects.create(invoice=inv, rail="bank", amount_minor=minor, received_amount=1, state=state)

    def test_filter_returns_only_that_invoice(self):
        a, b = self.invoice(send=True), self.invoice(send=True)
        pa, _ = self.pay(a), self.pay(b)
        c, _, _ = self.client_for(scopes=("read",))
        body = c.get("/api/v1/payments", {"invoice": a.pk}).json()
        self.assertEqual([p["id"] for p in body["results"]], [pa.pk])
        self.assertEqual(c.get("/api/v1/payments").json()["count"], 2)

    def test_payment_exposes_final_and_state(self):
        a = self.invoice(send=True)
        self.pay(a)
        c, _, _ = self.client_for(scopes=("read",))
        row = c.get("/api/v1/payments", {"invoice": a.pk}).json()["results"][0]
        self.assertIn("final", row)
        self.assertEqual(row["state"], "confirmed")

    def test_non_integer_is_400(self):
        c, _, _ = self.client_for(scopes=("read",))
        self.assertEqual(c.get("/api/v1/payments", {"invoice": "abc"}).status_code, 400)

    def test_other_organizations_invoice_is_empty(self):
        theirs = self.invoice(org=self.other, send=True)
        self.pay(theirs)
        c, _, _ = self.client_for(scopes=("read",))
        r = c.get("/api/v1/payments", {"invoice": theirs.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"], [])
        self.assertEqual(r.json()["count"], 0)

    def test_us_routed_invoice_is_visible_to_the_requester(self):
        inv = self.invoice(send=True)
        type(inv).objects.filter(pk=inv.pk).update(org=self.us, requested_by_org=self.org)
        p = self.pay(inv)
        c, _, _ = self.client_for(scopes=("read",))
        body = c.get("/api/v1/payments", {"invoice": inv.pk}).json()
        self.assertEqual([x["id"] for x in body["results"]], [p.pk])
