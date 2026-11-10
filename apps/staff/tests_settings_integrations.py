"""API tokens and webhook endpoints on the staff pages (Task 7)."""
import socket
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api.models import ApiToken, hash_token
from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks import events as webhook_events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
URL = "https://hooks.example.com/in/secret-path-token"


def resolving():
    return mock.patch("socket.getaddrinfo", return_value=PUBLIC)


class TokenPagesTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)

    def make(self, scopes=("read",)):
        r = self.c.post(reverse("staff:token_new"), {"org": self.org.pk, "name": "agent", "scopes": list(scopes)})
        return r, ApiToken.objects.filter(name="agent").first()

    def test_created_once_no_store_and_hash(self):
        r, token = self.make(("read", "invoices:write"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r["Cache-Control"])
        plain = r.context["plain"]
        self.assertContains(r, plain)
        self.assertEqual(token.token_hash, hash_token(plain))
        self.assertEqual(token.scopes, ["read", "invoices:write"])
        audit = AuditEntry.objects.get(action="api_token.create")
        self.assertEqual(audit.detail["channel"], "staff")
        listing = self.c.get(reverse("staff:token_list"))
        self.assertContains(listing, "agent")
        self.assertNotContains(listing, plain)
        self.assertNotContains(listing, token.token_hash)

    def test_unknown_scope_refused(self):
        r = self.c.post(reverse("staff:token_new"), {"org": self.org.pk, "name": "x", "scopes": ["root"]})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "unknown scope", status_code=400)
        self.assertFalse(ApiToken.objects.exists())

    def test_name_and_org_required(self):
        r = self.c.post(reverse("staff:token_new"), {"org": self.org.pk, "name": " ", "scopes": ["read"]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ApiToken.objects.exists())
        r = self.c.post(reverse("staff:token_new"), {"org": 9999, "name": "x", "scopes": ["read"]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ApiToken.objects.exists())

    def test_scope_change_audited_with_channel_staff(self):
        _, token = self.make()
        r = self.c.post(reverse("staff:token_scopes", args=[token.pk]), {"scopes": ["read", "payments:confirm"]})
        self.assertEqual(r.status_code, 302)
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["read", "payments:confirm"])
        a = AuditEntry.objects.get(action="api_token.scopes")
        self.assertEqual(a.detail, {"scopes": ["read", "payments:confirm"], "channel": "staff"})

    def test_scope_change_unknown_refused(self):
        _, token = self.make()
        self.c.post(reverse("staff:token_scopes", args=[token.pk]), {"scopes": ["bogus"]})
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["read"])
        self.assertFalse(AuditEntry.objects.filter(action="api_token.scopes").exists())

    def test_revoke_keeps_row_and_token_gets_401(self):
        r, token = self.make()
        plain = r.context["plain"]
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        self.assertEqual(api.get("/api/v1/me").status_code, 200)
        self.assertEqual(self.c.post(reverse("staff:token_revoke", args=[token.pk])).status_code, 302)
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertEqual(api.get("/api/v1/me").status_code, 401)
        a = AuditEntry.objects.get(action="api_token.revoke")
        self.assertEqual(a.detail["channel"], "staff")
        self.assertContains(self.c.get(reverse("staff:token_list")), "Revoked")

    def test_get_and_anonymous(self):
        self.assertEqual(self.c.get(reverse("staff:token_new")).status_code, 405)
        self.assertEqual(self.client.get(reverse("staff:token_list")).status_code, 302)

    def test_settings_index_links(self):
        r = self.c.get(reverse("staff:settings"))
        self.assertContains(r, reverse("staff:token_list"))
        self.assertContains(r, reverse("staff:webhook_list"))

    def test_revoke_form_asks_first_and_ui_js_handles_it(self):
        self.make()
        page = self.c.get(reverse("staff:token_list"))
        self.assertContains(page, 'data-confirm="Revoke this token? Integrations using it stop working immediately."')
        self.assertContains(page, "Created")
        js = (Path(__file__).resolve().parents[2] / "static/ui/ui.js").read_text()
        self.assertIn('addEventListener("submit"', js)
        self.assertIn("data-confirm", js)

    def test_non_ascii_digit_org_is_400_not_500(self):
        r = self.c.post(reverse("staff:token_new"), {"org": "\u00b2", "name": "x", "scopes": ["read"]})
        self.assertEqual(r.status_code, 400)

    def test_anonymous_post_redirects_to_login(self):
        self.assertEqual(self.client.post(reverse("staff:token_new")).status_code, 302)


class WebhookPagesTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)  # creating and deleting take the step-up (red-team A6)

    def make(self, url=URL, **extra):
        with resolving():
            r = self.c.post(reverse("staff:webhook_new"), {"org": self.org.pk, "url": url, **extra})
        return r, WebhookEndpoint.objects.first()

    def test_secret_shown_once_detail_hides_it_and_path(self):
        r, ep = self.make(events=["invoice.paid"], description="ERP")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r["Cache-Control"])
        secret = r.context["plain"]
        self.assertContains(r, secret)
        self.assertEqual(ep.secret, secret)
        self.assertEqual(ep.events, ["invoice.paid"])
        self.assertEqual(AuditEntry.objects.filter(action="webhook.create").count(), 1)
        for name, args in (("staff:webhook_list", []), ("staff:webhook_detail", [ep.pk])):
            page = self.c.get(reverse(name, args=args))
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, secret)
            self.assertNotContains(page, "secret-path-token")
            self.assertContains(page, "hooks.example.com")

    def test_http_and_private_refused(self):
        for url, code in (("http://hooks.example.com/x", "scheme"), ("https://10.0.0.5/x", "private_address"),
                          ("https://hooks.example.com:8443/x", "port")):
            with resolving():
                r = self.c.post(reverse("staff:webhook_new"), {"org": self.org.pk, "url": url})
            self.assertContains(r, code, status_code=400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_unknown_event_refused(self):
        r, ep = self.make(events=["nope.nope"])
        self.assertContains(r, "unknown event", status_code=400)
        self.assertIsNone(ep)

    def test_no_events_means_all(self):
        _, ep = self.make()
        self.assertEqual(ep.events, ["*"])

    def test_delete_keeps_events(self):
        _, ep = self.make()
        event = webhook_events.emit("ping", org=self.org, endpoint=ep, data={"endpoint_id": ep.pk})
        self.assertEqual(WebhookDelivery.objects.filter(endpoint=ep).count(), 1)
        r = self.c.post(reverse("staff:webhook_delete", args=[ep.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(WebhookEndpoint.objects.exists())
        self.assertFalse(WebhookDelivery.objects.exists())
        self.assertTrue(WebhookEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(AuditEntry.objects.filter(action="webhook.delete").exists())

    def test_ping_queues_one_delivery(self):
        _, ep = self.make()
        r = self.c.post(reverse("staff:webhook_ping", args=[ep.pk]))
        self.assertEqual(r.status_code, 302)
        row = WebhookDelivery.objects.get(endpoint=ep)
        self.assertEqual(row.event.type, "ping")
        self.assertContains(self.c.get(reverse("staff:webhook_detail", args=[ep.pk])), "ping")

    def test_ping_inactive_refused(self):
        _, ep = self.make()
        WebhookEndpoint.objects.filter(pk=ep.pk).update(active=False)
        self.c.post(reverse("staff:webhook_ping", args=[ep.pk]))
        self.assertFalse(WebhookDelivery.objects.exists())

    def test_retry_failed_of_this_endpoint_only(self):
        _, ep = self.make()
        other = WebhookEndpoint.objects.create(org=self.org, url=URL, secret="s", events=["*"])
        ev = WebhookEvent.objects.create(type="ping", org=self.org)
        mine = WebhookDelivery.objects.create(event=ev, endpoint=ep, state="failed", attempts=5)
        theirs = WebhookDelivery.objects.create(event=ev, endpoint=other, state="failed", attempts=5)
        self.c.post(reverse("staff:webhook_retry", args=[ep.pk]))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((mine.state, mine.attempts), ("queued", 0))
        self.assertEqual(theirs.state, "failed")

    def test_detail_shows_only_last_50(self):
        _, ep = self.make()
        ev = WebhookEvent.objects.create(type="ping", org=self.org)
        for _ in range(55):
            WebhookDelivery.objects.create(event=ev, endpoint=ep, state="sent")
        r = self.c.get(reverse("staff:webhook_detail", args=[ep.pk]))
        self.assertEqual(len(r.context["deliveries"]), 50)

    def test_endpoint_limit_message(self):
        for _ in range(10):
            WebhookEndpoint.objects.create(org=self.org, url=URL, secret="s", events=["*"])
        with resolving():
            r = self.c.post(reverse("staff:webhook_new"), {"org": self.org.pk, "url": URL})
        self.assertContains(r, "endpoint_limit", status_code=400)

    def test_get_new_is_405_and_anonymous_redirected(self):
        self.assertEqual(self.c.get(reverse("staff:webhook_new")).status_code, 405)
        self.assertEqual(self.client.get(reverse("staff:webhook_list")).status_code, 302)

    def test_delete_form_asks_first(self):
        _, ep = self.make()
        self.assertContains(self.c.get(reverse("staff:webhook_detail", args=[ep.pk])),
                            'data-confirm="Delete this webhook endpoint? Its secret and delivery log are deleted."')

    def test_ping_rate_limited(self):
        from django.core.cache import cache
        cache.clear()
        _, ep = self.make()
        for _ in range(7):
            r = self.c.post(reverse("staff:webhook_ping", args=[ep.pk]), follow=True)
        self.assertContains(r, "Too many pings")
        self.assertEqual(WebhookDelivery.objects.filter(endpoint=ep).count(), 6)

    def test_url_longer_than_2000_refused(self):
        r = self.c.post(reverse("staff:webhook_new"), {"org": self.org.pk, "url": "https://a.example.com/" + "a" * 2000})
        self.assertContains(r, "2000", status_code=400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_created_pages_use_copy_component(self):
        r, _ = self.make()
        self.assertContains(r, 'data-copy="%s"' % r.context["plain"])
