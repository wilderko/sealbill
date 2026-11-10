from apps.api.tests import ApiBase, LINE
from apps.invoices.models import Invoice
from apps.invoices.services import send_invoice
from apps.orgs.models import default_org_settings


class ReturnUrlApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.org.settings = {**default_org_settings(), "return_url_hosts": ["liberation.services", "Shop.Example"]}
        self.org.save()
        self.client, _, _ = self.client_for()

    def body(self, **extra):
        return {"contact": {"email": "x@example.com"}, "currency": "EUR", "lines": [dict(LINE)],
                "external_reference": "ref-1", **extra}

    def post(self, **extra):
        return self.client.post("/api/v1/invoices", self.body(**extra), format="json")

    def test_allowed_host_is_stored_and_returned(self):
        r = self.post(return_url="https://liberation.services/orders/5/")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["return_url"], "https://liberation.services/orders/5/")
        self.assertEqual(Invoice.objects.get().return_url, "https://liberation.services/orders/5/")

    def test_host_compares_in_lowercase_and_port_443_is_fine(self):
        self.assertEqual(self.post(return_url="https://LIBERATION.services:443/x").status_code, 201)

    def test_omitted_is_empty(self):
        r = self.post()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["return_url"], "")

    def refused(self, url):
        r = self.post(return_url=url)
        self.assertEqual(r.status_code, 400, (url, r.content))
        self.assertEqual(r.json()["error"], "return_url_not_allowed", r.json())
        self.assertEqual(Invoice.objects.count(), 0)

    def test_http_is_refused(self):
        self.refused("http://liberation.services/x")

    def test_another_host_is_refused(self):
        self.refused("https://evil.example/x")

    def test_lookalike_hosts_are_refused(self):
        self.refused("https://liberation.services.evil.example/")
        self.refused("https://sub.liberation.services/")
        self.refused("https://evil.example/@liberation.services")

    def test_parser_tricks_are_refused(self):
        for url in ("https://liberation.services@evil.example/", "https://evil.example\\@liberation.services/",
                    "https://liberation.services\\.evil.example/", "https://liberation.services:8443/",
                    "https://liberation.services /x", "https://liberation.services/\nx",
                    "https:///x", "//liberation.services/x", "javascript:alert(1)",
                    "https://user:pw@liberation.services/", "https://liberation.services:abc/"):
            with self.subTest(url=url):
                self.refused(url)

    def test_too_long_is_refused(self):
        self.refused("https://liberation.services/" + "a" * 500)

    def test_hosts_are_the_requesters_not_the_issuers(self):
        contact = {"email": "us@example.com", "us_tax_resident": "yes"}
        r = self.client.post("/api/v1/invoices", self.body(contact=contact, return_url="https://shop.example/x"),
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Invoice.objects.get().org, self.us)

    def test_org_without_hosts_refuses_everything(self):
        self.org.settings = {"underpay_allowance_bps": 50}
        self.org.save()
        self.refused("https://liberation.services/x")

    def test_replay_with_another_return_url_is_409(self):
        self.assertEqual(self.post(return_url="https://liberation.services/a").status_code, 201)
        r = self.post(return_url="https://liberation.services/b")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.post(return_url="https://liberation.services/a").status_code, 200)
        self.assertEqual(self.post().status_code, 409)

    def test_sent_invoice_return_url_is_frozen(self):
        inv = Invoice.objects.get(pk=self.post(return_url="https://liberation.services/a").json()["id"])
        send_invoice(inv, email_client=False)
        inv.refresh_from_db()
        inv.return_url = "https://liberation.services/b"
        from apps.invoices.errors import InvoiceError
        with self.assertRaises(InvoiceError):
            inv.save()
