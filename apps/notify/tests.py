import email
import io
from email.header import decode_header, make_header
import datetime
import time
from unittest import mock

from django.db import connection
from django.utils import timezone

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import translation

from apps.contacts.services import find_or_create_contact
from apps.invoices.errors import InvoiceError
from apps.invoices.services import create_invoice, send_invoice
from apps.notify import services
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import VaultSealed, vault

LINE = {"name": "Service", "quantity": 1, "unit_price_minor": 133700}


class OutboxTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", email_from="billing@seller.example")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(
            self.org, "client@example.com", legal_name="Ján Žilinský", preferred_language="sk")

    def sent_invoice(self, contact=None, **kw):
        inv = create_invoice(self.org, contact or self.contact, currency="EUR", lines=[LINE], **kw)[0]
        return send_invoice(inv)

    def test_sealed_vault_keeps_mail_queued_then_sends_after_unlock(self):
        self.sent_invoice()
        from apps.vault.testing import TEST_VAULT_PASSWORD

        vault.seal()
        self.assertEqual(services.flush_outbox(), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OutboxMessage.objects.values_list("state", flat=True).get(), "queued")
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(services.flush_outbox(), 1)
        self.assertEqual(len(mail.outbox), 1)
        m = OutboxMessage.objects.get()
        self.assertEqual((m.state, bool(m.sent_at)), ("sent", True))

    def test_long_non_ascii_subject_decodes_without_a_leading_space(self):
        # Django folds a non-ASCII Subject without knowing "Subject: " is in front of it, leaving the
        # first line empty: the client then reads the subject with a leading space.
        from email import policy

        long_name = "Žilinská účtovnícka a poradenská spoločnosť pre malé a stredné podniky, s.r.o., Košice-Juh"
        Organization.objects.filter(pk=self.org.pk).update(legal_name=long_name)
        self.org.refresh_from_db()
        inv = self.sent_invoice()[0]
        services.flush_outbox()
        wire = mail.outbox[0].message().as_bytes(linesep="\r\n")
        self.assertNotIn(b"Subject: \r\n", wire)
        parsed = email.message_from_bytes(wire, policy=policy.default)
        self.assertEqual(parsed["Subject"], f"Faktúra {inv.number} od {long_name}")

    def test_invoice_mail_in_contact_language_with_pdf_and_links(self):
        inv, view_plain, pay_plain = self.sent_invoice()
        services.flush_outbox()
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["client@example.com"])
        self.assertEqual(msg.from_email, "billing@seller.example")
        self.assertEqual(msg.reply_to, ["billing@seller.example"])
        self.assertIn(view_plain, msg.body)
        self.assertIn(pay_plain, msg.body)
        self.assertIn("Ján Žilinský", msg.body)
        self.assertNotIn("Dear", msg.body)  # Slovak, not English
        html = msg.alternatives[0][0]
        self.assertIn(f'href="{inv.view_url(view_plain)}"', html)
        self.assertEqual(msg.attachments[0][2], "application/pdf")
        self.assertTrue(msg.attachments[0][1].startswith(b"%PDF"))
        self.assertIn(inv.number, msg.subject)
        self.assertNotIn("Invoice", msg.subject)

    def test_korean_and_slovak_names_in_subject_are_encoded(self):
        for org_name, lang in (("주식회사 리버레이션", "ko"), ("Žilinská účtáreň s.r.o.", "sk")):
            OutboxMessage.objects.all().delete()
            mail.outbox.clear()
            org = Organization.objects.create(slug=f"o-{lang}", legal_name=org_name, default_language=lang)
            add_bank(org)
            c, _ = find_or_create_contact(org, f"{lang}@example.com", legal_name="X")
            inv = create_invoice(org, c, currency="EUR", lines=[LINE])[0]
            inv, *_ = send_invoice(inv)
            with translation.override(lang):  # read before the flush: a sent row keeps no subject (DATA-4)
                expected = services._subject(OutboxMessage.objects.get())
            self.assertEqual(services.flush_outbox(), 1)
            raw = mail.outbox[0].message().as_bytes()
            subject = str(make_header(decode_header(email.message_from_bytes(raw)["Subject"])))
            self.assertEqual(subject, expected)
            self.assertIn(org_name, subject)
            self.assertNotIn("\n", subject)

    def test_failed_send_retries_with_backoff_for_about_a_day_then_marks_failed(self):
        from apps.audit.models import AuditEntry

        self.sent_invoice()
        schedule = (2, 5, 15, 30, 60, 120, 240, 480)  # minutes; about sixteen hours in all
        with mock.patch("apps.notify.services.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            with self.assertLogs("apps.notify.services", "ERROR") as logs:
                for expected, minutes in enumerate(schedule, start=1):
                    OutboxMessage.objects.update(next_attempt_at=None)
                    before = timezone.now()
                    self.assertEqual(services.flush_outbox(), 0)
                    m = OutboxMessage.objects.get()
                    self.assertEqual((m.attempts, m.state), (expected, "queued"))
                    self.assertAlmostEqual(
                        (m.next_attempt_at - before).total_seconds(), minutes * 60, delta=5)
                    self.assertEqual(services.flush_outbox(), 0)  # not due yet: not attempted
                    self.assertEqual(OutboxMessage.objects.get().attempts, expected)
                self.assertFalse(AuditEntry.objects.filter(action="outbox.failed").exists())
                OutboxMessage.objects.update(next_attempt_at=None)
                self.assertEqual(services.flush_outbox(), 0)
        m = OutboxMessage.objects.get()
        self.assertEqual((m.attempts, m.state, m.next_attempt_at), (9, "failed", None))
        self.assertTrue(any("OSError" in line for line in logs.output))
        self.assertFalse(any("client@example.com" in line or "smtp down" in line for line in logs.output))
        self.assertEqual(services.flush_outbox(), 0)  # failed messages are left alone
        entry = AuditEntry.objects.get(action="outbox.failed")
        self.assertEqual((entry.model_label, entry.object_pk, entry.actor), ("notify.outboxmessage", str(m.pk), None))
        self.assertEqual(entry.detail["kind"], "invoice")
        self.assertNotIn("client@example.com", str(entry.detail))

    def test_undecryptable_message_fails_and_the_next_one_still_goes(self):
        self.sent_invoice()
        bad = OutboxMessage.objects.get()
        with connection.cursor() as cur:  # bypass the field: store a value no key opens
            cur.execute("UPDATE notify_outboxmessage SET \"to\" = %s WHERE id = %s", ["gAAAAAbogus", bad.pk])
        services.enqueue("vault_sealed_staff", to=["ops@example.com"], language="en",
                         context={"count": 1, "unlock_url": "https://x/unlock/"})
        with self.assertLogs("apps.notify.services", "ERROR"), self.assertLogs("apps.vault.fields", "ERROR"):
            self.assertEqual(services.flush_outbox(), 1)
        states = dict(OutboxMessage.objects.values_list("pk", "state"))
        self.assertEqual(states[bad.pk], "failed")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])

    def test_sealed_mid_loop_stops_without_counting_an_attempt(self):
        self.sent_invoice()
        with mock.patch("apps.notify.services._build", side_effect=VaultSealed("sealed")):
            self.assertEqual(services.flush_outbox(), 0)
        m = OutboxMessage.objects.get()
        self.assertEqual((m.attempts, m.state), (0, "queued"))

    def test_lock_is_taken_on_the_outbox_row_only(self):
        self.sent_invoice()
        from django.db.models.query import QuerySet

        real = QuerySet.select_for_update
        seen = []

        def spy(qs, *a, **kw):
            seen.append(kw)
            return real(qs, *a, **kw)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            services.flush_outbox()
        self.assertEqual(seen, [{"skip_locked": True, "of": ("self",)}])

    def test_postgres_locks_only_the_outbox_table(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL only")
        from django.test.utils import CaptureQueriesContext

        self.sent_invoice()
        with CaptureQueriesContext(connection) as ctx:
            services.flush_outbox()
        locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(locks)
        self.assertTrue(all('FOR UPDATE OF "notify_outboxmessage"' in q and "JOIN" not in q for q in locks))

    def test_payment_request_mail_says_payment_request(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], kind="payment_request")[0]
        send_invoice(inv)
        services.flush_outbox()
        self.assertIn("Žiadosť o platbu je priložená", mail.outbox[0].body)
        self.assertNotIn("Faktúra je priložená", mail.outbox[0].body)

    def test_send_invoice_with_email_client_false_enqueues_nothing(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        send_invoice(inv, email_client=False)
        self.assertEqual(OutboxMessage.objects.count(), 0)

    def test_send_invoice_without_any_address_refuses_before_sending(self):
        self.contact.emails.all().delete()
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        with self.assertRaises(InvoiceError):
            send_invoice(inv)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "draft")

    @override_settings(EMAIL_ATTACHMENT_BUDGET_BYTES=10)
    def test_oversized_pdf_is_left_out_with_a_line_in_the_body(self):
        self.sent_invoice()
        services.flush_outbox()
        msg = mail.outbox[0]
        self.assertEqual(msg.attachments, [])
        self.assertIn("PDF sa nepodarilo priložiť", msg.body)  # the Slovak omission sentence

    def test_staff_kind_cannot_be_addressed_to_the_contact(self):
        # A staff letter is never encrypted, so addressed to the client it would reach them in
        # plaintext, whatever key they hold, and say what only staff should read.
        for kind in sorted(services.STAFF_KINDS & set(services.CONTEXT_BUILDERS)):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                services.enqueue_addressed_later(kind, recipient="contact", contact=self.contact,
                                                 plain_context={"payment_id": 1})
        self.assertEqual(OutboxMessage.objects.count(), 0)
        services.enqueue_addressed_later("review_staff", recipient="staff", plain_context={"payment_id": 1})
        self.assertEqual(OutboxMessage.objects.count(), 1)

    def test_staff_kinds_and_oldest_first(self):
        services.enqueue("vault_sealed_staff", to=["ops@example.com"], language="en",
                         context={"count": 3, "unlock_url": "https://pay.example/unlock/"})
        services.enqueue("declared_staff", to=["ops@example.com"], language="en",
                         context={"ref": "2026-0001", "name": "A", "total": "1 EUR", "admin_url": "https://x/"})
        self.assertEqual(services.flush_outbox(limit=1), 1)
        self.assertIn("vault is sealed", mail.outbox[0].subject)
        self.assertEqual(services.flush_outbox(), 1)
        self.assertEqual(mail.outbox[1].subject, "Payment declared for 2026-0001")


