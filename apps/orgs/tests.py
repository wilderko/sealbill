import datetime
import threading
from unittest import mock, skipUnless

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.orgs.models import BankAccount, Organization
from apps.orgs.numbering import allocate_number
from apps.vault.registry import encrypted_field_names
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.testing import UnsealedVaultMixin


def make_org(slug="lt", **kw):
    return Organization.objects.create(slug=slug, legal_name="Liberation Travel s.r.o.", **kw)


class NumberingTests(UnsealedVaultMixin, TestCase):
    def test_allocate_number_formats_and_increments(self):
        org = make_org()
        self.assertEqual(allocate_number(org, datetime.date(2026, 11, 2)), "2026-0001")
        self.assertEqual(allocate_number(org, datetime.date(2026, 11, 2)), "2026-0002")
        org.refresh_from_db()
        self.assertEqual(org.next_number, 3)

    def test_allocate_number_is_atomic_under_concurrency(self):
        if connection.vendor == "postgresql":
            self.skipTest("the SQLite spy test applies; the threaded test is PostgreSQL-only")
        org = make_org()
        real = Organization.objects.select_for_update
        seen = []

        def spy(*a, **kw):
            seen.append(connection.in_atomic_block)
            return real(*a, **kw)

        with mock.patch.object(Organization.objects, "select_for_update", side_effect=spy):
            allocate_number(org, datetime.date(2026, 1, 1))
        self.assertEqual(seen, [True])

    def test_select_for_update_issued_on_postgres(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL only")
        org = make_org()
        with CaptureQueriesContext(connection) as ctx:
            allocate_number(org, datetime.date(2026, 1, 1))
        self.assertTrue(any("FOR UPDATE" in q["sql"] for q in ctx.captured_queries))

    def test_bad_number_format_rejected(self):
        org = make_org()
        org.number_format = "{nope}"
        with self.assertRaises(ValidationError):
            org.full_clean()
        org.number_format = "{year}/{seq:05d}"
        org.full_clean()


class OrgRulesTests(UnsealedVaultMixin, TestCase):
    def test_only_one_us_resident_issuer(self):
        make_org("a", is_us_resident_issuer=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_org("b", is_us_resident_issuer=True)
        make_org("c", is_us_resident_issuer=False)
        make_org("d", is_us_resident_issuer=False)

    def test_default_settings(self):
        org = make_org()
        org.refresh_from_db()
        self.assertEqual(
            org.settings,
            {"underpay_allowance_bps": 50, "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": 1000,
             "accounting_currency": "EUR", "return_url_hosts": []},
        )

    def test_bank_account_intermediary_fields_encrypted(self):
        names = encrypted_field_names(BankAccount)
        for n in ("beneficiary_name", "beneficiary_address", "account_number", "bank_name", "bank_address",
                  "bank_swift", "intermediary_bank_name", "intermediary_swift", "intermediary_account",
                  "instructions"):
            self.assertIn(n, names)

    def test_bank_account_roundtrip(self):
        org = make_org()
        BankAccount.objects.create(org=org, currency="EUR", intermediary_swift="DEUTDEFF",
                                   instructions={"en": "Pay"})
        ba = BankAccount.objects.get()
        self.assertEqual(ba.intermediary_swift, "DEUTDEFF")
        self.assertEqual(ba.instructions, {"en": "Pay"})


    def test_number_format_validated_on_save(self):
        for bad in ("{year}", "{seq.__class__}", "{year[0]}{seq}", "{other}{seq}", "{seq"):
            with self.assertRaises(ValidationError, msg=bad):
                Organization(slug="x", number_format=bad).save()
        Organization(slug="ok", number_format="{year}/{seq:05d}").save()


@skipUnless(connection.vendor == "postgresql", "needs row locks (PostgreSQL)")
class NumberingConcurrencyTests(UnsealedVaultMixin, TransactionTestCase):
    def test_two_threads_get_distinct_numbers(self):
        org = make_org()
        start = org.next_number
        results, barrier = [], threading.Barrier(2)

        def work():
            try:
                barrier.wait()
                results.append(allocate_number(Organization.objects.get(pk=org.pk), datetime.date(2026, 1, 1)))
            finally:
                connection.close()

        threads = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(set(results)), 2)
        org.refresh_from_db()
        self.assertEqual(org.next_number, start + 2)


class ReturnUrlHostsMigrationTests(TestCase):
    def run_migration(self):
        import importlib
        from django.apps import apps as real_apps
        importlib.import_module("apps.orgs.migrations.0002_return_url_hosts").allow_the_shop(real_apps, None)

    def test_listed_orgs_get_the_shop_and_others_do_not(self):
        from apps.orgs.models import Organization
        a = Organization.objects.create(slug="liberation")
        b = Organization.objects.create(slug="seychelles", settings={"return_url_hosts": ["shop.example"], "x": 1})
        c = Organization.objects.create(slug="eu")
        self.run_migration()
        self.run_migration()  # again: nothing is added twice
        for org in (a, b, c):
            org.refresh_from_db()
        self.assertEqual(a.settings["return_url_hosts"], ["liberation.services"])
        self.assertEqual(b.settings["return_url_hosts"], ["shop.example", "liberation.services"])
        self.assertEqual(b.settings["x"], 1)
        self.assertEqual(c.settings["return_url_hosts"], [])

    def test_no_such_orgs_is_a_no_op(self):
        self.run_migration()


class ReturnUrlHostsMigrationSealedTests(UnsealedVaultMixin, TestCase):
    def test_runs_with_the_vault_sealed_on_an_org_with_encrypted_columns(self):
        import importlib
        from django.apps import apps as real_apps
        from apps.orgs.models import Organization
        from apps.vault.vault import vault
        Organization.objects.create(slug="liberation", legal_name="Liberation LLC", address="Main 1")
        vault.seal()
        importlib.import_module("apps.orgs.migrations.0002_return_url_hosts").allow_the_shop(real_apps, None)
        vault.unseal("correct horse battery")
        self.assertEqual(Organization.objects.get(slug="liberation").settings["return_url_hosts"], ["liberation.services"])
