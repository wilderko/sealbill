"""Audit log, outbox, deliveries and invoice search on the staff pages (Task 9)."""
import datetime

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.models import Contact
from apps.invoices.models import Invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

SENTINEL = "SENTINEL-DETAIL-VALUE"


class AuditPagesTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.c = verified_staff_client()
        self.a = AuditEntry.objects.create(actor_username="alice", action="invoice.void", model_label="invoices.invoice",
                                           object_pk="1", detail={"note": SENTINEL})
        self.b = AuditEntry.objects.create(actor_username="bob", action="contact.edit", model_label="contacts.contact", object_pk="2")
        AuditEntry.objects.filter(pk=self.b.pk).update(created_at=timezone.now() - datetime.timedelta(days=10))

    def ids(self, **params):
        page = self.c.get(reverse("staff:audit_list"), params)
        self.assertEqual(page.status_code, 200)
        return [e.pk for e in page.context["page"].object_list]

    def test_each_filter_narrows(self):
        self.assertEqual(self.ids(), [self.a.pk, self.b.pk])
        self.assertEqual(self.ids(action="contact.edit"), [self.b.pk])
        self.assertEqual(self.ids(model="invoices.invoice"), [self.a.pk])
        self.assertEqual(self.ids(actor="bob"), [self.b.pk])
        recent = (timezone.now() - datetime.timedelta(days=5)).date().isoformat()
        self.assertEqual(self.ids(**{"from": recent}), [self.a.pk])
        self.assertEqual(self.ids(to=recent), [self.b.pk])

    def test_invalid_filters_are_ignored(self):
        self.assertEqual(self.ids(**{"from": "garbage", "to": "2026-13-45", "page": "zzz"}), [self.a.pk, self.b.pk])

    def test_list_never_loads_or_shows_detail(self):
        with CaptureQueriesContext(connection) as ctx:
            page = self.c.get(reverse("staff:audit_list"))
        self.assertNotContains(page, SENTINEL)
        self.assertTrue(all("detail" in e.get_deferred_fields() for e in page.context["page"].object_list))
        entry_sql = [q["sql"] for q in ctx if "audit_auditentry" in q["sql"] and "SELECT" in q["sql"]]
        self.assertTrue(entry_sql and all('"detail"' not in s.split(" FROM ")[0] for s in entry_sql), entry_sql)

    def test_detail_page_shows_detail(self):
        page = self.c.get(reverse("staff:audit_detail", args=[self.a.pk]))
        self.assertContains(page, SENTINEL)

    def test_detail_shows_sealed_when_unreadable(self):
        from unittest.mock import patch

        from apps.vault.vault import VaultSealed
        with patch("apps.staff.views_activity.AuditEntry.objects.only", side_effect=VaultSealed()):
            page = self.c.get(reverse("staff:audit_detail", args=[self.a.pk]))
        self.assertContains(page, "[sealed]")

    def test_paginates_at_50_newest_first(self):
        for i in range(55):
            AuditEntry.objects.create(actor_username="x", action="n", model_label="m", object_pk=str(i))
        page = self.c.get(reverse("staff:audit_list")).context["page"]
        self.assertEqual(len(page.object_list), 50)
        self.assertEqual(page.paginator.num_pages, 2)

    def test_pages_need_staff(self):
        from django.test import Client
        for name in ("audit_list", "outbox_list", "delivery_list"):
            self.assertEqual(Client().get(reverse(f"staff:{name}")).status_code, 302)

    def test_settings_links_to_them(self):
        page = self.c.get(reverse("staff:settings"))
        for name in ("audit_list", "outbox_list", "delivery_list"):
            self.assertContains(page, reverse(f"staff:{name}"))


class OutboxAndDeliveriesTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.c = verified_staff_client()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")

    def test_outbox_shows_failed_row_with_actions_and_no_recipient(self):
        m = OutboxMessage.objects.create(kind="invoice", state="failed", last_error="Boom", to=["secret@example.com"])
        ok = OutboxMessage.objects.create(kind="receipt", state="sent")
        page = self.c.get(reverse("staff:outbox_list"))
        self.assertContains(page, reverse("staff:retry_outbox", args=[m.pk]))
        self.assertContains(page, reverse("staff:dismiss_outbox", args=[m.pk]))
        self.assertNotContains(page, reverse("staff:dismiss_outbox", args=[ok.pk]))
        self.assertNotContains(page, "secret@example.com")
        only = self.c.get(reverse("staff:outbox_list"), {"state": "sent"}).context["page"].object_list
        self.assertEqual([x.pk for x in only], [ok.pk])
        self.assertEqual(self.c.get(reverse("staff:outbox_list"), {"state": "nope"}).status_code, 200)

    def test_deliveries_show_host_only_with_actions(self):
        endpoint = WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/path/TOKEN123?k=v", secret="s", events=["*"])
        event = WebhookEvent.objects.create(type="invoice.paid", org=self.org, data={"x": 1})
        d = WebhookDelivery.objects.create(event=event, endpoint=endpoint, state="failed", last_error_kind="http_503")
        page = self.c.get(reverse("staff:delivery_list"))
        self.assertContains(page, "hook.example")
        self.assertNotContains(page, "TOKEN123")
        self.assertContains(page, reverse("staff:retry_webhook", args=[d.pk]))
        self.assertContains(page, reverse("staff:dismiss_webhook", args=[d.pk]))


class InvoiceSearchTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.c = verified_staff_client()
        org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        contact = Contact.objects.create(org=org, legal_name="Ada", display_name="Ada")
        self.one = Invoice.objects.create(org=org, contact=contact, requested_by_org=org, currency="EUR", number="2026-0001",
                                          issue_date=datetime.date(2026, 11, 1), external_reference="SHOP-ORDER-77")
        self.two = Invoice.objects.create(org=org, contact=contact, requested_by_org=org, currency="EUR", number="2026-0002",
                                          issue_date=datetime.date(2026, 11, 1))

    def found(self, q):
        return [i.pk for i in self.c.get(reverse("staff:invoice_list"), {"q": q}).context["invoices"]]

    def test_finds_by_number_and_external_reference(self):
        self.assertEqual(self.found("2026-0002"), [self.two.pk])
        self.assertEqual(self.found("SHOP-ORDER-77"), [self.one.pk])

    def test_detail_shows_reference_and_payment_link(self):
        page = self.c.get(reverse("staff:invoice", args=[self.one.pk]))
        self.assertContains(page, "SHOP-ORDER-77")
        self.assertNotContains(self.c.get(reverse("staff:invoice", args=[self.two.pk])), "External reference")
