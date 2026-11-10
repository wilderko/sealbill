"""Invoices carried over from Zaprite / Request Finance are history: the staff page says so and locks them."""
import datetime

from apps.exports import csv as exports_csv
from apps.invoices.migration import is_migrated, migration_info, staff_note
from apps.invoices.models import Invoice
from apps.invoices.services import send_invoice
from apps.notify.models import OutboxMessage
from apps.payments.models import Payment
from apps.payments.services import record_manual_payment
from apps.staff.tests_invoicing import InvoicingBase
from apps.api.models import ApiToken
from apps.webhooks.models import WebhookEvent
from apps.quotes.models import Quote
from rest_framework.test import APIClient
from django.db import connection

INFO = {"source": "zaprite", "source_id": "od_1", "source_number": "2026-017", "source_status": "PROCESSING",
        "source_url": "https://app.zaprite.com/o/od_1", "migrated_at": "2026-11-06T10:00:00+00:00"}


class NoteTests(InvoicingBase):
    def check(self, language, texts):
        zap, rf_n, zap_req, rf = texts
        with self.settings(MIGRATION_NOTE_LANGUAGE=language):
            self.assertEqual(staff_note(INFO), zap)
            self.assertEqual(staff_note({**INFO, "source": "rf", "source_number": "RF-9"}), rf_n)
            self.assertEqual(staff_note({**INFO, "source_number": ""}), zap_req)
            self.assertEqual(staff_note({**INFO, "source_number": None}), zap_req)
            for number in (None, ""):
                note = staff_note({**INFO, "source": "rf", "source_number": number})
                self.assertEqual(note, rf)
                self.assertNotIn("None", note)

    def test_the_exact_english_strings(self):
        self.check("en", ("Paid invoice migrated from Zaprite (original number 2026-017).",
                          "Paid invoice migrated from Request Finance (original number RF-9).",
                          "Paid payment request migrated from Zaprite.",
                          "Paid invoice migrated from Request Finance."))

    def test_the_exact_slovak_strings(self):
        self.check("sk", ("Zaplatená faktúra prenesená zo Zaprite (pôvodné číslo 2026-017).",
                          "Zaplatená faktúra prenesená z Request Finance (pôvodné číslo RF-9).",
                          "Zaplatená platobná požiadavka prenesená zo Zaprite.",
                          "Zaplatená faktúra prenesená z Request Finance."))

    def test_the_default_is_english_and_an_unknown_language_falls_back(self):
        self.assertIn("migrated from Zaprite", staff_note(INFO))
        with self.settings(MIGRATION_NOTE_LANGUAGE="xx"):
            self.assertIn("migrated from Zaprite", staff_note(INFO))

    def test_reading_the_marker(self):
        inv = self.draft()
        self.assertFalse(is_migrated(inv))
        self.assertIsNone(migration_info(inv))
        inv.metadata = {"migration": INFO}
        self.assertTrue(is_migrated(inv))
        self.assertEqual(migration_info(inv)["source_id"], "od_1")
        inv.metadata = {"other": 1}
        self.assertFalse(is_migrated(inv))


class MigratedBase(InvoicingBase):
    def migrated(self):
        """What the importer leaves: a paid invoice with one confirmed manual payment and the marker."""
        inv = self.draft()
        inv, _v, _p = send_invoice(inv, email_client=False)
        record_manual_payment(inv, amount_minor=133700, rail="manual", note="migrated", user=self.staff)
        Invoice.objects.filter(pk=inv.pk).update(metadata={"migration": INFO})
        return Invoice.objects.get(pk=inv.pk)

    def break_metadata(self, inv, raw="gAAAAABnotavalidtoken"):
        with connection.cursor() as cur:
            cur.execute("UPDATE invoices_invoice SET metadata = %s WHERE id = %s", [raw, inv.pk])


