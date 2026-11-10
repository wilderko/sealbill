"""import_invoices: paid, numbered, side-effect-free history from NDJSON."""
import datetime
import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.audit.models import AuditEntry
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import find_contact
from apps.invoices import importer
from apps.invoices.models import Invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.payments.models import Payment
from apps.quotes.models import Quote
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks.models import WebhookEvent


def contact(sid, email="a@example.com", **over):
    rec = {"type": "contact", "source_id": sid, "email": email, "legal_name": f"Name {sid}", "display_name": "",
           "address": {"line1": "Main 1", "city": "Town", "country": "SK"}, "tax_id": "", "language": "en"}
    rec.update(over)
    return rec


def invoice(sid, csid, number="100", total=133700, **over):
    rec = {"type": "invoice", "source_id": sid, "number": number, "kind": "invoice", "contact_source_id": csid,
           "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14,
           "title": "Work", "footer": "Thanks for paying",
           "lines": [{"name": "Service", "description": "d", "quantity": 1, "unit_price_minor": total}],
           "total_minor": total, "source_status": "COMPLETE", "source_url": f"https://z.example/{sid}"}
    rec.update(over)
    return rec


def ndjson(*records):
    return [json.dumps(r) for r in records]


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.lockdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.lockdir, True)
        o = override_settings(INVOICE_IMPORT_LOCK_FILE=os.path.join(self.lockdir, "import.lock"))
        o.enable()
        self.addCleanup(o.disable)
        self.lib = Organization.objects.create(slug="liberation", legal_name="Liberation LLC", address="X 1")
        self.sey = Organization.objects.create(slug="seychelles", legal_name="Liberation Travel Ltd.",
                                               is_us_resident_issuer=True)

    def run_it(self, records, source="zaprite", org=None, dry_run=False):
        return importer.run(ndjson(*records), source=source, org=org or self.lib, dry_run=dry_run)


class HappyTests(Base):
    def batch(self):
        return [
            contact("c1", "one@example.com"), contact("c2", "two@example.com"),
            invoice("od_1", "c1", "449"), invoice("od_2", "c1", "450", total=5000),
            invoice("od_3", "c2", "451", currency="USD"),
            invoice("od_4", "c2", None, kind="payment_request", total=700),
        ]

    def test_rows(self):
        rep = self.run_it(self.batch())
        self.assertEqual((rep.contacts_new, rep.contacts_merged, rep.contacts_without_email), (2, 0, 0))
        self.assertEqual(Invoice.objects.count(), 4)
        self.assertEqual(rep.refused, [])
        for inv in Invoice.objects.all():
            self.assertEqual(inv.status, "paid")
            self.assertEqual(inv.org, self.lib)
            self.assertEqual(inv.requested_by_org, self.lib)
            self.assertEqual(inv.offered_methods, [])
            self.assertEqual(inv.notes, "Thanks for paying")
            self.assertIsNotNone(inv.snapshot)
            self.assertIsNone(inv.view_token_hash)
            self.assertIsNone(inv.pay_token_hash)
            pays = list(inv.payments.all())
            self.assertEqual(len(pays), 1)
            p = pays[0]
            self.assertEqual((p.rail, p.state, p.final), ("manual", "confirmed", True))
            self.assertEqual(p.amount_minor, inv.lines.first().unit_price_minor)
            self.assertEqual(p.external_id, "migration:zaprite:liberation:" + inv.metadata["migration"]["source_id"])
            self.assertEqual(p.confirmed_at, inv.paid_at)
        inv = Invoice.objects.get(external_reference="zaprite:od_1")
        self.assertEqual(inv.number, "449")
        self.assertEqual(str(inv.issue_date), "2025-04-01")
        self.assertEqual(str(inv.due_date), "2025-04-15")
        self.assertEqual(inv.paid_at.date().isoformat(), "2025-04-03")
        self.assertEqual(inv.sent_at.date().isoformat(), "2025-04-01")
        self.assertEqual(set(inv.metadata), {"migration"})
        self.assertEqual(set(inv.metadata["migration"]),
                         {"source", "source_id", "source_number", "source_status", "source_url", "migrated_at"})
        self.assertEqual(inv.metadata["migration"]["source"], "zaprite")
        self.assertEqual(inv.metadata["migration"]["source_number"], "449")
        self.assertEqual(inv.metadata["migration"]["source_status"], "COMPLETE")
        pr = Invoice.objects.get(external_reference="zaprite:od_4")
        self.assertEqual((pr.kind, pr.number), ("payment_request", None))
        self.assertEqual(pr.status, "paid")
        self.assertEqual(rep.invoices_by_kind_currency[("invoice", "EUR")], 2)
        self.assertEqual(rep.invoices_by_kind_currency[("payment_request", "EUR")], 1)
        self.assertEqual(rep.totals_by_currency["EUR"], 133700 + 5000 + 700)
        self.assertEqual(rep.source_status_counts, {"COMPLETE": 4})

    def test_audit(self):
        self.run_it(self.batch())
        self.assertEqual(AuditEntry.objects.filter(action="invoice.migrate").count(), 4)
        e = AuditEntry.objects.filter(action="invoice.migrate").first()
        self.assertEqual(e.detail["source"], "zaprite")
        self.assertIn(e.detail["source_id"], {"od_1", "od_2", "od_3", "od_4"})
        self.assertEqual(set(e.detail), {"source", "source_id"})
        run = AuditEntry.objects.get(action="migration.run").detail
        self.assertEqual(run["invoices"], 4)
        self.assertIn("skipped_by_status", run)
        self.assertEqual(AuditEntry.objects.filter(action="contact.create").exclude(actor_username="importer").count(), 0)
        self.assertEqual(AuditEntry.objects.filter(action="migration.run").count(), 1)

    def test_zero_total_is_paid_without_payment(self):
        self.run_it([contact("c1"), invoice("od_0", "c1", "9", total=0)])
        inv = Invoice.objects.get()
        self.assertEqual(inv.status, "paid")
        self.assertIsNotNone(inv.paid_at)
        self.assertEqual(Payment.objects.count(), 0)

    def test_no_paid_date_falls_back_to_issue_date(self):
        self.run_it([contact("c1"), invoice("od_1", "c1", paid_date=None)])
        self.assertEqual(Invoice.objects.get().paid_at.date().isoformat(), "2025-04-01")


