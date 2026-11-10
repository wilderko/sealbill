"""End to end on the fiat rails: a bank transfer (or a payment link) that staff see arrive and
record. Nothing watches a bank account, so a client's "I have paid" is a claim for staff, and only
a person's record makes money of it. The invoice here is made and sent on the staff pages."""
import json
import re
import uuid

from django.core import mail

from apps.invoices.models import Invoice
from apps.invoices.reference import payment_reference
from apps.orgs.models import FiatLink
from apps.payments.e2e_helpers import CLIENT, CLIENT_NAME, STAFF, E2EBase
from apps.payments.models import Declaration, Payment
from apps.webhooks.models import WebhookEvent


class FiatBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.staff = self.staff_client()
        self.link = FiatLink.objects.create(org=self.org, kind="revolut", label="Revolut", currencies=["EUR"],
                                            url="https://revolut.me/seller")

    def staff_invoice(self, price="100.00", methods=None) -> tuple[int, str]:
        """Made and sent on the staff pages; returns the invoice and the pay link from its letter."""
        data = {
            "organization": self.org.pk, "kind": "invoice", "email": CLIENT, "legal_name": CLIENT_NAME,
            "display_name": "", "tax_ids": "", "address_line1": "", "address_line2": "", "address_city": "",
            "address_zip": "", "address_state": "", "address_country": "", "preferred_language": "en",
            "us_tax_resident": "no", "currency": "EUR", "issue_date": "", "due_days": "14", "title": "Residency file",
            "notes": "", "form_token": str(uuid.uuid4()),
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-name": "Service", "lines-0-description": "",
            "lines-0-quantity": "1", "lines-0-unit_price": price, "lines-0-tax_percent": "0",
            "lines-0-discount_percent": "0",
            "methods": methods or [f"bank:{self.bank.pk}", f"fiat:{self.link.pk}"],
        }
        from django.utils import timezone

        data["issue_date"] = timezone.localdate().isoformat()
        r = self.staff.post("/invoices/new/", data)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        pk = int(re.search(r"/invoices/(\d+)/", r["Location"]).group(1))
        r = self.staff.post(f"/invoices/{pk}/send/", {"email_client": "on"})
        self.assertEqual(r.status_code, 302)
        return pk, self.letter_pay_path()

    def record(self, pk, amount, rail="bank", note="SEPA credit seen"):
        return self.staff.post(f"/invoices/{pk}/payments/", {"amount": amount, "rail": rail, "note": note})