class DetailTests(MigratedBase):
    def test_banner_and_no_actions(self):
        inv = self.migrated()
        html = self.client.get(f"/invoices/{inv.pk}/").content.decode()
        self.assertIn("Paid invoice migrated from Zaprite (original number 2026-017).", html)
        with self.settings(MIGRATION_NOTE_LANGUAGE="sk"):
            sk = self.client.get(f"/invoices/{inv.pk}/").content.decode()
        self.assertIn("Zaplatená faktúra prenesená zo Zaprite (pôvodné číslo 2026-017).", sk)
        self.assertIn("PROCESSING", html)
        self.assertIn('href="https://app.zaprite.com/o/od_1"', html)
        for gone in ("Record payment", "Void invoice", "Reissue the client links", f"/invoices/{inv.pk}/send/",
                     f"/invoices/{inv.pk}/payments/", f"/invoices/{inv.pk}/void/", f"/invoices/{inv.pk}/delete/"):
            self.assertNotIn(gone, html)

    def test_a_normal_invoice_has_no_banner_and_keeps_actions(self):
        inv, _v, _p = send_invoice(self.draft(), email_client=False)
        html = self.client.get(f"/invoices/{inv.pk}/").content.decode()
        self.assertNotIn("migrated from", html)
        self.assertIn("Record payment", html)
        self.assertIn("Void invoice", html)

    def test_unsafe_source_urls_are_not_linked(self):
        inv = self.migrated()
        for url in ("javascript:alert(1)", "data:text/html,x", "vbscript:msgbox(1)", "ftp://x.example/a"):
            Invoice.objects.filter(pk=inv.pk).update(metadata={"migration": {**INFO, "source_url": url}})
            html = self.client.get(f"/invoices/{inv.pk}/").content.decode()
            self.assertIn("migrated from", html)
            self.assertNotIn(f'href="{url}', html, url)

    def test_the_reverse_form_is_hidden(self):
        inv = self.migrated()
        pay = inv.payments.get()
        html = self.client.get(f"/invoices/{inv.pk}/").content.decode()
        self.assertNotIn(f"/payments/{pay.pk}/reverse/", html)

    def test_a_value_that_cannot_be_decrypted_renders_the_page_locked(self):
        inv = self.migrated()
        self.break_metadata(inv)
        r = self.client.get(f"/invoices/{inv.pk}/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("cannot be read", html)
        self.assertNotIn("Record payment", html)
        self.assertNotIn("/reverse/", html)

    def test_the_sealed_marker_renders_the_page_locked(self):
        inv = self.migrated()
        Invoice.objects.filter(pk=inv.pk).update(metadata={"_encrypted": True})
        html = self.client.get(f"/invoices/{inv.pk}/").content.decode()
        self.assertIn("cannot be read", html)
        self.assertNotIn("Void invoice", html)


class LockedActionTests(MigratedBase):
    def test_actions_are_refused_with_409_and_change_nothing(self):
        inv = self.migrated()
        before_msgs = OutboxMessage.objects.count()
        before_pay = Payment.objects.count()
        posts = [
            (f"/invoices/{inv.pk}/send/", {"email_client": "on"}),
            (f"/invoices/{inv.pk}/payments/", {"amount": "1.00", "rail": "manual", "note": "x"}),
            (f"/invoices/{inv.pk}/void/", {"reason": "x"}),
            (f"/invoices/{inv.pk}/delete/", {}),
            (f"/invoices/{inv.pk}/links/", {"email_client": "on"}),
        ]
        for url, data in posts:
            self.assertEqual(self.client.post(url, data).status_code, 409, url)
        self.assertEqual(self.client.get(f"/invoices/{inv.pk}/links/").status_code, 409)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "paid")
        self.assertTrue(Invoice.objects.filter(pk=inv.pk).exists())
        self.assertEqual(OutboxMessage.objects.count(), before_msgs)
        self.assertEqual(Payment.objects.count(), before_pay)


class CsvTests(MigratedBase):
    def test_migrated_from_column(self):
        inv = self.migrated()
        normal, _v, _p = send_invoice(self.draft(), email_client=False)
        day = datetime.date(2000, 1, 1)
        Invoice.objects.filter(pk__in=[inv.pk, normal.pk]).update(issue_date=day)
        header, *rows = list(exports_csv.invoices_rows([self.org.pk], day, day))
        self.assertIn("migrated_from", header)
        col, num = header.index("migrated_from"), header.index("number")
        by_number = {r[num]: r[col] for r in rows}
        self.assertEqual(by_number[Invoice.objects.get(pk=inv.pk).number], "zaprite:PROCESSING")
        self.assertEqual(by_number[Invoice.objects.get(pk=normal.pk).number], "")


