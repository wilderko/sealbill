import datetime
import threading
from unittest import mock, skipUnless

from django.core import mail
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.models import Contact, ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.invoices import reminders
from apps.invoices.models import Invoice
from apps.invoices.money import totals
from apps.invoices.services import create_invoice, send_invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Payment
from apps.staff.tests import StaffBase
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault
from apps.webhooks.models import WebhookEvent

LINE = {"name": "Service", "quantity": 1, "unit_price_minor": 133700}


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "c@example.com", legal_name="Client",
                                                 preferred_language="en")

    def sent(self, age_days=31, contact=None, **send):
        inv = create_invoice(self.org, contact or self.contact, currency="EUR", lines=[LINE])[0]
        inv = send_invoice(inv, **send)[0]
        Invoice.objects.filter(pk=inv.pk).update(sent_at=timezone.now() - datetime.timedelta(days=age_days))
        OutboxMessage.objects.filter(kind="invoice").update(state="sent")  # out of the way of flush
        OutboxMessage.objects.exclude(kind="invoice").delete()
        return Invoice.objects.get(pk=inv.pk)

    def reminders(self):
        return OutboxMessage.objects.filter(kind="reminder")


@override_settings(INVOICE_REMINDER_DAYS=30)
class WhoGetsOneTests(Base):
    def test_a_sent_invoice_30_days_old_is_reminded(self):
        inv = self.sent()
        self.assertEqual(reminders.send_due_reminders(), 1)
        self.assertEqual(self.reminders().count(), 1)
        inv.refresh_from_db()
        self.assertIsNotNone(inv.reminder_sent_at)

    def test_overdue_and_partially_paid_are_reminded(self):
        a, b = self.sent(), self.sent()
        Invoice.objects.filter(pk=a.pk).update(status="overdue")
        Invoice.objects.filter(pk=b.pk).update(status="partially_paid")
        self.assertEqual(reminders.send_due_reminders(), 2)

    def test_other_statuses_are_not(self):
        for status in ("declared", "paid", "overpaid", "void", "draft"):
            Invoice.objects.filter(pk=self.sent().pk).update(status=status)
        self.assertEqual(reminders.send_due_reminders(), 0)

    def test_too_young_is_not(self):
        self.sent(age_days=29)
        self.assertEqual(reminders.send_due_reminders(), 0)

    def test_exactly_30_days_is_reminded(self):
        inv = self.sent(age_days=0)
        now = inv.sent_at + datetime.timedelta(days=30)
        self.assertEqual(reminders.send_due_reminders(now=now), 1)

    def test_an_invoice_the_caller_writes_about_is_not(self):
        self.sent(email_client=False, client_letters=False)
        self.assertEqual(reminders.send_due_reminders(), 0)

    def test_an_invoice_sent_without_the_e_mail_is_not(self):
        # Staff chose another channel (Signal, by hand): the system never mailed it, so it never nags.
        inv = self.sent(email_client=False)
        self.assertFalse(OutboxMessage.objects.filter(invoice=inv).exists())
        self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertEqual(reminders.remind(inv.pk, 30), False)
        self.assertFalse(self.reminders().exists())

    def test_a_mailed_invoice_counts_whatever_became_of_its_letter(self):
        inv = self.sent()
        OutboxMessage.objects.filter(invoice=inv, kind="invoice").update(state="cancelled")
        self.assertEqual(reminders.send_due_reminders(), 1)

    def test_a_migrated_invoice_is_not(self):
        inv = self.sent()
        Invoice.objects.filter(pk=inv.pk).update(metadata={"migration": {"source": "zaprite"}})
        self.assertEqual(reminders.send_due_reminders(), 0)

    def test_a_checkout_contact_is_never_mailed(self):
        buyer, _ = find_or_create_contact(self.org, "anyone@example.com", legal_name="Whoever")
        inv = self.sent(contact=buyer, age_days=40)
        Contact.objects.filter(pk=buyer.pk).update(of_record=False)
        ContactEmail.objects.filter(contact=buyer).update(of_record=False)
        self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertFalse(self.reminders().exists())

    def test_a_contact_without_an_address_is_not(self):
        contact = Contact.objects.create(org=self.org, legal_name="No mail")
        self.sent(contact=contact, email_client=False)
        Invoice.objects.update(sent_at=timezone.now() - datetime.timedelta(days=40))
        self.assertEqual(reminders.send_due_reminders(), 0)

    def test_a_sealed_vault_does_nothing_and_a_later_run_does(self):
        inv = self.sent()
        vault.seal()
        self.assertEqual(reminders.send_due_reminders(), 0)
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(reminders.send_due_reminders(), 1)
        self.assertIsNotNone(Invoice.objects.get(pk=inv.pk).reminder_sent_at)


