from apps.api.tests import ApiBase


class MeTests(ApiBase):
    def test_me_endpoint_reports_scopes(self):
        c, _, _ = self.client_for(scopes=("read", "invoices:write"), name="agent")
        r = c.get("/api/v1/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"organization": "eu", "token": "agent", "scopes": ["read", "invoices:write"]})

    def test_me_needs_read_scope_and_a_token(self):
        c, _, _ = self.client_for(scopes=("invoices:write",))
        self.assertEqual(c.get("/api/v1/me").status_code, 403)
        from rest_framework.test import APIClient
        self.assertEqual(APIClient().get("/api/v1/me").status_code, 401)


class ContactEmailFilterTests(ApiBase):
    def test_contacts_email_filter_uses_the_blind_index(self):
        from apps.contacts.services import find_or_create_contact
        from apps.api import views

        find_or_create_contact(self.org, "Old@Example.com", legal_name="Old")
        for i in range(5):
            find_or_create_contact(self.org, f"n{i}@example.com", legal_name="N")
        c, _, _ = self.client_for(scopes=("read",))
        old = views.Page.default_limit
        views.Page.default_limit = 2
        try:
            self.assertEqual(c.get("/api/v1/contacts").json()["count"], 6)
            for q in ("old@example.com", "OLD@EXAMPLE.COM"):
                body = c.get("/api/v1/contacts", {"email": q}).json()
                self.assertEqual(body["count"], 1)
                self.assertEqual(body["results"][0]["legal_name"], "Old")
            self.assertEqual(c.get("/api/v1/contacts", {"email": "nobody@example.com"}).json()["count"], 0)
        finally:
            views.Page.default_limit = old

    def test_email_filter_is_scoped_to_the_token_org(self):
        from apps.contacts.services import find_or_create_contact

        find_or_create_contact(self.other, "x@example.com", legal_name="Theirs")
        c, _, _ = self.client_for(scopes=("read",))
        self.assertEqual(c.get("/api/v1/contacts", {"email": "x@example.com"}).json()["count"], 0)
