"""Both exporters' output, fed to the real importer: the record shapes the tools write are the ones
`import_invoices` reads. Runs under `manage.py test` (it needs Django and the database)."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.conf import settings

if not settings.configured:  # plain `python -m unittest`: the other tools tests still run
    raise unittest.SkipTest("needs Django: run it with manage.py test")

from django.test import TestCase, override_settings  # noqa: E402

from apps.contacts.models import Contact  # noqa: E402
from apps.invoices import importer  # noqa: E402
from apps.invoices.models import Invoice  # noqa: E402
from apps.orgs.models import Organization  # noqa: E402
from apps.vault.testing import UnsealedVaultMixin  # noqa: E402
from tools.migrate import tests_zaprite_export as zt  # noqa: E402
from tools.migrate import zaprite_export  # noqa: E402

HERE = Path(__file__).parent
NODE = shutil.which("node")


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        lockdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, lockdir, True)
        o = override_settings(INVOICE_IMPORT_LOCK_FILE=os.path.join(lockdir, "import.lock"))
        o.enable()
        self.addCleanup(o.disable)
        self.org = Organization.objects.create(slug="acme", legal_name="Acme Fixture s.r.o.", address="Main 1")


class ZapriteRoundTripTest(Base):
    def export(self):
        out, err = io.StringIO(), io.StringIO()
        client = zt.FakeClient({"/v1/contacts": [zt.CONTACTS], "/v1/orders": [zt.ORDERS]})
        self.assertEqual(zaprite_export.main([], client=client, stdout=out, stderr=err), 0)
        return out.getvalue().split("\n")

    def test_everything_exported_imports_except_the_promo_the_importer_cannot_express(self):
        report = importer.run(self.export(), source="zaprite", org=self.org, dry_run=False)
        self.assertEqual([(ref, reason) for _no, ref, reason in report.refused],
                         [("zaprite:od_promo", "the lines do not add up to total_minor")])
        self.assertEqual(Invoice.objects.filter(org=self.org).count(), 11)
        self.assertEqual(report.contacts_new, 6)
        self.assertTrue(all(i.status == "paid" for i in Invoice.objects.all()))
        again = importer.run(self.export(), source="zaprite", org=self.org, dry_run=False)
        self.assertEqual(again.skipped_existing, 11)  # idempotent
        self.assertEqual(Invoice.objects.count(), 11)


@unittest.skipUnless(NODE, "Node.js is not installed")
class RequestFinanceRoundTripTest(Base):
    def export(self):
        script = ("const rf = require(process.argv[1]); const fs = require('fs');"
                  "const body = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));"
                  "rf.mapInvoices(body.result).then((m) => process.stdout.write(rf.toNdjson(m)));")
        done = subprocess.run([NODE, "-e", script, str(HERE / "request_finance_export.js"),
                               str(HERE / "testdata" / "rf_invoices.json")], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.split("\n")

    def test_the_page_scripts_records_import_cleanly(self):
        lines = self.export()
        dry = importer.run(lines, source="rf", org=self.org, dry_run=True)
        self.assertEqual(dry.refused, [])
        self.assertEqual(Invoice.objects.count(), 0)
        report = importer.run(lines, source="rf", org=self.org, dry_run=False)
        self.assertEqual(report.refused, [])
        self.assertEqual(report.skipped_by_status, {"canceled": 1, "draft": 1})
        self.assertEqual(report.contacts_new, 3)  # a buyer named only by a draft and a canceled invoice: none
        self.assertEqual(report.collisions, [("rf0012", "101", "101-RF")])
        totals = {i.external_reference: (i.currency, i.number) for i in Invoice.objects.all()}
        self.assertEqual(totals, {
            "rf:rf0001": ("EUR", "101"), "rf:rf0002": ("BTC", "102"), "rf:rf0003": ("ETH", "103"),
            "rf:rf0004": ("DAI", "104"), "rf:rf0011": ("USDT", "111"), "rf:rf0012": ("EUR", "101-RF"),
        })
        self.assertEqual(report.totals_by_currency, {"EUR": 118800 + 70000, "BTC": 9900000, "ETH": 12345000,
                                                     "DAI": 225000, "USDT": 25000000})
        eth = Invoice.objects.get(external_reference="rf:rf0003")
        self.assertEqual((eth.status, str(eth.paid_at.date())), ("paid", "2025-05-03"))
        self.assertEqual(Contact.objects.filter(org=self.org).count(), 3)
        again = importer.run(lines, source="rf", org=self.org, dry_run=False)
        self.assertEqual(again.skipped_existing, 6)