class StaffPaymentLockTests(MigratedBase):
    def test_reverse_is_refused_and_changes_nothing(self):
        inv = self.migrated()
        pay = inv.payments.get()
        events = WebhookEvent.objects.count()
        r = self.client.post(f"/payments/{pay.pk}/reverse/", {"reason": "mistake"})
        self.assertEqual(r.status_code, 409)
        pay.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual((pay.state, inv.status), ("confirmed", "paid"))
        self.assertEqual(WebhookEvent.objects.count(), events)

    def test_confirm_reject_refund_keep_are_refused(self):
        inv = self.migrated()
        pay = inv.payments.get()
        for action, data in (("confirm", {"note": "x"}), ("reject", {"note": "x"}), ("refund", {"note": "x"}),
                             ("gateway-invalid", {"note": "x"})):
            r = self.client.post(f"/payments/{pay.pk}/{action}/", data)
            self.assertIn(r.status_code, (404, 409), action)
            if r.status_code == 404:
                self.fail(f"no such staff url {action}")
        pay.refresh_from_db()
        self.assertEqual(pay.state, "confirmed")

    def test_dismiss_a_declaration_is_refused(self):
        from apps.payments.models import Declaration
        inv = self.migrated()
        decl = Declaration.objects.create(invoice=inv, network="arbitrum", state="open")
        r = self.client.post(f"/declarations/{decl.pk}/dismiss/", {"reason": "x"})
        self.assertEqual(r.status_code, 409)
        decl.refresh_from_db()
        self.assertEqual(decl.state, "open")

    def test_assigning_a_payment_to_a_migrated_invoice_is_refused(self):
        inv = self.migrated()
        stray = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=0,
                                       received_amount="1", tx_hash="0x" + "11" * 32, state="needs_review",
                                       review_reason="no_matching_invoice", amount_base_units="1000000",
                                       final=True)
        r = self.client.post(f"/payments/{stray.pk}/assign/",
                             {"invoice": str(inv.pk), "amount": "1.00", "note": "x"})
        self.assertEqual(r.status_code, 409)
        stray.refresh_from_db()
        self.assertEqual((stray.state, stray.invoice_id), ("needs_review", None))

    def test_unreadable_metadata_refuses_the_mutation(self):
        inv = self.migrated()
        self.break_metadata(inv)
        pay = Payment.objects.get(invoice_id=inv.pk)
        for url, data in ((f"/invoices/{inv.pk}/void/", {"reason": "x"}),
                          (f"/payments/{pay.pk}/reverse/", {"reason": "x"}),
                          (f"/invoices/{inv.pk}/payments/", {"amount": "1", "rail": "manual", "note": "x"})):
            r = self.client.post(url, data)
            self.assertEqual(r.status_code, 409, url)
            self.assertIn("cannot be read", r.content.decode(), url)
        self.assertEqual(Payment.objects.get(pk=pay.pk).state, "confirmed")

    def test_sealed_marker_refuses_the_mutation(self):
        inv = self.migrated()
        Invoice.objects.filter(pk=inv.pk).update(metadata={"_encrypted": True})
        r = self.client.post(f"/invoices/{inv.pk}/void/", {"reason": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Unlock the vault", r.content.decode())


class ApiLockTests(MigratedBase):
    def api(self):
        token, plain = ApiToken.issue(self.org, "t", ["read", "invoices:write", "payments:confirm"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c

    def counts(self):
        return (OutboxMessage.objects.count(), WebhookEvent.objects.count(), Quote.objects.count(),
                Payment.objects.count())

    def refused(self, r):
        self.assertEqual(r.status_code, 409, r.content[:300])
        self.assertEqual(r.json()["error"], "invoice_migrated")

    def test_every_invoice_mutation_is_refused_and_changes_nothing(self):
        inv = self.migrated()
        c = self.api()
        before = self.counts()
        base = f"/api/v1/invoices/{inv.pk}"
        self.refused(c.post(f"{base}/send", {}, format="json"))
        self.refused(c.post(f"{base}/reissue-links", {}, format="json"))
        self.refused(c.post(f"{base}/void", {"reason": "x"}, format="json"))
        self.refused(c.post(f"{base}/payments", {"amount_minor": 100, "rail": "manual", "note": "x"}, format="json"))
        self.refused(c.post(f"{base}/quotes", {"method": "bank", "network": "sepa", "asset": "EUR"}, format="json"))
        self.assertEqual(self.counts(), before)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "paid")

    def test_each_endpoint_alone(self):
        inv = self.migrated()
        c = self.api()
        for tail, body in (("send", {}), ("reissue-links", {}), ("void", {"reason": "x"}),
                           ("payments", {"amount_minor": 100, "rail": "manual", "note": "x"}),
                           ("quotes", {"method": "bank", "network": "sepa", "asset": "EUR"})):
            with self.subTest(tail):
                self.refused(c.post(f"/api/v1/invoices/{inv.pk}/{tail}", body, format="json"))

    def test_reads_stay_allowed(self):
        inv = self.migrated()
        c = self.api()
        self.assertEqual(c.get(f"/api/v1/invoices/{inv.pk}").status_code, 200)
        self.assertEqual(c.get(f"/api/v1/invoices/{inv.pk}/quotes").status_code, 200)

    def test_payment_actions_are_refused(self):
        inv = self.migrated()
        pay = inv.payments.get()
        c = self.api()
        for tail, body in (("reverse", {"reason": "x"}), ("confirm", {"note": "x"}), ("reject", {"note": "x"}),
                           ("acknowledge-refund", {"note": "x"})):
            with self.subTest(tail):
                self.refused(c.post(f"/api/v1/payments/{pay.pk}/{tail}", body, format="json"))
        pay.refresh_from_db()
        self.assertEqual(pay.state, "confirmed")

    def test_unreadable_metadata_is_refused_too(self):
        inv = self.migrated()
        self.break_metadata(inv)
        c = self.api()
        self.refused(c.post(f"/api/v1/invoices/{inv.pk}/void", {"reason": "x"}, format="json"))
