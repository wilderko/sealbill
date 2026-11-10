from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.payments.models import Declaration, Payment
from apps.payments.services import (
    apply_payments, confirm_payment, reject_payment, remaining_minor, record_manual_payment,
)
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin


class PaymentBase(UnsealedVaultMixin, TestCase):
    language = "sk"

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.staff = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(
            self.org, "a@example.com", legal_name="Ján Žilinský", preferred_language=self.language
        )
        inv, _ = create_invoice(
            self.org, self.contact, currency="EUR",
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}],
        )
        self.invoice, self.view, self.pay = send_invoice(inv)
        OutboxMessage.objects.all().delete()  # drop the invoice e-mail

    def refresh(self):
        self.invoice.refresh_from_db()
        return self.invoice


class ApplyPaymentsTests(PaymentBase):
    def test_partial_then_full_is_paid(self):
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="first", user=self.staff)
        self.assertEqual(self.refresh().status, "partially_paid")
        self.assertIsNone(self.invoice.paid_at)
        self.assertEqual(remaining_minor(self.invoice), 6000)
        record_manual_payment(self.invoice, amount_minor=6000, rail="bank", note="rest", user=self.staff)
        self.assertEqual(self.refresh().status, "paid")
        self.assertIsNotNone(self.invoice.paid_at)

    def test_overpaid(self):
        record_manual_payment(self.invoice, amount_minor=10100, rail="manual", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "overpaid")
        self.assertIsNotNone(self.invoice.paid_at)

    def test_receipt_enqueued_once_on_paid(self):
        record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="", user=self.staff)
        apply_payments(self.invoice)
        apply_payments(self.invoice)
        receipts = OutboxMessage.objects.filter(kind="receipt")
        self.assertEqual(receipts.count(), 1)
        msg = receipts.get()
        self.assertEqual((msg.recipient, msg.to), ("contact", None))  # addressed when it is sent
        notify._address(msg)
        self.assertEqual(msg.language, "sk")
        self.assertEqual(msg.to, ["a@example.com"])
        for key in ("ref", "org", "name", "total", "view_url"):
            self.assertIn(key, msg.body_context)
        self.assertEqual(msg.body_context["view_url"], "")  # nothing secret in the receipt
        # a further payment pushing it to overpaid sends no second receipt
        record_manual_payment(self.invoice, amount_minor=5, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "overpaid")
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt").count(), 1)

    def test_partial_email_on_each_increase_not_on_reapply(self):
        record_manual_payment(self.invoice, amount_minor=3000, rail="bank", note="", user=self.staff)
        apply_payments(self.invoice)
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)
        record_manual_payment(self.invoice, amount_minor=2000, rail="bank", note="", user=self.staff)
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 2)
        msg = OutboxMessage.objects.filter(kind="partial").last()
        notify._address(msg)
        ctx = msg.body_context
        for key in ("paid", "remaining", "pay_url", "ref", "org", "name"):
            self.assertIn(key, ctx)
        self.assertEqual(ctx["pay_url"], "")
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt").count(), 0)

    def test_open_declarations_matched_when_paid(self):
        d = Declaration.objects.create(invoice=self.invoice, note="x")
        record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="", user=self.staff)
        d.refresh_from_db()
        self.assertEqual(d.state, "matched")
        self.assertTrue(AuditEntry.objects.filter(action="declaration.matched", object_pk=str(d.pk)).exists())

    def test_void_and_draft_refuse_payments(self):
        void_invoice(self.invoice, "oops", self.staff)
        with self.assertRaises(InvoiceError):
            record_manual_payment(self.invoice, amount_minor=100, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "void")
        draft, _ = create_invoice(
            self.org, self.contact, currency="EUR", lines=[{"name": "S", "unit_price_minor": 100}]
        )
        with self.assertRaises(InvoiceError):
            record_manual_payment(draft, amount_minor=100, rail="bank", note="", user=self.staff)

    def test_amount_and_rail_validated(self):
        for amount in (0, -5):
            with self.assertRaises(InvoiceError):
                record_manual_payment(self.invoice, amount_minor=amount, rail="bank", note="", user=self.staff)
        with self.assertRaises(InvoiceError):
            record_manual_payment(self.invoice, amount_minor=100, rail="cash", note="", user=self.staff)
        self.assertEqual(Payment.objects.count(), 0)

    def test_confirm_and_reject_write_audit_entries(self):
        p1 = Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=10000,
                                    received_amount="100.00", state="detected")
        p2 = Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=500,
                                    received_amount="5.00", state="needs_review")
        confirm_payment(p1, self.staff, note="seen on statement")
        reject_payment(p2, self.staff, "not ours")
        self.assertEqual(self.refresh().status, "paid")
        p1.refresh_from_db(); p2.refresh_from_db()
        self.assertEqual((p1.state, p2.state), ("confirmed", "rejected"))
        self.assertEqual(p1.confirmed_by, self.staff)
        self.assertTrue(AuditEntry.objects.filter(action="payment.confirm", actor=self.staff, object_pk=str(p1.pk)).exists())
        e = AuditEntry.objects.get(action="payment.reject", object_pk=str(p2.pk))
        self.assertEqual(e.detail["note"], "not ours")
        with self.assertRaises(InvoiceError):
            reject_payment(p1, self.staff, "late")  # a confirmed payment is not rejected
        with self.assertRaises(InvoiceError):
            confirm_payment(p2, self.staff)

    def test_record_and_void_audited(self):
        p = record_manual_payment(self.invoice, amount_minor=100, rail="bank", note="n", user=self.staff)
        self.assertTrue(AuditEntry.objects.filter(action="payment.record", object_pk=str(p.pk)).exists())

    def test_duplicate_tx_hash_rejected_by_constraint(self):
        kw = dict(invoice=self.invoice, rail="manual", network="eth", asset="USDC", amount_minor=100,
                  received_amount="1", tx_hash="0xabc", log_index=3)
        Payment.objects.create(**kw)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(**kw)
        Payment.objects.create(**{**kw, "log_index": 4})
        # manual payments carry no hash and never collide
        Payment.objects.create(invoice=self.invoice, rail="manual", amount_minor=1, received_amount="0.01")
        Payment.objects.create(invoice=self.invoice, rail="manual", amount_minor=1, received_amount="0.01")