class ContactTests(Base):
    def test_same_email_across_sources_is_one_contact_and_buyers_stay_their_own(self):
        self.run_it([contact("c1", "Jan@X.sk", legal_name="Jan Zaprite"), invoice("od_1", "c1", "1")])
        rep = self.run_it([contact("r1", " jan@x.sk ", legal_name="Jan Novak RF"),
                           invoice("rf_1", "r1", "2", currency="EUR")], source="rf")
        self.assertEqual(Contact.objects.filter(org=self.lib).count(), 1)
        self.assertEqual(ContactEmail.objects.count(), 1)
        self.assertEqual((rep.contacts_new, rep.contacts_merged), (0, 1))
        a = Invoice.objects.get(external_reference="zaprite:od_1")
        b = Invoice.objects.get(external_reference="rf:rf_1")
        self.assertEqual(a.contact_id, b.contact_id)
        self.assertEqual(a.snapshot["buyer"]["name"], "Jan Zaprite")
        self.assertEqual(b.snapshot["buyer"]["name"], "Jan Novak RF")

    def test_merge_most_recent_wins_field_by_field(self):
        self.run_it([contact("c1", "a@x.sk", legal_name="Old Name", tax_id="TAX1", display_name="Old Disp"),
                     invoice("od_1", "c1", "1", issue_date="2024-01-01")])
        self.run_it([contact("r1", "A@x.sk", legal_name="New Name", tax_id="", display_name="New Disp"),
                     invoice("rf_1", "r1", "2", issue_date="2025-06-01")], source="rf")
        c = find_contact(self.lib, "a@x.sk")
        self.assertEqual(c.legal_name, "New Name")
        self.assertEqual(c.display_name, "New Disp")
        self.assertEqual(c.tax_ids, "TAX1")  # the newer record had none

    def test_older_record_does_not_overwrite(self):
        self.run_it([contact("c1", "a@x.sk", legal_name="New Name"), invoice("od_1", "c1", "1", issue_date="2025-06-01")])
        self.run_it([contact("r1", "a@x.sk", legal_name="Old Name", tax_id="T9"),
                     invoice("rf_1", "r1", "2", issue_date="2023-01-01")], source="rf")
        c = find_contact(self.lib, "a@x.sk")
        self.assertEqual(c.legal_name, "New Name")
        self.assertEqual(c.tax_ids, "T9")

    def test_within_one_run_the_newer_record_wins_whatever_the_order(self):
        self.run_it([contact("c_new", "a@x.sk", legal_name="Newer"), contact("c_old", "A@X.sk", legal_name="Older"),
                     invoice("od_1", "c_new", "1", issue_date="2025-06-01"),
                     invoice("od_2", "c_old", "2", issue_date="2022-01-01")])
        self.assertEqual(find_contact(self.lib, "a@x.sk").legal_name, "Newer")

    def test_without_email_once_per_source_id_and_never_merged_by_name(self):
        recs = [contact("c1", None, legal_name="Same Name"), contact("c2", "", legal_name="Same Name"),
                invoice("od_1", "c1", "1"), invoice("od_2", "c2", "2")]
        rep = self.run_it(recs)
        self.assertEqual(rep.contacts_without_email, 2)
        self.assertEqual(Contact.objects.count(), 2)
        self.assertNotEqual(*Invoice.objects.values_list("contact_id", flat=True))
        rep = self.run_it(recs)  # again: still two
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(rep.skipped_existing, 2)

    def test_seychelles_rf_is_us_resident(self):
        self.run_it([contact("r1", "us@x.com"), contact("r2", None), invoice("rf_1", "r1", "1")],
                    source="rf", org=self.sey)
        self.assertEqual(set(Contact.objects.values_list("us_tax_resident", flat=True)), {"yes"})

    def test_others_stay_unknown(self):
        self.run_it([contact("c1")])
        self.assertEqual(Contact.objects.get().us_tax_resident, "unknown")
        self.run_it([contact("r1", "b@x.com")], source="rf")
        self.assertEqual(Contact.objects.get(emails__address_index__isnull=False, pk=Contact.objects.latest("pk").pk)
                         .us_tax_resident, "unknown")


