"""Plan A final review I1: money that arrives while the vault is sealed still moves the invoice.

Each test sends an invoice while the vault is unsealed and then seals it. Everything a watcher
does with a confirmed payment (lock, recompute, match declarations, audit, queue the client's
letter) must then work without the key; the letter is addressed and written only at flush.
"""
import datetime
import json
from unittest import mock

from django.core import mail
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone, translation
from django.utils.translation import gettext

from apps.audit import services as audit
from apps.audit.models import AuditEntry
from apps.contacts.models import ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import INVOICE_PLAIN_FIELDS, create_invoice, lock_invoice, send_invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Declaration, Payment
from apps.payments.services import apply_payments, record_manual_payment
from apps.vault.registry import encrypted_field_names
from apps.vault.testing import TEST_VAULT_PASSWORD, UnsealedVaultMixin
from apps.vault.vault import vault
from apps.payments.testing import person


class SealedBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(
            self.org, "old@example.com", legal_name="Ján Žilinský", preferred_language="sk"
        )
        # The first address is no longer the primary one: the letter must go to the primary.
        self.contact.emails.update(primary=False)
        ContactEmail.objects.create(contact=self.contact, address="primary@example.com", primary=True)
        # Encrypted values everywhere a full-row load would decrypt them.
        inv, _ = create_invoice(
            self.org, self.contact, currency="EUR", title="Consulting", notes="Thank you",
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}],
        )
        self.invoice, *_ = send_invoice(inv, email_client=False)

    def confirmed_payment(self, amount_minor):
        """What a watcher records: plaintext columns only, nothing that needs the key."""
        return Payment.objects.create(
            invoice_id=self.invoice.pk, rail="manual", asset="EUR", amount_minor=amount_minor,
            received_amount="0", state="confirmed", confirmed_at=timezone.now(),
        )

    def stored(self, *fields):
        return Invoice.objects.filter(pk=self.invoice.pk).values_list(*fields).get()


class SealedPaymentTests(SealedBase):
    def test_apply_payments_while_sealed_marks_paid(self):
        declaration = Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash="0xabc")
        vault.seal()
        self.confirmed_payment(10000)
        self.assertEqual(apply_payments(self.invoice), "paid")
        status, paid_at = self.stored("status", "paid_at")
        self.assertEqual(status, "paid")
        self.assertIsNotNone(paid_at)
        self.assertEqual(Declaration.objects.filter(pk=declaration.pk).values_list("state", flat=True).get(), "matched")
        entry = AuditEntry.objects.filter(action="declaration.matched", object_pk=str(declaration.pk)).values(
            "actor_id", "actor_username", "detail", "system_detail").get()
        self.assertEqual(entry, {"actor_id": None, "actor_username": "system", "detail": None,
                                 "system_detail": {"invoice": self.invoice.pk}})
        self.assertEqual(apply_payments(self.invoice), "paid")  # no new money: nothing changes
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt").count(), 1)

    def test_receipt_queued_sealed_is_addressed_at_flush(self):
        vault.seal()
        self.confirmed_payment(10000)
        apply_payments(self.invoice)
        row = OutboxMessage.objects.filter(kind="receipt").values(
            "to", "cc", "subject_context", "body_context", "recipient", "contact_id", "plain_context").get()
        self.assertEqual(row, {
            "to": None, "cc": None, "subject_context": None, "body_context": None,
            "recipient": "contact", "contact_id": self.contact.pk,
            "plain_context": {"paid_minor": 10000, "total_minor": 10000, "currency": "EUR"},
        })
        self.assertEqual(notify.flush_outbox(), 0)  # sealed: it waits
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(notify.flush_outbox(), 1)
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["primary@example.com"])
        with translation.override("sk"):
            subject = gettext(notify.SUBJECTS["receipt"]) % {"ref": self.invoice.number, "org": "Seller s.r.o."}
        self.assertEqual(m.subject, subject)
        self.assertNotIn("Receipt", m.subject)
        self.assertIn("Ján Žilinský", m.body)
        self.assertIn("100,00 EUR", m.body)  # money formatted in the contact's language
        self.assertNotIn("://", m.body)  # the receipt carries no link
        self.assertEqual(m.attachments[0][2], "application/pdf")
        self.assertEqual(OutboxMessage.objects.values_list("state", "language").get(), ("sent", "sk"))

    def test_partial_letter_sealed_then_sent(self):
        # Staff recorded part of it earlier, with a note (encrypted) on the payment.
        record_manual_payment(self.invoice, amount_minor=1500, rail="bank", note="on the statement", user=person())
        OutboxMessage.objects.all().delete()
        vault.seal()
        self.confirmed_payment(2500)
        self.assertEqual(apply_payments(self.invoice), "partially_paid")
        self.assertIsNone(self.stored("paid_at")[0])
        self.assertEqual(
            OutboxMessage.objects.filter(kind="partial").values_list("to", "plain_context").get(),
            (None, {"paid_minor": 4000, "total_minor": 10000, "currency": "EUR"}),
        )
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(notify.flush_outbox(), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["primary@example.com"])
        self.assertIn("40,00 EUR", m.body)  # received
        self.assertIn("60,00 EUR", m.body)  # still outstanding
        self.assertNotIn("Amount received", m.body)  # Slovak, not English
        self.assertEqual(m.attachments, [])

    def test_contact_without_email_fails_final_at_flush(self):
        vault.seal()
        self.confirmed_payment(10000)
        apply_payments(self.invoice)
        vault.unseal(TEST_VAULT_PASSWORD)
        self.contact.emails.all().delete()
        with self.assertLogs("apps.notify.services", "ERROR") as logs:
            self.assertEqual(notify.flush_outbox(), 0)
        msg = OutboxMessage.objects.get(kind="receipt")
        self.assertEqual((msg.state, msg.attempts, msg.next_attempt_at), ("failed", 1, None))
        self.assertIsNotNone(msg.failed_at)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(any("NoRecipient" in line for line in logs.output))
        entry = AuditEntry.objects.get(action="outbox.failed", object_pk=str(msg.pk))
        self.assertEqual(entry.detail["error"], "NoRecipient")

    def test_sent_invoice_lifecycle_save_works_sealed(self):
        vault.seal()
        self.invoice.status = "paid"
        self.invoice.paid_at = timezone.now()
        self.invoice.save(update_fields=["status", "paid_at"])
        self.assertEqual(self.stored("status")[0], "paid")
        with transaction.atomic():
            locked = lock_invoice(self.invoice.pk)
            locked.status, locked.paid_at = "sent", None
            locked.save(update_fields=["status", "paid_at"])
        self.assertEqual(self.stored("status", "paid_at"), ("sent", None))

    def test_sent_invoice_title_edit_still_refused(self):
        self.invoice.title = "Changed"
        for update_fields in (["title"], ["status", "title"], None):
            with self.subTest(update_fields=update_fields), self.assertRaises(InvoiceError):
                self.invoice.save(update_fields=update_fields)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).title, "Consulting")

    def test_lock_invoice_reads_plaintext_columns_only(self):
        encrypted = set(encrypted_field_names(Invoice))
        self.assertTrue(encrypted)
        self.assertFalse(encrypted & set(INVOICE_PLAIN_FIELDS))
        self.assertIn("contact_id", INVOICE_PLAIN_FIELDS)
        self.assertIn("status", INVOICE_PLAIN_FIELDS)
        vault.seal()
        with transaction.atomic():
            locked = lock_invoice(self.invoice.pk)
        self.assertEqual(locked.get_deferred_fields(), encrypted)
        self.assertEqual(locked.status, "sent")


