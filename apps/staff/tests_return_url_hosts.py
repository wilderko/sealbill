"""The organization form's return_url_hosts textarea."""
from django.test import TestCase
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.staff.forms_settings import OrganizationForm
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin


class ReturnUrlHostsFormTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.url = reverse("staff:org_edit", args=[self.org.pk])
        self.c = verified_staff_client(superuser=True)

    def fields(self, **over):
        data = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
                "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
                "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
                "accounting_currency": "EUR", "return_url_hosts": ""}
        data.update(over)
        return data

    def test_default_has_the_key_and_the_page_shows_the_field(self):
        self.assertEqual(self.org.settings["return_url_hosts"], [])
        self.assertContains(self.c.get(self.url), 'name="return_url_hosts"')

    def test_unchanged_post_reports_nothing_even_without_the_key(self):
        self.org.settings = {"underpay_allowance_bps": 50}
        self.org.save()
        form = OrganizationForm(self.fields(), instance=self.org)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("return_url_hosts", form.changed_data)
        self.assertEqual(form.changed_data, [])

    def test_saves_normalised_and_audits_the_field_name(self):
        r = self.c.post(self.url, self.fields(return_url_hosts="Liberation.Services\r\n\r\nshop.example\r\nliberation.services\r\n"))
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["return_url_hosts"], ["liberation.services", "shop.example"])
        entry = AuditEntry.objects.get(action="organization.update")
        self.assertEqual(entry.detail["fields"], ["return_url_hosts"])

    def test_reposting_the_stored_list_is_no_change(self):
        self.org.settings = {**self.org.settings, "return_url_hosts": ["a.example", "b.example"], "other": 1}
        self.org.save()
        form = OrganizationForm(self.fields(return_url_hosts="B.example\r\na.example"), instance=self.org)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.changed_data, ["return_url_hosts"])  # order is a change
        self.org.refresh_from_db()  # the first form's validation wrote its list onto the instance
        form = OrganizationForm(self.fields(return_url_hosts="a.example\r\nb.example\r\n"), instance=self.org)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.changed_data, [])

    def test_unknown_settings_keys_are_kept(self):
        self.org.settings = {**self.org.settings, "future_key": {"a": 1}}
        self.org.save()
        self.c.post(self.url, self.fields(return_url_hosts="a.example"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["future_key"], {"a": 1})
        self.assertEqual(self.org.settings["return_url_hosts"], ["a.example"])

    def test_not_bare_hosts_are_refused(self):
        for bad in ("https://liberation.services", "liberation.services/path", "liberation.services:443",
                    "*.liberation.services", "a b.example", "user@host.example", "-bad.example"):
            with self.subTest(bad=bad):
                r = self.c.post(self.url, self.fields(return_url_hosts=bad))
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, "Not a bare host name")
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["return_url_hosts"], [])