class NumberTests(Base):
    def test_collisions(self):
        self.run_it([contact("c1"), invoice("od_1", "c1", "591")])
        rep = self.run_it([contact("r1"), invoice("rf_1", "r1", "591"), invoice("rf_2", "r1", "591")], source="rf")
        nums = dict(Invoice.objects.values_list("external_reference", "number"))
        self.assertEqual(nums["zaprite:od_1"], "591")
        self.assertEqual(nums["rf:rf_1"], "591-RF")
        self.assertEqual(nums["rf:rf_2"], "591-RF2")
        self.assertEqual(rep.collisions, [("rf_1", "591", "591-RF"), ("rf_2", "591", "591-RF2")])

    def test_zaprite_duplicate(self):
        rep = self.run_it([contact("c1"), invoice("od_1", "c1", "7"), invoice("od_2", "c1", "7"),
                           invoice("od_3", "c1", "7")])
        self.assertEqual(sorted(Invoice.objects.values_list("number", flat=True)), ["7", "7-Z2", "7-Z3"])
        self.assertEqual(len(rep.collisions), 2)

    def test_next_number_unchanged(self):
        self.run_it([contact("c1"), invoice("od_1", "c1", "1")])
        self.lib.refresh_from_db()
        self.assertEqual(self.lib.next_number, 1)


class RefusalTests(Base):
    def test_one_cent_off_is_refused_and_the_rest_imported(self):
        bad = invoice("od_bad", "c1", "2", total=133701)
        bad["lines"][0]["unit_price_minor"] = 133700
        rep = self.run_it([contact("c1"), invoice("od_1", "c1", "1"), bad, invoice("od_3", "c1", "3")])
        self.assertEqual(Invoice.objects.count(), 2)
        self.assertFalse(Invoice.objects.filter(external_reference="zaprite:od_bad").exists())
        self.assertEqual(len(rep.refused), 1)
        self.assertEqual(rep.refused[0][0], 3)
        self.assertIn("total", rep.refused[0][2])

    def test_malformed_lines(self):
        good = contact("c1")
        missing = invoice("od_m", "c1", "5")
        del missing["currency"]
        lines = [json.dumps(good), "{not json", json.dumps({"type": "mystery", "source_id": "x"}),
                 json.dumps(missing), "", json.dumps(invoice("od_ok", "c1", "6")),
                 json.dumps(invoice("od_nocontact", "nobody", "8")), "[1]"]
        rep = importer.run(lines, source="zaprite", org=self.lib, dry_run=False)
        self.assertEqual([n for n, *_ in rep.refused], [2, 3, 4, 7, 8])
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(rep.invoices_by_kind_currency[("invoice", "EUR")], 1)

    def test_rf_unsent_and_cancelled_are_skipped_and_counted(self):
        recs = [contact("r1")] + [invoice(f"rf_{s}", "r1", str(i), source_status=s)
                                  for i, s in enumerate(["draft", "canceled", "rejected", "awaitingPayment"])]
        rep = self.run_it(recs, source="rf")
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(rep.skipped_by_status, {"draft": 1, "canceled": 1, "rejected": 1})
        self.assertEqual(rep.source_status_counts, {"awaitingPayment": 1})


class RerunTests(Base):
    def test_half_failed_run_then_rerun(self):
        recs = [contact("c1"), contact("c2", None)] + [invoice(f"od_{i}", "c1" if i % 2 else "c2", str(i))
                                                       for i in range(1, 6)]
        real = importer._write_payment
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("disk gone")
            return real(*a, **kw)

        with mock.patch.object(importer, "_write_payment", flaky):
            with self.assertRaises(RuntimeError):
                self.run_it(recs)
        self.assertEqual(Invoice.objects.count(), 2)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(AuditEntry.objects.filter(action="migration.run").count(), 0)
        rep = self.run_it(recs)
        self.assertEqual(Invoice.objects.count(), 5)
        self.assertEqual(Payment.objects.count(), 5)
        self.assertEqual(rep.skipped_existing, 2)
        self.assertEqual(rep.invoices_by_kind_currency[("invoice", "EUR")], 3)
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(len(set(Invoice.objects.values_list("number", flat=True))), 5)
        self.assertEqual(AuditEntry.objects.filter(action="invoice.migrate").count(), 5)

    def test_rerun_skips_everything(self):
        recs = [contact("c1"), invoice("od_1", "c1", "1")]
        self.run_it(recs)
        rep = self.run_it(recs)
        self.assertEqual((rep.skipped_existing, Invoice.objects.count(), Payment.objects.count()), (1, 1, 1))
        self.assertEqual(rep.invoices_by_kind_currency, {})