@override_settings(INVOICE_REMINDER_DAYS=30)
class OneLetterTests(Base):
    def test_a_second_run_sends_nothing(self):
        self.sent()
        self.assertEqual(reminders.send_due_reminders(), 1)
        self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertEqual(self.reminders().count(), 1)

    def test_a_stale_candidate_is_refused_under_the_lock(self):
        inv = self.sent()
        self.assertTrue(reminders.remind(inv.pk, 30))
        self.assertFalse(reminders.remind(inv.pk, 30))  # what a concurrent run sees once the first commits
        self.assertEqual(self.reminders().count(), 1)

    def test_reminded_in_the_same_transaction_as_the_letter(self):
        inv = self.sent()
        with mock.patch("apps.notify.services.enqueue", side_effect=RuntimeError("secret client text")), \
                self.assertLogs("apps.invoices.reminders", "ERROR") as logs:
            self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertEqual(logs.output, [f"ERROR:apps.invoices.reminders:reminder for invoice #{inv.pk} failed (RuntimeError)"])
        self.assertIsNone(Invoice.objects.get(pk=inv.pk).reminder_sent_at)
        self.assertFalse(AuditEntry.objects.filter(action="invoice.reminder").exists())

    def test_audited_with_the_delay_in_days(self):
        inv = self.sent(age_days=35)
        reminders.send_due_reminders()
        entry = AuditEntry.objects.get(action="invoice.reminder", object_pk=str(inv.pk))
        self.assertEqual(entry.detail, {"days": 35})


class LockPathTests(Base):
    """What a second concurrent run sees, deterministically: it takes the row lock after the first run
    committed, so it finds the stamp. Runs on every database; the real two-thread race is below."""

    @override_settings(INVOICE_REMINDER_DAYS=30)
    def test_a_run_that_starts_from_a_stale_candidate_list_sends_nothing(self):
        inv = self.sent()
        stale = [inv.pk]
        self.assertEqual(reminders.send_due_reminders(), 1)
        with mock.patch.object(reminders, "_candidates", return_value=stale):
            self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertEqual(self.reminders().count(), 1)

    @override_settings(INVOICE_REMINDER_DAYS=30)
    def test_the_row_is_read_through_the_lock(self):
        inv = self.sent()
        with mock.patch.object(reminders, "lock_invoice", wraps=reminders.lock_invoice) as locked:
            reminders.remind(inv.pk, 30)
        locked.assert_called_once_with(inv.pk)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL only: needs real row locks (SQLite serialises writers)")