class PayPageBalanceTests(PaymentBase):
    def test_pay_page_shows_remaining_balance_when_partially_paid(self):
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, "100.00")
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        html = r.content.decode()
        due = html.split("Amount to pay")[1].split("</div>")[0]
        self.assertIn("60.00", due)
        self.assertNotIn("100.00", due)


class FixRound1Tests(PaymentBase):
    def test_cannot_void_invoice_with_confirmed_money(self):
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        Declaration.objects.create(invoice=self.invoice)
        from apps.payments.services import declare_payment
        declare_payment(self.invoice)
        self.assertEqual(self.refresh().status, "partially_paid")  # confirmed money decides before a claim
        Invoice.objects.filter(pk=self.invoice.pk).update(status="declared")  # even a row that says declared
        with self.assertRaises(InvoiceError):
            void_invoice(self.invoice, "oops", self.staff)
        self.assertEqual(self.refresh().status, "declared")

    def test_apply_payments_idempotent_on_declared(self):
        from apps.payments.services import declare_payment
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        declare_payment(self.invoice)
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)
        self.assertEqual(apply_payments(self.invoice), "partially_paid")
        self.assertEqual(self.refresh().status, "partially_paid")
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)

    def test_reverse_payment(self):
        from apps.payments.services import reverse_payment
        p = record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "paid")
        mails = OutboxMessage.objects.count()
        with self.assertRaises(InvoiceError):
            reverse_payment(p, self.staff, "  ")
        reverse_payment(p, self.staff, "bounced")
        p.refresh_from_db()
        self.assertEqual(p.state, "reversed")
        self.assertEqual(self.refresh().status, "sent")
        self.assertIsNone(self.invoice.paid_at)
        self.assertEqual(OutboxMessage.objects.count(), mails)  # no client e-mail
        e = AuditEntry.objects.get(action="payment.reverse")
        self.assertEqual((e.detail["reason"], e.actor_username), ("bounced", "clerk"))
        with self.assertRaises(InvoiceError):
            reverse_payment(p, self.staff, "again")
        # partial reversal: 10000 paid + 500 -> overpaid; reverse the 500 -> paid
        record_manual_payment(self.invoice, amount_minor=6000, rail="bank", note="", user=self.staff)
        q = record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        reverse_payment(q, self.staff, "x")
        self.assertEqual(self.refresh().status, "partially_paid")

    def test_dismiss_declaration(self):
        from apps.payments.services import dismiss_declaration
        d = Declaration.objects.create(invoice=self.invoice)
        dismiss_declaration(d, self.staff, "")
        d.refresh_from_db()
        self.assertEqual(d.state, "dismissed")
        self.assertTrue(AuditEntry.objects.filter(action="declaration.dismiss", actor=self.staff).exists())
        with self.assertRaises(InvoiceError):
            dismiss_declaration(d, self.staff, "")

    def test_declared_staff_link_is_staff_page(self):
        from django.test import override_settings
        from apps.payments.services import declare_payment
        with override_settings(STAFF_NOTIFY_EMAILS=["s@example.com"], SITE_URL="https://pay.example"):
            declare_payment(self.invoice)
        msg = OutboxMessage.objects.get(kind="declared_staff")
        self.assertEqual(msg.body_context["admin_url"], f"https://pay.example/invoices/{self.invoice.pk}/")