class DryRunTests(Base):
    def test_writes_nothing(self):
        recs = [contact("c1"), contact("c2", None), invoice("od_1", "c1", "1"), invoice("od_2", "c2", "1")]
        rep = self.run_it(recs, dry_run=True)
        for model in (Contact, ContactEmail, Invoice, Payment, AuditEntry):
            self.assertEqual(model.objects.count(), 0, model)
        self.assertEqual((rep.contacts_new, rep.contacts_without_email), (2, 1))
        self.assertEqual(len(rep.collisions), 1)
        self.assertEqual(rep.totals_by_currency["EUR"], 267400)


class NoSideEffectsTests(Base):
    def test_nothing_fires(self):
        self.run_it([contact("c1"), invoice("od_1", "c1", "1"), invoice("od_2", "c1", None, kind="payment_request")])
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertEqual(WebhookEvent.objects.count(), 0)
        self.assertEqual(Quote.objects.count(), 0)
        self.assertEqual(Invoice.objects.exclude(view_token_hash=None).count(), 0)
        self.assertEqual(Invoice.objects.exclude(pay_token_hash=None).count(), 0)
        self.assertTrue(all(i.view_token_copy is None and i.pay_token_copy is None for i in Invoice.objects.all()))
        self.assertFalse(Invoice.objects.filter(client_letters=True).exists())


