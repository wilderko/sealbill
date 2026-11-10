"""Contacts on the staff pages (Task 8)."""
import datetime

from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse

from apps.audit.models import AuditEntry
from apps.contacts.models import Contact, ContactEmail
from apps.invoices.models import Invoice
from apps.orgs.models import Organization
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault


class ContactPagesTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.other = Organization.objects.create(slug="sey", legal_name="Seychelles Ltd")
        self.contact = self.make(self.org, "client@example.com", "Ada Client")
        self.c = verified_staff_client()

    def make(self, org, email, name, **kw):
        contact = Contact.objects.create(org=org, legal_name=name, display_name=name, **kw)
        ContactEmail.objects.create(contact=contact, address=email, primary=True)
        return contact

    def test_search_by_email_ignores_case_and_spaces(self):
        self.make(self.org, "someone@example.com", "Bob Else")
        page = self.c.get(reverse("staff:contact_list"), {"q": " Client@Example.COM "})
        self.assertContains(page, "Ada Client")
        self.assertNotContains(page, "Bob Else")

    def test_search_finds_nothing_for_unknown_address(self):
        page = self.c.get(reverse("staff:contact_list"), {"q": "nobody@example.com"})
        self.assertNotContains(page, "Ada Client")

    def test_list_is_paginated_at_50(self):
        for i in range(54):
            self.make(self.org, f"p{i}@example.com", f"Person {i}")
        first = self.c.get(reverse("staff:contact_list"))
        self.assertEqual(len(first.context["page"].object_list), 50)
        second = self.c.get(reverse("staff:contact_list"), {"page": 2})
        self.assertEqual(len(second.context["page"].object_list), 5)
        self.assertEqual(self.c.get(reverse("staff:contact_list"), {"page": "junk"}).status_code, 200)

    def test_other_organization_is_visible_and_named(self):
        self.make(self.other, "far@example.com", "Far Away")
        page = self.c.get(reverse("staff:contact_list"))
        self.assertContains(page, "Far Away")
        self.assertContains(page, "Seychelles Ltd")
        only = self.c.get(reverse("staff:contact_list"), {"org": self.other.pk})
        self.assertContains(only, "Far Away")
        self.assertNotContains(only, "Ada Client")

    def test_detail_lists_invoices_emails_and_keys_link(self):
        inv = Invoice.objects.create(org=self.org, contact=self.contact, requested_by_org=self.org, currency="EUR", number="2026-0001",
                                     issue_date=datetime.date(2026, 11, 1))
        page = self.c.get(reverse("staff:contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "client@example.com")
        self.assertContains(page, reverse("staff:invoice", args=[inv.pk]))
        self.assertContains(page, reverse("staff:contact_keys", args=[self.contact.pk]))
        self.assertContains(page, reverse("staff:contact_edit", args=[self.contact.pk]))

    def fields(self, **over):
        data = {"legal_name": "Ada Client", "display_name": "Ada Client", "tax_ids": "", "phone": "", "notes": "",
                "address_line1": "", "address_line2": "", "address_city": "", "address_zip": "", "address_state": "",
                "address_country": "", "preferred_language": "", "us_tax_resident": "unknown"}
        data.update(over)
        return data

    def test_edit_is_audited_with_the_us_flag_change(self):
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        url = reverse("staff:contact_edit", args=[self.contact.pk])
        r = self.c.post(url, self.fields(us_tax_resident="no", address_city="Praha", phone="+420123"))
        self.assertRedirects(r, reverse("staff:contact_detail", args=[self.contact.pk]), fetch_redirect_response=False)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.us_tax_resident, "no")  # staff may lower a stored yes
        self.assertEqual(self.contact.address, {"city": "Praha"})
        entry = AuditEntry.objects.get(action="contact.update")
        self.assertEqual(entry.detail["us_tax_resident"], {"from": "yes", "to": "no"})
        self.assertEqual(entry.detail["fields"], ["address", "phone", "us_tax_resident"])
        self.assertNotIn("Praha", str(entry.detail))

    def test_edit_without_changes_is_not_audited(self):
        url = reverse("staff:contact_edit", args=[self.contact.pk])
        self.c.post(url, self.fields())
        self.assertFalse(AuditEntry.objects.filter(action="contact.update").exists())

    def test_edit_form_prefills_address(self):
        self.contact.address = {"line1": "Main 1", "city": "Brno"}
        self.contact.save()
        page = self.c.get(reverse("staff:contact_edit", args=[self.contact.pk]))
        self.assertContains(page, "Main 1")
        self.assertContains(page, "Brno")

    def test_no_delete_url(self):
        for name in ("contact_delete", "contact_new"):
            with self.assertRaises(NoReverseMatch):
                reverse(f"staff:{name}")
        r = self.c.post(f"/contacts/{self.contact.pk}/delete/")
        self.assertEqual(r.status_code, 404)

    def test_anonymous_is_refused(self):
        anon = Client()
        for name, args in (("staff:contact_list", []), ("staff:contact_detail", [self.contact.pk]),
                           ("staff:contact_edit", [self.contact.pk])):
            r = anon.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn(reverse("staff:login"), r["Location"])

    def test_rail_shows_contacts_and_settings(self):
        page = self.c.get(reverse("staff:needs_person"))
        self.assertContains(page, f'href="{reverse("staff:contact_list")}"')
        self.assertContains(page, f'href="{reverse("staff:settings")}"')
        self.assertContains(self.c.get(reverse("staff:contact_detail", args=[self.contact.pk])), 'aria-current="page"')

    def test_sealed_vault_redirects_to_unlock(self):
        vault.seal()
        r = self.c.get(reverse("staff:contact_list"))
        self.assertRedirects(r, "/unlock/", fetch_redirect_response=False)

    def test_unknown_address_keys_survive_and_untouched_address_is_unchanged(self):
        self.contact.address = {"line1": "Main 1", "region_code": "XY", "city": "Brno"}
        self.contact.save()
        url = reverse("staff:contact_edit", args=[self.contact.pk])
        page = self.c.get(url)
        data = self.fields(address_line1="Main 1", address_city="Brno", phone="+1")
        self.c.post(url, data)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.address, {"line1": "Main 1", "region_code": "XY", "city": "Brno"})
        self.c.post(url, self.fields(address_line1="Main 1", address_city="Plzen", phone="+1"))
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.address, {"line1": "Main 1", "region_code": "XY", "city": "Plzen"})
        self.assertEqual(AuditEntry.objects.filter(action="contact.update").order_by("-pk").first().detail["fields"], ["address"])

    def test_unchanged_post_says_no_changes(self):
        r = self.c.post(reverse("staff:contact_edit", args=[self.contact.pk]), self.fields(), follow=True)
        self.assertContains(r, "No changes.")

    def test_list_queries_do_not_grow_with_rows(self):
        for i in range(2):
            self.make(self.org, f"q{i}@example.com", f"Q {i}")
        self.c.get(reverse("staff:contact_list"))  # warm caches
        with self.assertNumQueries(9):
            self.c.get(reverse("staff:contact_list"))
