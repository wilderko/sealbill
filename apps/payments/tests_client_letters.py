"""An invoice the caller handles (`client_letters=False`) gets no letter to the client at all."""
from django.test import override_settings

from apps.api.tests import ApiBase
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import reissue_links, send_invoice
from apps.links.tests import LinkBase
from apps.notify.models import OutboxMessage
from apps.payments.services import declare_payment, record_manual_payment
from apps.payments.tests import PaymentBase
from apps.payments.testing import person

CLIENT_KINDS = ("invoice", "receipt", "partial")


def client_mail():
    return OutboxMessage.objects.filter(kind__in=CLIENT_KINDS)


class HandledByTheCallerTests(PaymentBase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STAFF_NOTIFY_EMAILS=["staff@seller.example"]))
        self.invoice = Invoice.objects.get(pk=self.invoice.pk)

    def handled(self):
        """A second, fresh draft sent with client_letters=False."""
        from apps.invoices.services import create_invoice

        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        sent, _v, _p = send_invoice(inv, email_client=False, client_letters=False)
        OutboxMessage.objects.all().delete()
        return sent

    def test_full_payment_queues_no_client_mail(self):
        inv = self.handled()
        record_manual_payment(inv, amount_minor=10000, rail="bank", note="", user=self.staff)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "paid")
        self.assertEqual(client_mail().count(), 0)

    def test_partial_payment_queues_no_client_mail(self):
        inv = self.handled()
        record_manual_payment(inv, amount_minor=4000, rail="bank", note="", user=self.staff)
        inv.refresh_from_db()
        self.assertEqual(inv.status, "partially_paid")
        self.assertEqual(client_mail().count(), 0)

    def test_the_default_still_queues_both_letters(self):
        self.assertTrue(self.invoice.client_letters)
        record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)
        record_manual_payment(self.invoice, amount_minor=6000, rail="bank", note="", user=self.staff)
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt").count(), 1)

    def test_the_staff_alert_still_goes_out(self):
        inv = self.handled()
        declare_payment(inv, note="paid")
        self.assertEqual(OutboxMessage.objects.filter(kind="declared_staff").count(), 1)

    def test_client_letters_false_refuses_the_invoice_letter(self):
        from apps.invoices.services import create_invoice

        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        with self.assertRaises(InvoiceError):
            send_invoice(inv, email_client=True, client_letters=False)

    def test_no_letter_for_reissued_links(self):
        from apps.invoices.services import queue_invoice_letter

        inv = self.handled()
        v, p = reissue_links(inv)
        self.assertIsNone(queue_invoice_letter(inv, "a@example.com", v, p))
        self.assertEqual(client_mail().count(), 0)


class PaymentLinkOrderTests(LinkBase):
    def test_a_payment_link_order_still_gets_its_receipt(self):
        invoice, _pay = self.checkout()
        self.assertTrue(Invoice.objects.get(pk=invoice.pk).client_letters)
        record_manual_payment(invoice, amount_minor=4000, rail="bank", note="", user=person())
        self.assertEqual(OutboxMessage.objects.filter(kind="partial").count(), 1)
        self.pay_fully(invoice)
        self.assertEqual(OutboxMessage.objects.filter(kind="receipt").count(), 1)


class SendApiTests(ApiBase):
    def draft(self):
        from apps.contacts.services import find_or_create_contact
        from apps.invoices.services import create_invoice
        from apps.api.tests import LINE

        contact, _ = find_or_create_contact(self.org, "x@example.com", legal_name="X")
        return create_invoice(self.org, contact, currency="EUR", lines=[{**LINE, "quantity": 1}])[0]

    def send(self, body):
        inv = self.draft()
        c, _, _ = self.client_for()
        return inv, c.post(f"/api/v1/invoices/{inv.pk}/send", body, format="json")

    def test_stored_and_exposed_read_only(self):
        inv, r = self.send({"client_letters": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["client_letters"], False)
        self.assertFalse(Invoice.objects.get(pk=inv.pk).client_letters)
        self.assertEqual(OutboxMessage.objects.filter(kind="invoice").count(), 0)

    def test_default_is_true_and_email_client_keeps_its_meaning(self):
        inv, r = self.send({"email_client": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["client_letters"], True)
        self.assertEqual(OutboxMessage.objects.filter(kind="invoice").count(), 0)

    def test_empty_body_queues_the_invoice_letter(self):
        inv, r = self.send({})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["client_letters"], True)
        self.assertEqual(OutboxMessage.objects.filter(kind="invoice").count(), 1)

    def test_contradiction_is_400_and_nothing_is_sent(self):
        inv, r = self.send({"client_letters": False, "email_client": True})
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(Invoice.objects.get(pk=inv.pk).status, "draft")

    def test_cannot_be_changed_later(self):
        inv, _ = self.send({"client_letters": False})
        inv = Invoice.objects.get(pk=inv.pk)
        inv.client_letters = True
        with self.assertRaises(InvoiceError):
            inv.save()
