"""Plan C carry-overs in the invoice and notify services."""
import datetime

from django.core import mail
from django.test import TestCase

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.services import InvoiceError, create_invoice, mark_overdue, send_invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import FiatLink, Organization
from apps.orgs.testing import add_bank
from apps.vault.testing import UnsealedVaultMixin
from apps.payments.testing import person

LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 10000}


class CarryoverBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="A")


class FiatLinkCurrencyTests(CarryoverBase):
    def test_explicit_fiat_link_in_wrong_currency_refused(self):
        link = FiatLink.objects.create(org=self.org, kind="wise", currencies=["USD"])
        with self.assertRaisesMessage(InvoiceError, "does not accept EUR"):
            create_invoice(self.org, self.contact, currency="EUR", lines=[LINE],
                           offered_methods=[f"fiat:{link.pk}"])
        self.assertFalse(Invoice.objects.exists())
        invoice, _ = create_invoice(self.org, self.contact, currency="usd", lines=[LINE],
                                    offered_methods=[f"fiat:{link.pk}"])
        self.assertEqual(invoice.offered_methods, [f"fiat:{link.pk}"])


class PartialLetterTests(CarryoverBase):
    def test_partial_letter_cancelled_when_invoice_no_longer_partial(self):
        from apps.payments.services import record_manual_payment, reverse_payment

        invoice, *_ = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0])
        OutboxMessage.objects.all().delete()
        p = record_manual_payment(invoice, amount_minor=4000, rail="bank", note="", user=person())
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)
        reverse_payment(p, None, "bounced")
        self.assertEqual(notify.flush_outbox(), 0)
        self.assertEqual(OutboxMessage.objects.get(kind="partial").state, "cancelled")
        self.assertEqual(len(mail.outbox), 0)


class OverdueAuditTests(CarryoverBase):
    def test_overdue_move_is_audited(self):
        invoice, *_ = send_invoice(create_invoice(self.org, self.contact, currency="EUR", lines=[LINE],
                                                  due_days=5)[0])
        later = invoice.due_date + datetime.timedelta(days=1)
        self.assertEqual(mark_overdue(later), 1)
        entry = AuditEntry.objects.get(action="invoice.overdue", object_pk=str(invoice.pk))
        self.assertEqual(entry.system_detail, {"from": "sent", "to": "overdue"})
        self.assertEqual(mark_overdue(later), 0)
        self.assertEqual(AuditEntry.objects.filter(action="invoice.overdue").count(), 1)