class CommandTests(Base):
    def call(self, records, *args, stdin=None):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin if stdin is not None else "\n".join(ndjson(*records)))):
            with redirect_stdout(out):
                call_command("import_invoices", *args, stdout=out)
        return out.getvalue()

    def test_prints_no_personal_data(self):
        recs = [contact("c1", "secret.person@example.com", legal_name="Zuzana Tajna",
                        address={"line1": "Hidden Street 99", "city": "Secretville", "country": "SK"}),
                contact("c2", None, legal_name="Peter Skryty"),
                invoice("od_1", "c1", "5", title="Private Title"), invoice("od_2", "c2", "5"),
                {"type": "invoice", "source_id": "od_bad", "oops": "Zuzana Tajna"}]
        for extra in (["--dry-run"], []):
            out = self.call(recs, "--source", "zaprite", "--org", "liberation", *extra)
            for s in ("secret.person", "Zuzana", "Tajna", "Hidden Street", "Secretville", "Peter", "Skryty",
                      "Private Title", "example.com"):
                self.assertNotIn(s, out)
            self.assertIn("zaprite:od_2", out)  # the collision, named by id
            self.assertRegex(out, r"line 5")

    def test_btc_check_prints_three_fields(self):
        recs = [contact("c1", "x@y.com", legal_name="Nameless Person"),
                invoice("od_btc", "c1", "1", currency="BTC", total=123456,
                        lines=[{"name": "n", "quantity": 1, "unit_price_minor": 123456}],
                        source_url="https://app.zaprite.com/o/od_btc"),
                invoice("od_eur", "c1", "2")]
        out = self.call(recs, "--source", "zaprite", "--org", "liberation", "--dry-run", "--btc-check", "od_btc,od_zzz")
        lines = [l for l in out.splitlines() if l.startswith("btc-check")]
        self.assertIn("od_btc", lines[0])
        self.assertIn("123456", lines[0])
        self.assertIn("https://app.zaprite.com/o/od_btc", lines[0])
        self.assertNotIn("Nameless", out)
        self.assertNotIn("x@y.com", out)
        self.assertEqual(len(lines), 2)  # the unknown id is reported as not found
        self.assertIn("od_zzz", lines[1])

    def test_unknown_org_is_an_error(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.call([], "--source", "zaprite", "--org", "nope")


class LineAdjustmentTests(Base):
    def one(self, line, total, sid="od_1"):
        rec = invoice(sid, "c1", sid, total=total, lines=[{"name": "n", "quantity": 1, "unit_price_minor": 10000, **line}])
        return importer.run(ndjson(contact("c1"), rec), source="zaprite", org=self.lib, dry_run=False)

    def test_discount_and_tax_import(self):
        self.assertEqual(self.one({"discount_amount_minor": 1000}, 9000, "a").refused, [])
        self.assertEqual(self.one({"discount_rate_bps": 1000, "tax_rate_bps": 2000}, 10800, "b").refused, [])
        li = Invoice.objects.get(external_reference="zaprite:b").lines.get()
        self.assertEqual((li.discount_rate_bps, li.tax_rate_bps, li.discount_amount_minor), (1000, 2000, 0))
        self.assertEqual(Invoice.objects.get(external_reference="zaprite:a").lines.get().discount_amount_minor, 1000)
        self.assertEqual(Payment.objects.get(invoice__external_reference="zaprite:b").amount_minor, 10800)

    def test_wrong_total_is_refused(self):
        rep = self.one({"discount_amount_minor": 1000}, 10000)
        self.assertEqual(len(rep.refused), 1)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_bad_values_are_refused_with_the_line_number(self):
        for bad in ({"discount_amount_minor": -1}, {"tax_rate_bps": 1.5}, {"discount_rate_bps": "10"},
                    {"discount_rate_bps": 10001}, {"tax_rate_bps": 10001}, {"tax_rate_bps": True}):
            rep = self.one(bad, 10000)
            self.assertEqual([n for n, *_ in rep.refused], [2], bad)
        self.assertEqual(Invoice.objects.count(), 0)


class MissingContactTests(Base):
    def test_refused_with_reason_and_run_goes_on(self):
        rep = self.run_it([contact("c1"), invoice("od_1", "ghost", "1"), invoice("od_2", "c1", "2")])
        self.assertEqual(rep.refused, [(2, "zaprite:od_1", "contact_source_id names no contact record")])
        self.assertEqual(list(Invoice.objects.values_list("external_reference", flat=True)), ["zaprite:od_2"])


class FixRound1Tests(Base):
    def test_payment_is_dated_in_the_books_by_the_source(self):
        from apps.exports.csv import payments_rows
        self.run_it([contact("c1"), invoice("od_1", "c1", "1", paid_date="2025-04-03")])
        p = Payment.objects.get()
        self.assertEqual(p.detected_at, p.confirmed_at)
        self.assertEqual(p.detected_at, p.invoice.paid_at)
        today = datetime.date.today()
        self.assertEqual(len(list(payments_rows([self.lib.pk], today.replace(day=1), today))), 1)  # header only
        rows = list(payments_rows([self.lib.pk], datetime.date(2025, 4, 1), datetime.date(2025, 4, 30)))
        self.assertEqual(len(rows), 2)

    def test_native_shaped_number_at_or_above_next_number_is_refused(self):
        self.lib.next_number = 5
        self.lib.save()
        rep = self.run_it([contact("c1"), invoice("od_1", "c1", "2026-0007"), invoice("od_2", "c1", "2026-0005"),
                           invoice("od_3", "c1", "2026-0003"), invoice("od_4", "c1", "591"), invoice("od_5", "c1", "LS-9")])
        self.assertEqual(sorted(Invoice.objects.values_list("number", flat=True)), ["2026-0003", "591", "LS-9"])
        self.assertEqual([n for n, *_ in rep.refused], [2, 3])
        self.assertIn("numbering", rep.refused[0][2])

    def test_rerun_leaves_contacts_alone_and_counts_add_up(self):
        recs = [contact("c1", "a@x.sk", legal_name="Orig"), contact("c2", None, legal_name="Nomail"),
                invoice("od_1", "c1", "1"), invoice("od_2", "c2", "2")]
        first = self.run_it(recs)
        self.assertEqual(first.contacts_new + first.contacts_merged + first.contacts_skipped_existing, 2)
        c = find_contact(self.lib, "a@x.sk")
        c.legal_name = "Corrected by staff"
        c.save()
        again = self.run_it(recs)
        self.assertEqual(find_contact(self.lib, "a@x.sk").legal_name, "Corrected by staff")
        self.assertEqual((again.contacts_new, again.contacts_merged, again.contacts_skipped_existing), (0, 0, 2))
        self.assertEqual(Contact.objects.count(), 2)

    def test_sealed_vault_is_a_command_error_before_anything_is_read(self):
        from django.core.management.base import CommandError
        from apps.vault.vault import vault
        vault.seal()

        class Boom:
            def read(self):
                raise AssertionError("stdin was read")

        with mock.patch("sys.stdin", Boom()):
            with self.assertRaisesRegex(CommandError, "(?i)unseal"):
                call_command("import_invoices", "--source", "zaprite", "--org", "liberation")

    def test_out_of_range_values_are_refused_per_line(self):
        def inv(sid, **over):
            return json.dumps(invoice(sid, "c1", over.pop("number", sid), **over))
        big = invoice("od_big", "c1", "b", total=10**20)
        big["lines"][0]["unit_price_minor"] = 10**20
        qty = invoice("od_q", "c1", "q")
        qty["lines"][0]["quantity"] = 10**10
        rate = invoice("od_r", "c1", "r")
        rate["lines"][0]["tax_rate_bps"] = 10001
        lines = [json.dumps(contact("c1")), json.dumps(big), json.dumps(qty), json.dumps(rate),
                 inv("od_d1", issue_date="0001-01-01"), inv("od_d2", paid_date="9999-12-31"),
                 inv("od_d3", due_days=10**9), inv("x" * 300), inv("od_ok", number="ok")]
        rep = importer.run(lines, source="zaprite", org=self.lib, dry_run=False)
        self.assertEqual([n for n, *_ in rep.refused], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(list(Invoice.objects.values_list("external_reference", flat=True)), ["zaprite:od_ok"])

    def test_second_run_at_once_is_refused_and_writes_nothing(self):
        import fcntl
        from django.core.management.base import CommandError
        path = os.path.join(self.lockdir, "import.lock")
        with open(path, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(importer.ImportBusy):
                self.run_it([contact("c1"), invoice("od_1", "c1", "1")])
            with self.assertRaisesRegex(CommandError, "another import"):
                CommandTests.call(self, [contact("c1")], "--source", "zaprite", "--org", "liberation")
        self.assertEqual((Contact.objects.count(), Invoice.objects.count()), (0, 0))
        self.run_it([contact("c1"), invoice("od_1", "c1", "1")])  # the lock was released
        self.assertEqual(Invoice.objects.count(), 1)

    def test_repeated_contact_source_id_refused_first_stands(self):
        rep = self.run_it([contact("c1", "a@x.sk", legal_name="First"), contact("c1", "b@x.sk", legal_name="Second"),
                           invoice("od_1", "c1", "1")])
        self.assertEqual([n for n, *_ in rep.refused], [2])
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Invoice.objects.get().snapshot["buyer"]["name"], "First")

    def test_missing_language_gets_the_org_default_stated_one_is_kept(self):
        self.lib.default_language = "sk"
        self.lib.save()
        self.run_it([contact("c1", "a@x.sk", language=None), contact("c2", "b@x.sk", language=""),
                     contact("c3", "c@x.sk", language="de")])
        langs = {find_contact(self.lib, e).preferred_language for e in ("a@x.sk", "b@x.sk")}
        self.assertEqual(langs, {"sk"})
        self.assertEqual(find_contact(self.lib, "c@x.sk").preferred_language, "de")
        self.run_it([contact("r1", "c@x.sk", language=None), invoice("rf_1", "r1", "1", issue_date="2026-01-01")],
                    source="rf")
        self.assertEqual(find_contact(self.lib, "c@x.sk").preferred_language, "de")

    def test_snapshot_seller_and_buyer_tax_ids(self):
        self.run_it([contact("c1", tax_id="SK123"), invoice("od_1", "c1", "1")])
        snap = Invoice.objects.get().snapshot
        self.assertEqual(snap["seller"]["legal_name"], "Liberation LLC")
        self.assertEqual(snap["buyer"]["tax_ids"], "SK123")
        self.assertEqual(snap["methods"], [])


FIXTURE = os.path.join(os.path.dirname(__file__), "testdata", "zaprite_export.ndjson")


class WholeAddressTests(Base):
    """The contact `address` is exactly the Contact's own shape; nothing is dropped without a word."""
    FULL = {"line1": "Fixture Street 1", "line2": "Floor 2", "city": "Bratislava", "zip": "81101",
            "state": "BA", "country": "sk"}

    def test_a_full_address_is_stored_whole(self):
        self.run_it([contact("c1", address=self.FULL), invoice("od_1", "c1")])
        c = Contact.objects.get()
        self.assertEqual(c.address, {**self.FULL, "country": "SK"})

    def test_the_frozen_buyer_carries_postcode_and_country(self):
        self.run_it([contact("c1", address=self.FULL), invoice("od_1", "c1")])
        lines = Invoice.objects.get().snapshot["buyer"]["address_lines"]
        self.assertEqual(lines, ["Fixture Street 1", "Floor 2", "81101 Bratislava", "BA", "SK"])

    def test_an_unknown_key_refuses_the_contact_by_id(self):
        for key in ("zipCode", "countryCode", "street", "postal_code"):
            rep = self.run_it([contact("c9", address={"line1": "A", key: "x"})])
            self.assertEqual(rep.refused[0][1], "zaprite:c9", key)
            self.assertIn("address", rep.refused[0][2])
            self.assertIn(key, rep.refused[0][2])
            self.assertEqual(Contact.objects.count(), 0)

    def test_country_must_be_alpha2(self):
        for bad in ("Slovakia", "SVK", "1", ""):
            rep = self.run_it([contact("c9", address={"line1": "A", "country": bad})])
            if bad == "":
                self.assertEqual(rep.refused, [])
                Contact.objects.all().delete()
            else:
                self.assertEqual(len(rep.refused), 1, bad)

    def test_an_address_that_is_not_an_object_is_refused(self):
        rep = self.run_it([contact("c9", address="Main Street 1, Town")])
        self.assertEqual(len(rep.refused), 1)

    def test_the_exporters_fixture_goes_through_whole(self):
        """M3's output on its fixtures, piped as it is (spec 5.1): the stored address is complete."""
        with open(FIXTURE, encoding="utf-8") as f:
            lines = f.read().split("\n")
        rep = importer.run(lines, source="zaprite", org=self.lib, dry_run=False)
        self.assertEqual([r[1] for r in rep.refused], ["zaprite:od_promo"])  # the one the totals check names
        jan = find_contact(self.lib, "jan.fixture@example.test")
        self.assertEqual(jan.address, {"line1": "Fixture Street 1", "city": "Bratislava", "zip": "81101",
                                       "country": "SK"})
        inv = Invoice.objects.get(external_reference="zaprite:od_inv1")
        self.assertEqual(inv.snapshot["buyer"]["address_lines"], ["Fixture Street 1", "81101 Bratislava", "SK"])
        derived = Invoice.objects.get(external_reference="zaprite:od_cust")
        self.assertEqual(derived.snapshot["buyer"]["address_lines"],
                         ["Order Street 5", "Floor 2", "11000 Praha", "CZ"])
        api = Invoice.objects.get(external_reference="zaprite:od_apiorder")
        self.assertEqual(api.kind, "payment_request")
        self.assertEqual(api.snapshot["buyer"]["address_lines"], ["Order Street 5", "Floor 2", "11000 Praha", "CZ"])


class NewlineOnlyTests(Base):
    SEPARATORS = (" ", " ", "\u0085")

    def call(self, records):
        out = io.StringIO()
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        with mock.patch("sys.stdin", io.StringIO(text)):
            with redirect_stdout(out):
                call_command("import_invoices", "--source", "zaprite", "--org", "liberation", stdout=out)
        return out.getvalue()

    def test_a_unicode_separator_inside_text_does_not_split_a_record(self):
        for k, sep in enumerate(self.SEPARATORS):
            recs = [contact(f"c{k}", f"u{k}@example.com", legal_name=f"Name{sep}Surname",
                            address={"line1": f"Street{sep}1", "country": "SK"}),
                    invoice(f"od_{k}", f"c{k}", str(k + 1), title=f"Title{sep}Two",
                            lines=[{"name": f"Line{sep}name", "quantity": 1, "unit_price_minor": 133700}])]
            out = self.call(recs)
            self.assertIn("refused lines: 0", out, repr(sep))
            inv = Invoice.objects.get(external_reference=f"zaprite:od_{k}")
            self.assertEqual(inv.title, f"Title{sep}Two")
            self.assertEqual(inv.contact.legal_name, f"Name{sep}Surname")
            self.assertEqual(inv.contact.address["line1"], f"Street{sep}1")

    def test_btc_check_does_not_split_either(self):
        recs = [contact("c1", legal_name="A B"),
                invoice("od_btc", "c1", "1", currency="BTC", total=5, title="x y",
                        lines=[{"name": "n", "quantity": 1, "unit_price_minor": 5}])]
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(text)):
            call_command("import_invoices", "--source", "zaprite", "--org", "liberation", "--dry-run",
                         "--btc-check", "od_btc", stdout=out)
        self.assertIn("btc-check od_btc sats=5", out.getvalue())

    def test_line_numbers_do_not_shift(self):
        recs = [contact("c1", legal_name="A B"), invoice("od_1", "c1", "1", total=1), invoice("od_2", "c1", "2")]
        recs[1]["lines"][0]["unit_price_minor"] = 2  # wrong total, line 2
        out = self.call(recs)
        self.assertIn("line 2 zaprite:od_1", out)


class RefusalsNameTheRecordTests(Base):
    def test_each_kind_of_refusal_is_named_by_source_and_id(self):
        recs = [contact("c1"),
                invoice("od_total", "c1", "1", total=5),  # lines do not add up (line price stays 5? fixed below)
                invoice("od_cur", "c1", "2", currency="XXX"),
                invoice("od_nocontact", "ghost", "3"),
                invoice("od_name", "c1", "4", lines=[{"name": "x" * 501, "quantity": 1, "unit_price_minor": 133700}]),
                invoice("od_nodate", "c1", None, kind="payment_request", issue_date=None)]
        recs[1]["lines"][0]["unit_price_minor"] = 6
        rep = self.run_it(recs)
        self.assertEqual({(n, ref) for n, ref, _ in rep.refused},
                         {(2, "zaprite:od_total"), (3, "zaprite:od_cur"), (4, "zaprite:od_nocontact"),
                          (5, "zaprite:od_name"), (6, "zaprite:od_nodate")})

    def test_a_refused_contact_is_named_too(self):
        rep = self.run_it([contact("bad", address={"zipCode": "1"})])
        self.assertEqual([(n, ref) for n, ref, _ in rep.refused], [(1, "zaprite:bad")])

    def test_a_line_that_is_not_json_has_no_id_to_name(self):
        rep = importer.run(["{nope"], source="zaprite", org=self.lib, dry_run=True)
        self.assertEqual(rep.refused[0][0], 1)
        self.assertIsNone(rep.refused[0][1])

    def test_the_command_prints_them_and_skips_by_status_and_existing(self):
        recs = [contact("c1"), invoice("rf_draft", "c1", "1", source_status="draft"), invoice("rf_ok", "c1", "2"),
                invoice("rf_bad", "ghost", "3")]
        out = io.StringIO()
        for _ in range(2):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO("\n".join(ndjson(*recs)))):
                call_command("import_invoices", "--source", "rf", "--org", "liberation", stdout=out)
        text = out.getvalue()
        self.assertIn("line 4 rf:rf_bad", text)
        self.assertIn("skipped by source status draft: 1", text)
        self.assertIn("rf:rf_draft", text)
        self.assertIn("rf:rf_ok", text)  # skipped, already imported