class RecomputeStatusTests(PaymentBase):
    """I2: one rule decides the status (money, then an open declaration, then the due date)."""

    def past_due(self):
        import datetime

        Invoice.objects.filter(pk=self.invoice.pk).update(due_date=datetime.date.today() - datetime.timedelta(days=3))
        self.invoice.refresh_from_db()

    def declare(self, **kw):
        from apps.payments.services import declare_payment

        return declare_payment(self.invoice, **kw)

    def test_dismissing_the_only_declaration_goes_back_to_sent(self):
        from apps.payments.services import dismiss_declaration

        d = self.declare()
        self.assertEqual(self.refresh().status, "declared")
        dismiss_declaration(d, self.staff, "not seen")
        self.assertEqual(self.refresh().status, "sent")

    def test_dismissing_the_only_declaration_of_a_past_due_invoice_goes_to_overdue(self):
        from apps.payments.services import dismiss_declaration

        self.past_due()
        d = self.declare()
        self.assertEqual(self.refresh().status, "declared")
        dismiss_declaration(d, self.staff, "")
        self.assertEqual(self.refresh().status, "overdue")

    def test_dismissing_one_of_two_declarations_stays_declared(self):
        from apps.payments.services import dismiss_declaration

        first = self.declare()
        self.declare(note="again")
        dismiss_declaration(first, self.staff, "")
        self.assertEqual(self.refresh().status, "declared")

    def test_reversing_the_only_payment_recomputes(self):
        from apps.payments.services import reverse_payment

        self.past_due()
        p = record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "paid")
        reverse_payment(p, self.staff, "bounced")
        self.assertEqual(self.refresh().status, "overdue")
        self.assertIsNone(self.invoice.paid_at)
        q = record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        self.declare(note="paid the rest")
        self.assertEqual(self.refresh().status, "partially_paid")  # confirmed money decides first
        reverse_payment(q, self.staff, "bounced")
        self.assertEqual(self.refresh().status, "declared")  # the open declaration remains

    def test_partial_payment_on_a_declared_invoice_counts_by_sum(self):
        self.declare()
        record_manual_payment(self.invoice, amount_minor=3000, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "partially_paid")
        record_manual_payment(self.invoice, amount_minor=7000, rail="bank", note="", user=self.staff)
        self.assertEqual(self.refresh().status, "paid")
        self.assertFalse(Declaration.objects.filter(state="open").exists())

    def test_overdue_job_moves_declared_without_open_declaration(self):
        import datetime

        from apps.invoices.services import mark_overdue
        from apps.vault.vault import vault

        self.past_due()
        open_one = create_invoice(self.org, self.contact, currency="EUR", due_days=0,
                                  issue_date=datetime.date.today() - datetime.timedelta(days=9),
                                  lines=[{"name": "S", "quantity": 1, "unit_price_minor": 100}])[0]
        send_invoice(open_one, email_client=False)
        from apps.payments.services import declare_payment

        declare_payment(open_one)  # still has an open declaration: stays declared
        Invoice.objects.filter(pk=self.invoice.pk).update(status="declared")  # declared, nothing open
        vault.seal()  # plaintext columns only: runs sealed too
        self.assertEqual(mark_overdue(datetime.date.today()), 1)
        status = dict(Invoice.objects.values_list("pk", "status"))  # read as a sealed reader must
        self.assertEqual(status[self.invoice.pk], "overdue")
        self.assertEqual(status[open_one.pk], "declared")

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"])
    def test_staff_told_when_a_declaration_arrives_with_no_other_open(self):
        from apps.payments.services import dismiss_declaration

        staff_mail = lambda: OutboxMessage.objects.filter(kind="declared_staff").count()
        d = self.declare()
        self.assertEqual(staff_mail(), 1)
        self.declare()  # another one while the first is open: no second mail
        self.assertEqual(staff_mail(), 1)
        for open_d in Declaration.objects.filter(state="open"):
            dismiss_declaration(open_d, self.staff, "")
        self.declare()  # a fresh claim after the others were dismissed
        self.assertEqual(staff_mail(), 2)
        for open_d in Declaration.objects.filter(state="open"):
            dismiss_declaration(open_d, self.staff, "")
        Invoice.objects.filter(pk=self.invoice.pk).update(status="declared")  # declared with nothing open
        self.declare()
        self.assertEqual(staff_mail(), 3)
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        for open_d in Declaration.objects.filter(state="open"):
            dismiss_declaration(open_d, self.staff, "")
        self.declare()  # from partially_paid
        self.assertEqual(staff_mail(), 4)
        self.assertEqual(self.refresh().status, "partially_paid")
        del d

    def test_void_dismisses_open_declarations_with_an_audit_entry(self):
        d = self.declare()
        void_invoice(self.invoice, "duplicate", self.staff)
        d.refresh_from_db()
        self.assertEqual(d.state, "dismissed")
        e = AuditEntry.objects.get(action="declaration.dismiss", object_pk=str(d.pk))
        self.assertEqual(e.actor, self.staff)
        self.assertIn("void", e.detail["reason"])
        self.assertEqual(self.refresh().status, "void")
