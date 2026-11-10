"""The contact name search reads the table once (audit SEC-6), however many contacts there are, and
folds each stored name once per process."""
from unittest import mock

from django.db import connection
from django.test import TestCase

from apps.contacts import services
from apps.contacts.models import Contact
from apps.contacts.services import find_or_create_contact, search_by_name
from apps.orgs.models import Organization
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import VaultSealed, vault


class NameSearchQueriesTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        services.clear_name_cache()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.other = Organization.objects.create(slug="us", legal_name="Seller LLC")

    def people(self, n, org=None, name="Eva Horvath"):
        for i in range(n):
            find_or_create_contact(org or self.org, f"p{i}-{n}@example.com", legal_name=f"{name} {i}")

    def test_the_query_count_does_not_grow_with_the_table(self):
        self.people(3)
        with self.assertNumQueries(3):  # the names, the matches, their e-mail addresses
            self.assertEqual(len(search_by_name(self.org, "horvath")), 3)
        self.people(40, name="Jan Novak")
        services.clear_name_cache()
        with self.assertNumQueries(3):
            self.assertEqual(len(search_by_name(self.org, "novak")), 20)

    def test_a_name_is_folded_once_and_again_only_when_it_changes(self):
        self.people(5)
        with mock.patch.object(services, "fold_words", wraps=services.fold_words) as fold:
            search_by_name(self.org, "horvath")
            first = fold.call_count
            search_by_name(self.org, "horvath")
            self.assertEqual(fold.call_count, first + 1)  # only the query is folded the second time
        contact = Contact.objects.filter(org=self.org).order_by("pk").first()
        contact.legal_name = "Ida Kralova"
        contact.save(update_fields=["legal_name"])
        self.assertEqual([c.pk for c in search_by_name(self.org, "kralova")], [contact.pk])
        self.assertNotIn(contact.pk, [c.pk for c in search_by_name(self.org, "horvath")])

    def test_results_are_the_same_as_before(self):
        a, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján Novák")
        b, _ = find_or_create_contact(self.org, "b@example.com", legal_name="Eva Novak", display_name="Evička")
        find_or_create_contact(self.other, "z@example.com", legal_name="Zoltan Novak")
        find_or_create_contact(self.org, "a@example.com", legal_name="Copy Person", copy_for=self.other)
        link_buyer = Contact.objects.create(org=self.org, legal_name="Mallory Novak", of_record=False)
        self.assertEqual([c.pk for c in search_by_name(self.org, "novak")], [b.pk, a.pk])  # newest first
        self.assertEqual([c.pk for c in search_by_name(self.org, "evicka")], [b.pk])
        self.assertEqual(search_by_name(self.org, "nov"), [])
        self.assertEqual(search_by_name(self.org, "   "), [])
        self.assertNotIn(link_buyer.pk, [c.pk for c in search_by_name(self.org, "mallory")])
        found = search_by_name(self.org, "jan novak")
        self.assertEqual([c.legal_name for c in found], ["Ján Novák"])
        with self.assertNumQueries(0):
            [e.address for e in found[0].emails.all()]  # the e-mail addresses come with the result

    def test_an_unreadable_name_is_skipped_and_logged_every_time(self):
        bad, _ = find_or_create_contact(self.org, "bad@example.com", legal_name="Novak Broken")
        good, _ = find_or_create_contact(self.org, "good@example.com", legal_name="Novak Fine")
        with connection.cursor() as cur:
            cur.execute("UPDATE contacts_contact SET legal_name = %s WHERE id = %s", ["gAAAAAnotvalid", bad.pk])
        for _ in range(2):
            with self.assertLogs("apps.contacts.services", "ERROR") as logs:
                self.assertEqual([c.pk for c in search_by_name(self.org, "novak")], [good.pk])
            self.assertTrue(any(f"#{bad.pk}" in line for line in logs.output))

    def test_a_sealed_vault_still_refuses_with_names_in_the_cache(self):
        self.people(2)
        search_by_name(self.org, "horvath")
        vault.seal()
        with self.assertRaises(VaultSealed):
            search_by_name(self.org, "horvath")

    def test_the_cache_is_bounded(self):
        with mock.patch.object(services, "NAME_CACHE_MAX", 4):
            self.people(6)
            search_by_name(self.org, "horvath")
            self.assertLessEqual(len(services._NAME_CACHE), 4)
            self.assertEqual(len(search_by_name(self.org, "horvath")), 6)