class RecordSystemTests(SealedBase):
    def test_record_system_writes_plaintext_only(self):
        payment = self.confirmed_payment(1000)
        vault.seal()
        entry = audit.record_system("payment.detected", payment, {
            "amount_minor": 1000, "network": "arbitrum", "final": True, "reason": None, "invoices": [1, 2],
        })
        with connection.cursor() as cur:
            cur.execute(
                "SELECT system_detail, detail, actor_id, actor_username, model_label, object_pk "
                "FROM audit_auditentry WHERE id = %s", [entry.pk])
            system_detail, detail, actor_id, actor_username, label, object_pk = cur.fetchone()
        text = system_detail if isinstance(system_detail, str) else json.dumps(system_detail)
        self.assertIn('"amount_minor": 1000', text)
        self.assertEqual(json.loads(text)["invoices"], [1, 2])
        self.assertIsNone(detail)
        self.assertEqual((actor_id, actor_username), (None, "system"))
        self.assertEqual((label, object_pk), ("payments.payment", str(payment.pk)))

    def test_record_system_refuses_anything_but_flat_plain_values(self):
        before = AuditEntry.objects.count()
        for bad in (
            {"when": timezone.now()}, {"day": datetime.date(2026, 11, 2)}, {"nested": {"a": 1}},
            {"rate": 1.5}, {"list": [1, {"a": 1}]}, {"list": [[1]]}, {1: "non-string key"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                audit.record_system("payment.detected", self.invoice, bad)
        self.assertEqual(AuditEntry.objects.count(), before)


def _staff_builder(msg):
    return {"ref": "2026-0001", "name": "A", "total": "1 EUR", "admin_url": "https://pay.example/",
            **msg.plain_context}


@mock.patch.dict(notify.CONTEXT_BUILDERS, {"declared_staff": _staff_builder})
class StaffAddressedLaterTests(SealedBase):
    @override_settings(STAFF_NOTIFY_EMAILS=[])
    def test_staff_recipient_cancelled_when_no_staff_emails(self):
        vault.seal()
        msg = notify.enqueue_addressed_later(
            "declared_staff", recipient="staff", invoice=self.invoice, plain_context={"payment": 7})
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(OutboxMessage.objects.get(pk=msg.pk).state, "cancelled")
        entry = AuditEntry.objects.get(action="outbox.cancelled", object_pk=str(msg.pk))
        self.assertEqual(entry.detail["reason"], "no staff recipients")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com", "boss@example.com"])
    def test_staff_recipient_addressed_to_staff_in_english(self):
        vault.seal()
        notify.enqueue_addressed_later(
            "declared_staff", recipient="staff", invoice=self.invoice, plain_context={"payment": 7})
        vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(notify.flush_outbox(), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ops@example.com", "boss@example.com"])
        self.assertEqual(m.subject, "Payment declared for 2026-0001")
        self.assertIn("https://pay.example/", m.body)

    def test_enqueue_addressed_later_validates_its_arguments(self):
        cases = (
            dict(kind="nope", recipient="staff", plain_context={}),
            dict(kind="invoice", recipient="staff", plain_context={}),  # no builder
            dict(kind="declared_staff", recipient="someone", plain_context={}),
            dict(kind="receipt", recipient="contact", plain_context={}),  # no contact
            dict(kind="declared_staff", recipient="staff", plain_context={"when": timezone.now()}),
        )
        for kw in cases:
            kind = kw.pop("kind")
            with self.subTest(kind=kind, **kw), self.assertRaises(ValueError):
                notify.enqueue_addressed_later(kind, invoice=self.invoice, **kw)
        self.assertEqual(OutboxMessage.objects.count(), 0)