class ConcurrentRunTests(UnsealedVaultMixin, TransactionTestCase):
    def test_two_runs_at_once_send_one_letter(self):
        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(org)
        contact, _ = find_or_create_contact(org, "c@example.com", legal_name="Client")
        inv = send_invoice(create_invoice(org, contact, currency="EUR", lines=[LINE])[0])[0]
        Invoice.objects.filter(pk=inv.pk).update(sent_at=timezone.now() - datetime.timedelta(days=40))
        results = []

        def run():
            try:
                results.append(reminders.remind(inv.pk, 30))
            finally:
                connection.close()

        with override_settings(INVOICE_REMINDER_DAYS=30):
            threads = [threading.Thread(target=run) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(OutboxMessage.objects.filter(kind="reminder").count(), 1)


@override_settings(INVOICE_REMINDER_DAYS=30)
class LetterTests(Base):
    def test_links_are_reissued_and_the_webhook_fires(self):
        inv = self.sent()
        before = (inv.view_token_hash, inv.pay_token_hash)
        WebhookEvent.objects.all().delete()
        reminders.send_due_reminders()
        inv.refresh_from_db()
        self.assertNotEqual((inv.view_token_hash, inv.pay_token_hash), before)
        events = list(WebhookEvent.objects.filter(type="invoice.links_reissued"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, {"invoice_id": inv.pk, "status": "sent"})
        self.assertTrue(AuditEntry.objects.filter(action="invoice.reissue_links", object_pk=str(inv.pk)).exists())

    def test_the_letter_carries_the_new_links_and_what_is_still_due(self):
        inv = self.sent()
        reminders.send_due_reminders()
        notify.flush_outbox()
        (letter,) = [m for m in mail.outbox if "c@example.com" in m.to]
        inv.refresh_from_db()
        self.assertIn("/i/", letter.body)
        self.assertIn("/p/", letter.body)
        self.assertIn("1,337.00", letter.body)
        self.assertIn("already paid", letter.body)
        self.assertIn(inv.number, letter.subject)
        self.assertEqual(len(letter.attachments), 0)

    def test_the_amount_is_what_is_still_due(self):
        inv = self.sent()
        Payment.objects.create(invoice=inv, rail="bank", asset="EUR", amount_minor=33700, received_amount=33700, state="confirmed")
        Invoice.objects.filter(pk=inv.pk).update(status="partially_paid")
        reminders.send_due_reminders()
        notify.flush_outbox()
        (letter,) = [m for m in mail.outbox if "c@example.com" in m.to]
        self.assertIn("1,000.00", letter.body)
        self.assertNotIn("1,337.00", letter.body)

    def test_the_letter_is_in_the_contacts_language(self):
        contact, _ = find_or_create_contact(self.org, "k@example.com", legal_name="Klient", preferred_language="sk")
        self.sent(contact=contact)
        reminders.send_due_reminders()
        notify.flush_outbox()
        (letter,) = [m for m in mail.outbox if "k@example.com" in m.to]
        self.assertIn("Dovoľujeme si pripomenúť", letter.body)
        self.assertIn("Ak ste už zaplatili", letter.body)
        self.assertNotIn("friendly reminder", letter.body)

    def test_a_letter_whose_invoice_was_paid_meanwhile_is_cancelled(self):
        inv = self.sent()
        reminders.send_due_reminders()
        Invoice.objects.filter(pk=inv.pk).update(status="paid")
        notify.flush_outbox()
        self.assertEqual(self.reminders().get().state, "cancelled")
        self.assertEqual([m for m in mail.outbox if "c@example.com" in m.to], [])

    def test_a_failed_key_refuses_the_letter_rather_than_sending_it_plain(self):
        from apps.keys import delivery

        self.sent()
        reminders.send_due_reminders()
        with mock.patch("apps.notify.services.delivery.key_for_contact",
                        side_effect=delivery.EncryptionRefused("key_failed")):
            notify.flush_outbox()
        self.assertEqual(self.reminders().get().state, "failed")
        self.assertEqual(mail.outbox, [])

    def test_reissuing_by_hand_cancels_a_waiting_reminder(self):
        from apps.invoices.services import reissue_links

        inv = self.sent()
        reminders.send_due_reminders()
        reissue_links(inv)
        self.assertEqual(self.reminders().get().state, "cancelled")


class DisabledTests(Base):
    def test_zero_and_none_disable_it(self):
        self.sent(age_days=400)
        for value in (0, None):
            with override_settings(INVOICE_REMINDER_DAYS=value):
                self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertFalse(self.reminders().exists())

    def test_the_job_is_registered_daily(self):
        from apps.core import jobs, worker

        worker.reset()
        jobs.register_all()
        self.assertEqual(worker._jobs["invoice_reminders"]["every_s"], 86400)


class StaffPageTests(StaffBase):
    def test_the_staff_page_says_when_the_reminder_went(self):
        self.sign_in()
        inv = self.invoice
        OutboxMessage.objects.create(kind="invoice", invoice=inv, language="en", to=["a@example.com"],
                                     state="sent")
        Invoice.objects.filter(pk=inv.pk).update(sent_at=timezone.now() - datetime.timedelta(days=40))
        self.assertNotContains(self.client.get(f"/invoices/{inv.pk}/"), "Reminder queued on")
        with override_settings(INVOICE_REMINDER_DAYS=30):
            reminders.send_due_reminders()
        page = self.client.get(f"/invoices/{inv.pk}/")
        when = timezone.localtime(Invoice.objects.get(pk=inv.pk).reminder_sent_at).strftime("%Y-%m-%d")
        self.assertContains(page, f"Reminder queued on {when}")
        self.assertContains(page, "Payment reminder queued")


@override_settings(INVOICE_REMINDER_DAYS=30)
class FixRound1Tests(Base):
    def body(self, to="c@example.com"):
        notify.flush_outbox()
        (letter,) = [m for m in mail.outbox if to in m.to]
        return letter

    def test_the_amount_is_formatted_in_the_contacts_language(self):
        de, _ = find_or_create_contact(self.org, "d@example.com", legal_name="Kunde", preferred_language="de")
        self.sent(contact=de)
        reminders.send_due_reminders()
        letter = self.body("d@example.com")
        self.assertIn("1.337,00", letter.body)
        self.assertNotIn("1,337.00", letter.body)

    def test_a_us_resident_on_a_non_us_issuer_gets_nothing_and_no_new_links(self):
        inv = self.sent()
        Contact.objects.filter(pk=self.contact.pk).update(us_tax_resident="yes")
        before = inv.pay_token_hash
        self.assertEqual(reminders.send_due_reminders(), 0)
        inv.refresh_from_db()
        self.assertEqual((inv.pay_token_hash, inv.reminder_sent_at), (before, None))
        self.assertFalse(self.reminders().exists())

    def test_a_failed_key_skips_the_run_and_touches_nothing(self):
        from apps.keys import delivery

        inv = self.sent()
        before = inv.pay_token_hash
        with mock.patch("apps.invoices.reminders.delivery.key_for_contact",
                        side_effect=delivery.EncryptionRefused("key_failed")), \
                self.assertLogs("apps.invoices.reminders", "ERROR") as logs:
            self.assertEqual(reminders.send_due_reminders(), 0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"#{inv.pk}", logs.output[0])
        inv.refresh_from_db()
        self.assertEqual((inv.pay_token_hash, inv.reminder_sent_at), (before, None))
        self.assertFalse(self.reminders().exists())
        self.assertEqual(reminders.send_due_reminders(), 1)  # the key is fine again: the next run sends it

    def test_a_payment_in_flight_defers_the_reminder(self):
        for state in ("detected", "needs_review"):
            inv = self.sent()
            pay = Payment.objects.create(invoice=inv, rail="bank", asset="EUR", amount_minor=100,
                                         received_amount=100, state=state)
            self.assertEqual(reminders.send_due_reminders(), 0, state)
            Payment.objects.filter(pk=pay.pk).update(state="rejected")
            self.assertEqual(reminders.send_due_reminders(), 1, state)

    def test_an_open_quote_defers_the_reminder(self):
        from apps.quotes.models import Quote
        from apps.rails.models import RailAccount

        inv = self.sent()
        acct = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20,
                                          networks=["arbitrum"], assets=["USDC"])
        now = timezone.now()
        quote = Quote.objects.create(invoice=inv, rail_account=acct, network="arbitrum", asset="USDC",
                                     address="0x" + "ab" * 20, amount_base_units="1", min_accept_base_units="1",
                                     ceiling_base_units="1", invoice_amount_minor=100, issued_at=now,
                                     expires_at=now + datetime.timedelta(hours=1))
        self.assertEqual(reminders.send_due_reminders(), 0)
        Quote.objects.filter(pk=quote.pk).update(state="expired")
        self.assertEqual(reminders.send_due_reminders(), 1)

    def test_subjects_name_the_document_in_every_language(self):
        from django.utils import translation

        from apps.notify.services import SUBJECTS

        for lang, invoice_word, request_word in [
            ("sk", "faktúry", "žiadosť o platbu"), ("cs", "faktury", "žádost o platbu"),
            ("de", "Rechnung", "Zahlungsaufforderung"), ("es", "factura", "solicitud de pago"),
            ("fr", "facture", "demande de paiement"), ("pl", "faktura", "prośba o płatność"),
            ("ko", "청구서", "결제 요청"), ("ru", "счёт", "запрос на оплату"),
        ]:
            with translation.override(lang):
                a = translation.gettext(SUBJECTS["reminder"])
                b = translation.gettext(SUBJECTS["reminder_request"])
            self.assertIn(invoice_word, a, lang)
            self.assertNotIn(request_word, a, lang)
            self.assertIn(request_word, b, lang)
            self.assertNotIn(invoice_word, b, lang)

    def test_a_payment_request_reminder_has_the_request_subject(self):
        inv = create_invoice(self.org, self.contact, currency="EUR", lines=[LINE], kind="payment_request")[0]
        inv = send_invoice(inv)[0]
        Invoice.objects.filter(pk=inv.pk).update(sent_at=timezone.now() - datetime.timedelta(days=40))
        OutboxMessage.objects.filter(kind="invoice").update(state="sent")
        reminders.send_due_reminders()
        letter = self.body()
        self.assertIn("payment request", letter.subject)
        self.assertIn("payment request", letter.body)

    def test_a_run_logs_one_summary_line(self):
        self.sent()
        with self.assertLogs("apps.invoices.reminders", "INFO") as logs:
            reminders.send_due_reminders()
        self.assertEqual(logs.output, ["INFO:apps.invoices.reminders:invoice reminders: 1 queued"])
