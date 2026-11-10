from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.api import tests as api_tests
from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.errors import InvoiceError
from apps.invoices.services import create_invoice, send_invoice, void_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import REVIEW_REASONS, Payment
from apps.payments.services import (
    MAX_PAYMENT_MINOR, acknowledge_refund, assign_payment, confirm_payment, record_manual_payment,
)
from apps.staff import tests as staff_tests
from apps.vault.testing import UnsealedVaultMixin

BIG = "123456789012345678901234567890"


class CryptoBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján", preferred_language="en")
        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        self.draft = inv
        self.invoice, *_ = send_invoice(create_invoice(
            self.org, self.contact, currency="EUR",
            lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0], email_client=False)

    def review(self, **kw):
        base = dict(rail="evm", asset="USDC", amount_minor=10000, received_amount="100", state="needs_review",
                    review_reason="no_matching_invoice", final=True)  # staff act on final transfers only
        base.update(kw)
        return Payment.objects.create(**base)


class ModelTests(CryptoBase):
    def test_amount_base_units_exact_beyond_float_precision(self):
        p = self.review(amount_base_units=BIG, invoice=self.invoice)
        self.assertEqual(Payment.objects.get(pk=p.pk).amount_base_units, BIG)

    def test_digit_fields_validated(self):
        for bad in ("12.5", "-1", "abc", "1" * 79):
            with self.assertRaises(ValidationError):
                self.review(amount_base_units=bad)
        with self.assertRaises(ValidationError):
            self.review(refund_due_base_units="1e5")
        self.review(refund_due_base_units="")

    def test_native_and_token_log_in_one_tx_are_distinct(self):
        kw = dict(invoice=self.invoice, network="ethereum", tx_hash="0xabc")
        self.review(log_index=-1, **kw)
        self.review(log_index=0, **kw)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.review(log_index=0, **kw)

    def test_rail_external_id_unique_when_set(self):
        self.review(rail="btcpay", external_id="x1", invoice=self.invoice)
        self.review(rail="btcpay", external_id="", invoice=self.invoice)
        self.review(rail="btcpay", external_id="", invoice=self.invoice)
        self.review(rail="lightning", external_id="x1", invoice=self.invoice)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.review(rail="btcpay", external_id="x1", invoice=self.invoice)

    def test_payment_without_invoice_only_in_review(self):
        self.review(invoice=None)
        self.review(invoice=None, state="rejected")
        for state in ("confirmed", "detected", "reversed"):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.review(invoice=None, state=state)

    def test_review_reasons_complete(self):
        self.assertEqual(set(REVIEW_REASONS), {
            "token_not_allowed", "no_matching_invoice", "ambiguous", "after_expiry", "duplicate",
            "overpaid_beyond_ceiling", "invoice_closed", "before_quote", "dropped",
            "btcpay_partial", "btcpay_marked", "btcpay_invalid", "btcpay_late", "dust"})


class VoidTests(CryptoBase):
    def test_void_refused_while_detected_payment(self):
        self.review(invoice=self.invoice, state="detected")
        with self.assertRaisesMessage(InvoiceError, "a payment for this invoice is still being confirmed"):
            void_invoice(self.invoice, "no", self.staff)

    def test_void_allowed_with_review_payment_which_stays_listed(self):
        p = self.review(invoice=self.invoice)
        void_invoice(self.invoice, "no", self.staff)
        p.refresh_from_db()
        self.assertEqual((p.state, p.invoice_id), ("needs_review", self.invoice.pk))