class OutboxRecheckTests(UnsealedVaultMixin, TestCase):
    """M12: what was true when the e-mail was queued is checked again when it is sent."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.staff = get_user_model().objects.create_user("clerk", is_staff=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "client@example.com", legal_name="A")
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]
        self.invoice, *_ = send_invoice(inv)

    def test_invoice_mail_of_a_voided_invoice_is_cancelled(self):
        from apps.invoices.services import void_invoice

        void_invoice(self.invoice, "duplicate", self.staff)
        self.assertEqual(services.flush_outbox(), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OutboxMessage.objects.get(kind="invoice").state, "cancelled")

    def test_receipt_of_an_invoice_no_longer_paid_is_cancelled(self):
        from apps.payments.services import record_manual_payment, reverse_payment

        OutboxMessage.objects.all().delete()
        p = record_manual_payment(self.invoice, amount_minor=133700, rail="bank", note="", user=self.staff)
        reverse_payment(p, self.staff, "bounced")
        self.assertEqual(services.flush_outbox(), 0)
        self.assertEqual(OutboxMessage.objects.get(kind="receipt").state, "cancelled")
        record_manual_payment(self.invoice, amount_minor=133700, rail="bank", note="", user=self.staff)
        self.assertEqual(services.flush_outbox(), 1)  # paid again: this receipt goes
        self.assertEqual(OutboxMessage.objects.get(kind="receipt", state="sent").kind, "receipt")


class OutboxFailedAlertTests(UnsealedVaultMixin, TestCase):
    """I4: a message that reaches `failed` is announced to staff once, by direct SMTP."""

    def setUp(self):
        super().setUp()
        self.msg = services.enqueue("declared_staff", to=["client-secret@example.com"], language="en",
                                    context={"ref": "2026-0001", "name": "A", "total": "1 EUR", "admin_url": "x"})
        OutboxMessage.objects.filter(pk=self.msg.pk).update(state="failed", attempts=9)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
    def test_alert_once_per_failed_message_without_client_data(self):
        from apps.core import jobs

        jobs.outbox_failed_alert()
        jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com"])
        self.assertIn("https://pay.example/", m.body)
        self.assertIn("1", m.subject)
        self.assertNotIn("client-secret@example.com", m.body + m.subject)
        services.retry_now(OutboxMessage.objects.filter(pk=self.msg.pk))
        OutboxMessage.objects.filter(pk=self.msg.pk).update(state="failed")  # fails again
        jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 2)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_alert_says_why_and_names_the_key_only_when_a_key_failed(self):
        from apps.core import jobs

        def failed(last_error):
            m = services.enqueue("declared_staff", to=["a@example.com"], language="en",
                                 context={"ref": "2026-0002", "name": "A", "total": "1 EUR", "admin_url": "x"})
            OutboxMessage.objects.filter(pk=m.pk).update(state="failed", attempts=1, last_error=last_error)

        OutboxMessage.objects.filter(pk=self.msg.pk).update(last_error="EncryptionRefused(openssl_missing)")
        failed("UndecryptableValue")
        jobs.outbox_failed_alert()
        body = mail.outbox[0].body
        self.assertNotIn("encryption key", body)  # neither failure was the client's key
        self.assertIn("openssl", body)
        self.assertIn("vault key", body)
        self.assertNotIn("mail server", body)

        failed("EncryptionRefused(expired)")
        failed("EncryptionRefused(key_failed)")
        failed("OSError")
        jobs.outbox_failed_alert()
        body = mail.outbox[1].body
        self.assertIn("2 were refused because the contact's encryption key stopped working", body)
        self.assertIn("mail server", body)
        self.assertNotIn("openssl", body)  # only the new failures are reported
        self.assertNotIn("vault key", body)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_failed_alert_does_not_swallow_a_later_failure(self):
        from apps.core import jobs

        OutboxMessage.objects.filter(pk=self.msg.pk).update(failed_at=timezone.now())

        def send_while_retried(mail_self, *a, **kw):
            # While the alert is going out, staff retry the row and it fails again.
            services.retry_now(OutboxMessage.objects.filter(pk=self.msg.pk))
            OutboxMessage.objects.filter(pk=self.msg.pk).update(
                state="failed", failed_at=timezone.now() + datetime.timedelta(seconds=5))
            return 1

        with mock.patch("apps.core.jobs.EmailMultiAlternatives.send", autospec=True, side_effect=send_while_retried):
            jobs.outbox_failed_alert()
        self.assertIsNone(OutboxMessage.objects.get(pk=self.msg.pk).staff_alerted_at)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_an_alert_that_cannot_be_sent_is_tried_again(self):
        from apps.core import jobs

        with mock.patch("apps.core.jobs.EmailMultiAlternatives.send", side_effect=OSError("down")), \
                self.assertLogs("apps.core.jobs", "ERROR"):
            jobs.outbox_failed_alert()
        self.assertIsNone(OutboxMessage.objects.get(pk=self.msg.pk).staff_alerted_at)
        jobs.outbox_failed_alert()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_without_recipients_it_warns_once(self):
        from apps.core import jobs

        with self.assertLogs("apps.core.jobs", "WARNING") as logs:
            jobs.outbox_failed_alert()
            jobs.outbox_failed_alert()
        self.assertEqual(len(logs.output), 1)

    def test_retry_now_resets_attempts_and_state(self):
        services.retry_now(OutboxMessage.objects.all())
        m = OutboxMessage.objects.get()
        self.assertEqual((m.state, m.attempts, m.next_attempt_at, m.staff_alerted_at), ("queued", 0, None, None))


class OutboxAdminTests(UnsealedVaultMixin, TestCase):
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
        self.msg = services.enqueue("declared_staff", to=["hidden-recipient@example.com"], language="en",
                                    context={"ref": "2026-0001", "name": "A", "total": "1 EUR", "admin_url": "x"})
        OutboxMessage.objects.filter(pk=self.msg.pk).update(state="failed", attempts=9)

    def test_list_shows_delivery_state_but_no_recipient(self):
        r = self.client.get("/hidden-admin-xyz/notify/outboxmessage/")
        self.assertEqual(r.status_code, 200)
        for needle in ("declared_staff", "failed", "9"):
            self.assertContains(r, needle)
        self.assertNotContains(r, "hidden-recipient@example.com")

    def test_retry_now_action(self):
        from apps.audit.models import AuditEntry

        r = self.client.post("/hidden-admin-xyz/notify/outboxmessage/", {
            "action": "retry_now", "_selected_action": [self.msg.pk]})
        self.assertEqual(r.status_code, 302)
        m = OutboxMessage.objects.get()
        self.assertEqual((m.state, m.attempts), ("queued", 0))
        self.assertTrue(AuditEntry.objects.filter(action="outbox.retry", actor=self.admin).exists())