class FiatLifecycleTests(FiatBase):
    def test_bank_transfer_declared_by_the_client_and_recorded_by_staff(self):
        pk, pay = self.staff_invoice()
        invoice = Invoice.objects.get(pk=pk)
        page = self.pay_page(pay)
        self.assertIn(self.bank.account_number, page)
        self.assertIn(payment_reference(invoice), page)
        self.assertIn("https://revolut.me/seller", page)
        self.declare(pay, "other", "", note="paid by SEPA today")
        self.assertEqual(self.inv(pk).status, "declared")
        self.assertIn("We have been told that you have paid", self.status_text(pay))
        self.work("outbox")
        told = [m for m in mail.outbox if m.to == [STAFF] and "declared" in m.subject.lower()]
        self.assertEqual(len(told), 1)
        self.assertTrue(WebhookEvent.objects.filter(type="invoice.declared", invoice_id=pk).exists())
        self.assertEqual(self.record(pk, "100.00").status_code, 302)
        (payment,) = self.payments(pk)
        self.assertEqual((payment.rail, payment.amount_minor, payment.confirmed_by_id),
                         ("bank", 10000, self.staff.user.pk))
        self.assertEqual(Declaration.objects.get(invoice_id=pk).state, "matched")
        self.assertPaidEverywhere(pk, payment, pay, csv_amount="100.00")
        self.assertIn("payment.record", self.actions("payments.payment", payment.pk))

    def test_a_partial_transfer_then_the_rest(self):
        pk, pay = self.staff_invoice()
        self.assertEqual(self.record(pk, "40.00").status_code, 302)
        self.assertEqual(self.inv(pk).status, "partially_paid")
        self.assertIn("Partially paid. Please pay the remaining amount.", self.status_text(pay))
        self.assertIn("60.00", self.pay_page(pay))
        self.work("outbox")
        self.assertTrue(any(m.to == [CLIENT] and m.subject.startswith("Partial payment") for m in mail.outbox))
        self.assertEqual(self.record(pk, "60,00", rail="fiat_link", note="Revolut").status_code, 302)
        last = self.payments(pk)[-1]
        self.assertEqual((last.rail, last.amount_minor), ("fiat_link", 6000))
        self.assertPaidEverywhere(pk, last, pay)

    def test_an_overpaid_invoice_is_listed_for_a_person(self):
        pk, pay = self.staff_invoice()
        self.assertEqual(self.record(pk, "120.00").status_code, 302)
        (payment,) = self.payments(pk)
        self.assertPaidEverywhere(pk, payment, pay, status="overpaid")
        number = Invoice.objects.get(pk=pk).number
        self.assertRegex(self.staff.get("/").content.decode(),
                         rf">{re.escape(number)}</a></td><td class=\"cell-status\"><span class=\"pill pill--attention\">overpaid</span>")

    def test_a_token_cannot_record_a_payment_and_a_person_can(self):
        """Review S1, F1: "staff saw the money arrive" is said by a person, never by a token."""
        pk, pay = self.staff_invoice()
        body = json.dumps({"amount_minor": 10000, "rail": "bank", "note": "SEPA"})
        for scopes in (("read", "payments:confirm"), ("admin",)):
            confirmer, _token = self.api_client(scopes)
            r = confirmer.post(f"/api/v1/invoices/{pk}/payments", body, content_type="application/json",
                               HTTP_IDEMPOTENCY_KEY=f"sepa-{scopes[-1]}")
            self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"))
        reader, _t = self.api_client(("read", "invoices:write"))
        self.assertEqual(reader.post(f"/api/v1/invoices/{pk}/payments", body, content_type="application/json")
                         .status_code, 403)
        self.assertEqual(self.payments(pk), [])
        self.assertNotSettled(pk, pay)
        self.assertEqual(self.record(pk, "100.00").status_code, 302)  # the staff page, signed in with a second factor
        (payment,) = self.payments(pk)
        self.assertPaidEverywhere(pk, payment, pay)


class FiatWrongMoneyTests(FiatBase):
    def test_a_claim_alone_is_never_money(self):
        pk, pay = self.staff_invoice()
        self.declare(pay, "other", "SEPA-REF-123")
        self.work("declarations_verify")  # nothing to look up for a bank transfer
        self.assertNotSettled(pk, pay, status="declared")
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_void_invoice_takes_no_payment(self):
        pk, pay = self.staff_invoice()
        r = self.staff.post(f"/invoices/{pk}/void/", {"reason": "issued twice"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.record(pk, "100.00").status_code, 409)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertIn("voided", self.pay_page(pay))

    def test_a_bounced_transfer_is_reversed_and_the_invoice_is_owed_again(self):
        pk, pay = self.staff_invoice()
        self.assertEqual(self.record(pk, "100.00").status_code, 302)
        (payment,) = self.payments(pk)
        self.work("outbox")
        sent = len(mail.outbox)
        r = self.staff.post(f"/payments/{payment.pk}/reverse/", {"reason": "returned by the bank"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.inv(pk).status, "sent")
        self.assertIn("Awaiting payment", self.status_text(pay))
        self.assertTrue(WebhookEvent.objects.filter(type="payment.reversed", payment_id=payment.pk).exists())
        self.work("outbox")
        self.assertEqual(len(mail.outbox), sent)  # the client is not written to about it
        self.assertIn("payment.reverse", self.actions("payments.payment", payment.pk))