class AssignTests(CryptoBase):
    def test_assign_review_payment_confirms_and_applies(self):
        p = self.review()
        out = assign_payment(p, self.invoice, self.staff, amount_minor=10000, note="matched by hand")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")
        self.assertEqual((out.state, out.invoice_id, out.amount_minor, out.confirmed_by), ("confirmed", self.invoice.pk, 10000, self.staff))
        self.assertTrue(AuditEntry.objects.filter(action="payment.assign", object_pk=str(p.pk)).exists())

    def test_assign_payment_that_already_has_an_invoice(self):
        p = self.review(invoice=self.invoice)
        assign_payment(p, self.invoice, self.staff, amount_minor=10000, note="n")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")

    def test_assign_to_void_invoice_refused(self):
        void_invoice(self.invoice, "no", self.staff)
        p = self.review()
        with self.assertRaises(InvoiceError):
            assign_payment(p, self.invoice, self.staff, amount_minor=10000, note="n")
        p.refresh_from_db()
        self.assertEqual((p.state, p.invoice_id), ("needs_review", None))

    def other_org_with_address(self, address):
        from apps.rails.models import RailAccount
        org = Organization.objects.create(slug=f"o{Organization.objects.count()}", legal_name="Other LLC", address="Elm 2")
        add_bank(org)
        contact, _ = find_or_create_contact(org, "b@example.com", legal_name="Bob", preferred_language="en")
        inv = send_invoice(create_invoice(org, contact, currency="EUR", lines=[
            {"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0], email_client=False)[0]
        if address:
            RailAccount.objects.create(org=org, kind="evm", label="B", address=address, networks=["arbitrum"],
                                       assets=["USDC"])
        return inv

    def wallet_payment(self, address="0x" + "ab" * 20):
        from apps.rails.models import RailAccount
        acct = RailAccount.objects.create(org=self.org, kind="evm", label="A", address=address,
                                          networks=["arbitrum"], assets=["USDC"])
        return self.review(network="arbitrum", to_address=address.upper().replace("0X", "0x"), rail_account=acct)

    def test_assign_to_another_org_sharing_the_address(self):
        p = self.wallet_payment()
        theirs = self.other_org_with_address("0x" + "ab" * 20)
        out = assign_payment(p, theirs, self.staff, amount_minor=10000, note="shared wallet")
        theirs.refresh_from_db()
        self.assertEqual((out.state, out.invoice_id, theirs.status), ("confirmed", theirs.pk, "paid"))

    def test_assign_refused_for_an_org_without_that_address(self):
        p = self.wallet_payment()
        theirs = self.other_org_with_address("0x" + "cd" * 20)
        with self.assertRaises(InvoiceError):
            assign_payment(p, theirs, self.staff, amount_minor=10000, note="wrong wallet")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")
        bare = self.other_org_with_address("")
        with self.assertRaises(InvoiceError):
            assign_payment(p, bare, self.staff, amount_minor=10000, note="no wallet")

    def test_assign_refusals(self):
        p = self.review()
        for kw in ({"note": ""}, {"note": "  "}, {"amount_minor": 0}, {"amount_minor": MAX_PAYMENT_MINOR + 1},
                   {"amount_minor": True}, {"amount_minor": 1.5}):
            args = {"amount_minor": 10000, "note": "n", **kw}
            with self.assertRaises(InvoiceError, msg=kw):
                assign_payment(p, self.invoice, self.staff, **args)
        with self.assertRaises(InvoiceError):  # draft invoice
            assign_payment(p, self.draft, self.staff, amount_minor=10000, note="n")
        done = self.review(state="detected", invoice=self.invoice)
        with self.assertRaises(InvoiceError):
            assign_payment(done, self.invoice, self.staff, amount_minor=10000, note="n")


class RefundTests(CryptoBase):
    def test_acknowledge_refund_records_who(self):
        p = self.review(invoice=self.invoice, refund_due_base_units="500")
        out = acknowledge_refund(p, self.staff, "refunded by hand")
        p.refresh_from_db()
        self.assertEqual(p.refund_acknowledged_by, self.staff)
        self.assertIsNotNone(p.refund_acknowledged_at)
        self.assertTrue(AuditEntry.objects.filter(action="payment.refund_ack", object_pk=str(p.pk)).exists())
        self.assertEqual(out.pk, p.pk)

    def test_acknowledge_refund_needs_a_refund_and_a_note(self):
        none_due = self.review(invoice=self.invoice)
        with self.assertRaises(InvoiceError):
            acknowledge_refund(none_due, self.staff, "x")
        due = self.review(invoice=self.invoice, refund_due_base_units="5")
        with self.assertRaises(InvoiceError):
            acknowledge_refund(due, self.staff, " ")


class CapTests(CryptoBase):
    def test_manual_payment_over_cap_refused(self):
        with self.assertRaises(InvoiceError):
            record_manual_payment(self.invoice, amount_minor=MAX_PAYMENT_MINOR + 1, rail="bank", note="", user=self.staff)
        record_manual_payment(self.invoice, amount_minor=MAX_PAYMENT_MINOR, rail="bank", note="", user=self.staff)

    def test_confirm_over_cap_refused(self):
        p = self.review(invoice=self.invoice, amount_minor=MAX_PAYMENT_MINOR + 1)
        with self.assertRaises(InvoiceError):
            confirm_payment(p, self.staff)

    def test_confirm_payment_without_invoice_refused(self):
        p = self.review()
        with self.assertRaises(InvoiceError):
            confirm_payment(p, self.staff)


class NeedsPersonTests(staff_tests.StaffBase):
    def test_needs_person_survives_payment_without_invoice(self):
        Payment.objects.create(rail="evm", asset="USDC", amount_minor=5, received_amount="5",
                               state="needs_review", review_reason="ambiguous")
        self.sign_in()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "USDC")


class ApiNullInvoiceTests(api_tests.ApiBase):
    def test_needs_person_and_api_survive_payment_without_invoice(self):
        p = Payment.objects.create(rail="evm", asset="USDC", amount_minor=5, received_amount="5",
                                   state="needs_review", review_reason="ambiguous")
        plain, _, _ = self.client_for(scopes=("read", "payments:confirm"))
        r = plain.get("/api/v1/payments")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"] if "results" in r.json() else r.json()["items"], [])
        self.assertEqual(plain.post(f"/api/v1/payments/{p.pk}/confirm", {"note": "x"}, format="json").status_code, 404)
        admin, _, _ = self.client_for(scopes=("admin",), name="adm")
        r = admin.get("/api/v1/payments")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        rows = body.get("results", body.get("items"))
        self.assertEqual([x["id"] for x in rows], [p.pk])
        self.assertIsNone(rows[0]["invoice_id"])
