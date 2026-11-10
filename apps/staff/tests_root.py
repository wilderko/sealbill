"""The staff pages live at the site root; /staff/ only redirects."""
from urllib.parse import parse_qs, urlsplit

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import resolve, reverse

from apps.core import jobs
from apps.notify import services
from apps.notify.models import OutboxMessage
from apps.rails import jobs as rails_jobs
from apps.staff import device_cookie, urls as staff_urls
from apps.staff.testing import verified_staff_client
from apps.staff.tests import StaffBase
from apps.staff.views import CLIENT_ROOTS
from apps.vault.testing import UnsealedVaultMixin


class RootTests(UnsealedVaultMixin, TestCase):
    def test_needs_person_is_the_root(self):
        c = verified_staff_client()
        self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(reverse("staff:needs_person"), "/")
        self.assertEqual(reverse("staff:invoice_list"), "/invoices/")

    def test_old_paths_redirect_with_query(self):
        r = self.client.get("/staff/invoices/?status=paid")
        self.assertEqual((r.status_code, r["Location"]), (301, "/invoices/?status=paid"))
        self.assertEqual(self.client.head("/staff/").status_code, 301)
        self.assertEqual(self.client.get("/staff/")["Location"], "/")
        self.assertEqual(self.client.post("/staff/invoices/1/void/").status_code, 404)

    def test_old_path_redirect_never_leaves_the_host(self):
        for p in ("/staff//evil.example/x", "/staff/%2F%2Fevil.example", "/staff/\\evil.example",
                  "/staff////evil.example", "/staff/%5Cevil.example"):
            r = self.client.get(p)
            loc = r.get("Location", "")
            self.assertTrue(r.status_code == 404 or (loc.startswith("/") and not loc.startswith("//")
                                                      and not loc.startswith("/\\")), (p, loc))

    def test_bare_staff_and_control_characters(self):
        r = self.client.get("/staff")
        self.assertEqual((r.status_code, r["Location"]), (301, "/"))
        self.assertEqual(self.client.get("/staff/%0d%0aX").status_code, 404)

    def test_next_can_never_leave_the_host(self):
        user = verified_staff_client().user
        for nxt in ("/staff//evil.example", "/staff/\\evil.example", "//evil.example",
                    "/staff/%2F%2Fevil.example", "/staff///\\evil.example"):
            r = self.client.post("/login/", {"username": user.username, "password": "pw-clerk-1", "next": nxt})
            self.assertEqual(r.status_code, 302, nxt)
            target = parse_qs(urlsplit(r["Location"]).query)["next"][0]
            self.assertTrue(target.startswith("/") and not target.startswith(("//", "/\\")), (nxt, target))
            self.client.logout()

    def test_safe_next_maps_the_old_prefix(self):
        c = verified_staff_client()
        c.logout()
        user = c.user
        r = self.client.post("/login/?next=/staff/invoices/1/", {"username": user.username, "password": "pw-clerk-1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/verify/?next=%2Finvoices%2F1%2F")

    def test_no_staff_route_shadows_a_client_route(self):
        firsts = {str(p.pattern).split("/")[0] for p in staff_urls.urlpatterns}
        self.assertFalse(firsts & set(CLIENT_ROOTS))
        for path, module in (("/i/x/", "apps.portal"), ("/p/x/", "apps.portal"), ("/k/x/", "apps.keys"),
                             ("/l/x/", "apps.links"), ("/hooks/btcpay/1/", "apps.rails"),
                             ("/api/v1/me", "apps.api"), ("/healthz", "apps.core"), ("/llms.txt", "apps.api"),
                             ("/unlock/", "apps.vault"), ("/theme/", "apps.core")):
            self.assertTrue(resolve(path).func.__module__.startswith(module), path)

    def test_nav_renders_on_staff_pages_only(self):
        c = verified_staff_client()
        self.assertContains(c.get("/invoices/"), 'class="rail-brand"')
        self.assertNotContains(c.get("/healthz"), 'class="rail-brand"')

    def test_device_cookie_path_is_root(self):
        self.assertEqual(device_cookie.PATH, "/")
        from django.http import HttpResponse
        resp = HttpResponse()
        device_cookie.issue(resp, verified_staff_client().user)
        self.assertEqual(resp.cookies[device_cookie.COOKIE]["path"], "/")


class AlertLinkTests(StaffBase):
    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_alert_links_point_at_the_root(self):
        self.assertEqual(rails_jobs._page(), "https://pay.example/")
        m = services.enqueue("declared_staff", to=["a@example.com"], language="en",
                             context={"ref": "1", "name": "A", "total": "1 EUR", "admin_url": "x"})
        OutboxMessage.objects.filter(pk=m.pk).update(state="failed", attempts=9)
        mail.outbox.clear()
        jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://pay.example/\n", mail.outbox[0].body)
        self.assertNotIn("/staff/", mail.outbox[0].body)