class UsFlagFollowsTheOrganizationTests(Base):
    def test_a_us_issuer_org_flags_every_contact_yes_for_rf(self):
        self.run_it([contact("r1", "us@x.com"), contact("r2", None), invoice("rf_1", "r1", "1")],
                    source="rf", org=self.sey)
        self.assertEqual(set(Contact.objects.values_list("us_tax_resident", flat=True)), {"yes"})

    def test_the_slug_does_not_decide(self):
        other = Organization.objects.create(slug="somewhere", legal_name="S", is_us_resident_issuer=False)
        self.sey.is_us_resident_issuer = False
        self.sey.save()
        other.is_us_resident_issuer = True
        other.save()
        self.run_it([contact("r1", "a@x.com"), invoice("rf_1", "r1", "1")], source="rf", org=self.sey)
        self.assertEqual(Contact.objects.get().us_tax_resident, "unknown")
        self.run_it([contact("r2", "b@x.com"), invoice("rf_2", "r2", "1")], source="rf", org=other)
        self.assertEqual(Contact.objects.get(org=other).us_tax_resident, "yes")

    def test_a_non_us_org_never_flags_yes(self):
        self.run_it([contact("r1", "a@x.com"), invoice("rf_1", "r1", "1")], source="rf", org=self.lib)
        self.assertEqual(Contact.objects.get().us_tax_resident, "unknown")

    def test_zaprite_into_a_us_issuer_org_is_refused_without_the_flag(self):
        with self.assertRaises(importer.UsIssuerRefused):
            self.run_it([contact("c1"), invoice("od_1", "c1")], source="zaprite", org=self.sey)
        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_the_command_refuses_and_writes_nothing(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("\n".join(ndjson(contact("c1"), invoice("od_1", "c1"))))):
            with self.assertRaises(Exception) as cm:
                call_command("import_invoices", "--source", "zaprite", "--org", "seychelles", stdout=out)
        self.assertIn("--allow-us-issuer", str(cm.exception))
        self.assertEqual(Invoice.objects.count(), 0)

    def test_allow_us_issuer_lets_a_single_zaprite_record_in_flagged_yes(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("\n".join(ndjson(contact("c1"), invoice("od_1", "c1"))))):
            call_command("import_invoices", "--source", "zaprite", "--org", "seychelles", "--allow-us-issuer",
                         stdout=out)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Contact.objects.get().us_tax_resident, "yes")


