import time
from django.test import TestCase

from apps.contacts.models import ContactEmail
from apps.contacts.services import find_contact, find_or_create_contact
from apps.orgs.models import Organization
from apps.vault.testing import UnsealedVaultMixin


def make_org(slug="lt", **kw):
    return Organization.objects.create(slug=slug, legal_name="Liberation Travel s.r.o.", **kw)


class ContactTests(UnsealedVaultMixin, TestCase):
    def test_find_or_create_is_case_insensitive(self):
        org = make_org()
        c1, created1 = find_or_create_contact(org, "A@X.com", legal_name="Ann")
        c2, created2 = find_or_create_contact(org, "a@x.com")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(c1.pk, c2.pk)
        self.assertEqual(ContactEmail.objects.count(), 1)
        self.assertTrue(c1.emails.get().primary)

    def test_lookup_finds_contact_among_250(self):
        org = make_org()
        for i in range(250):
            find_or_create_contact(org, f"user{i}@example.com", legal_name=f"User {i}")
        found = find_contact(org, "USER249@example.com")
        self.assertIsNotNone(found)
        self.assertEqual(found.legal_name, "User 249")
        self.assertIsNone(find_contact(org, "nobody@example.com"))

    def test_same_email_in_other_org_is_separate(self):
        a, b = make_org("a"), make_org("b")
        ca, _ = find_or_create_contact(a, "x@x.com")
        cb, created = find_or_create_contact(b, "x@x.com")
        self.assertTrue(created)
        self.assertNotEqual(ca.pk, cb.pk)

    def test_unknown_us_flag_is_not_resident(self):
        c, _ = find_or_create_contact(make_org(), "u@x.com")
        self.assertEqual(c.us_tax_resident, "unknown")
        self.assertFalse(c.is_us_tax_resident)
        c.us_tax_resident = "yes"
        self.assertTrue(c.is_us_tax_resident)

    def test_language_falls_back_to_org_then_english(self):
        c, _ = find_or_create_contact(make_org(default_language="sk"), "l@x.com", preferred_language="pt")
        self.assertEqual(c.language, "sk")
        c.preferred_language = "de"
        self.assertEqual(c.language, "de")
        c2, _ = find_or_create_contact(make_org("b", default_language=""), "m@x.com", preferred_language="pt")
        self.assertEqual(c2.language, "en")


class EmptyEmailTests(UnsealedVaultMixin, TestCase):
    def test_empty_email_is_refused(self):
        org = make_org()
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                find_or_create_contact(org, bad)
        self.assertEqual(ContactEmail.objects.count(), 0)

    def test_find_contact_empty_is_none(self):
        org = make_org()
        self.assertIsNone(find_contact(org, ""))
        self.assertIsNone(find_contact(org, "  "))

    def test_saving_empty_contact_email_raises(self):
        org = make_org()
        c, _ = find_or_create_contact(org, "a@x.com")
        with self.assertRaises(ValueError):
            ContactEmail(contact=c, address="").save()


class ContactAdminTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from apps.staff.models import StaffTOTPDevice

        self.admin = get_user_model().objects.create_superuser("root", password="pw-root-1")
        device = StaffTOTPDevice.objects.create(user=self.admin, name="t", confirmed=True)
        self.client.force_login(self.admin)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session["staff_verified_at"] = int(time.time())  # a fresh second factor: admin changes need it
        session.save()
        self.org = make_org()
        self.other = make_org("other")
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ann")

    def post_change(self, **overrides):
        row = self.contact.emails.get()
        data = {
            "org": self.org.pk, "legal_name": "Ann", "display_name": "", "address": "null", "tax_ids": "",
            "phone": "", "notes": "", "preferred_language": "", "us_tax_resident": "unknown",
            "emails-TOTAL_FORMS": "1", "emails-INITIAL_FORMS": "1", "emails-MIN_NUM_FORMS": "0",
            "emails-MAX_NUM_FORMS": "1000", "emails-0-id": row.pk, "emails-0-contact": self.contact.pk,
            "emails-0-address": "a@example.com", "emails-0-primary": "on", **overrides,
        }
        return self.client.post(f"/hidden-admin-xyz/contacts/contact/{self.contact.pk}/change/", data)

    def test_flag_change_in_the_admin_is_audited(self):
        from apps.audit.models import AuditEntry

        r = self.post_change(us_tax_resident="yes")
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.us_tax_resident, "yes")
        e = AuditEntry.objects.get(action="contact.update", object_pk=str(self.contact.pk))
        self.assertEqual((e.actor, e.detail["us_tax_resident"]), (self.admin, {"from": "unknown", "to": "yes"}))

    def test_org_is_read_only_on_change(self):
        page = self.client.get(f"/hidden-admin-xyz/contacts/contact/{self.contact.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="org"')
        r = self.post_change(org=self.other.pk)
        self.assertEqual(r.status_code, 302)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.org, self.org)
        self.assertContains(self.client.get("/hidden-admin-xyz/contacts/contact/add/"), 'name="org"')  # chosen when adding