class MigratedCurrencyTests(Base):
    """Owner 2026-11-09: Request Finance invoices in USDT, ETH and the like come over in their own
    currency. Those currencies exist for carried-over history only: nothing new may be issued in them."""

    def test_an_rf_usdt_invoice_is_imported_in_usdt(self):
        rep = self.run_it([contact("c1"), invoice("rf1", "c1", "600", currency="USDT", total=1337000000,
                                                  source_status="open")], source="rf")
        self.assertEqual(rep.refused, [])
        inv = Invoice.objects.get(external_reference="rf:rf1")
        self.assertEqual((inv.currency, inv.status), ("USDT", "paid"))
        self.assertEqual(Payment.objects.get(invoice=inv).amount_minor, 1337000000)

    def test_every_carried_over_currency_has_its_exponent(self):
        from apps.invoices.currencies import CURRENCIES, MIGRATED_ONLY, minor_units
        self.assertEqual((minor_units("USDT"), minor_units("usdc"), minor_units("ETH"), minor_units("SOL")), (6, 6, 8, 9))
        self.assertFalse(set(MIGRATED_ONLY) & set(CURRENCIES))

    def test_nothing_new_is_issued_in_them(self):
        from apps.contacts.services import find_or_create_contact
        from apps.invoices.currencies import check_issuable
        from apps.invoices.services import create_invoice
        with self.assertRaises(ValueError):
            check_issuable("USDT")
        check_issuable("EUR")
        c, _ = find_or_create_contact(self.lib, "x@example.com", legal_name="X")
        with self.assertRaises(ValueError):
            create_invoice(self.lib, c, currency="USDT", lines=[{"name": "S", "quantity": 1, "unit_price_minor": 1}])
