"""SEC-2 (audit 2026-11): a payment waiting for a person is confirmed or assigned by a person.

A payment with `needs_staff_confirmation` waits in Needs a person. `confirm_payment` and
`assign_payment` refuse it unless the actor is an active staff user, so no caller can go around the
rule: API tokens act as inactive service users and get 409 `needs_person`; the staff pages keep
working. The guard keys on the flag alone, never on the asset (the owner, 2026-11-09)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.invoices.models import Invoice
from apps.payments import matching, services
from apps.payments.models import Payment
from apps.payments.tests_review_fixes import GatewayBase
from apps.staff.tests import StaffBase
from apps.rails.models import RailAccount
from apps.webhooks.models import WebhookEvent


def api(org, scopes=("read", "payments:confirm")):
    token, plain = ApiToken.issue(org, "automation", list(scopes))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
    return client, token


class Waiting(GatewayBase):
    """A Monero payment BTCPay called Settled: detected, waiting for a person (the PoC's state)."""

    def waiting(self, ext="btcpay:S1:x9"):
        p = self.xmr(self.xmr_quote(self.invoice, ext), ext)
        self.assertEqual((p.state, p.needs_staff_confirmation), ("detected", True))
        return p

    def waiting_in_review(self, ext="btcpay:S1:r1"):
        """No path stores this today (review clears the flag), but the guard must hold for it too."""
        p = self.waiting(ext)
        Payment.objects.filter(pk=p.pk).update(state="needs_review", review_reason="btcpay_late")
        return Payment.objects.get(pk=p.pk)

    def assertUntouched(self, p):
        fresh = Payment.objects.get(pk=p.pk)
        self.assertEqual((fresh.state, fresh.confirmed_at, fresh.confirmed_by_id, fresh.needs_staff_confirmation),
                         (p.state, None, None, True))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "sent")
        self.assertFalse(AuditEntry.objects.filter(action__in=("payment.confirm", "payment.assign"),
                                                   object_pk=str(p.pk)).exists())
        self.assertFalse(WebhookEvent.objects.filter(type="payment.confirmed").exists())


class ApiTokenRefusedTests(Waiting):
    def test_api_token_confirms_monero(self):
        """The audit's PoC (poc_sec_2): it answered 200, `confirmed`, and the invoice was paid."""
        p = self.waiting()
        client, _token = api(self.org)
        r = client.post(f"/api/v1/payments/{p.pk}/confirm", {"note": "script"}, format="json")
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["error"], "needs_person")
        self.assertUntouched(p)

    def test_an_admin_token_is_refused_too(self):
        p = self.waiting()
        client, _token = api(self.org, scopes=("admin",))
        r = client.post(f"/api/v1/payments/{p.pk}/confirm", {"note": "script"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"))
        self.assertUntouched(p)

    def test_api_assign_is_refused(self):
        p = self.waiting_in_review()
        client, _token = api(self.org)
        r = client.post(f"/api/v1/payments/{p.pk}/assign",
                        {"invoice_id": self.invoice.pk, "amount_minor": 10000, "note": "script"},
                        format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"), r.content)
        fresh = Payment.objects.get(pk=p.pk)
        self.assertEqual((fresh.state, fresh.idempotency_key, fresh.confirmed_by_id), ("needs_review", "", None))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "sent")

    def test_api_reject_is_refused(self):
        # Red-team report 2026-11-10, A3: a rejection cannot be undone, so it is a person's decision too.
        p = self.waiting()
        client, _token = api(self.org)
        r = client.post(f"/api/v1/payments/{p.pk}/reject", {"note": "script"}, format="json")
        self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"), r.content)
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "detected")
        self.assertFalse(AuditEntry.objects.filter(action="payment.reject", object_pk=str(p.pk)).exists())

    def test_a_payment_not_waiting_for_a_person_is_still_confirmed_by_a_token(self):
        """The guard is the flag, nothing else: a BTC payment BTCPay sees but has not settled is not waiting
        for a person, and a token still confirms it."""
        btc = self.hold(self.invoice, 200_000, network="bitcoin", asset="BTC", address="", account=self.btcpay,
                        external_id="btcpay:S1:b7", min_accept_base_units="200000", ceiling_base_units="200000")
        p = matching.record_gateway_payment(btc, network="bitcoin", external_id="btcpay:S1:b7",
                                            received_amount=Decimal("0.002"), amount_minor=10000, state="detected")
        client, token = api(self.org)
        r = client.post(f"/api/v1/payments/{p.pk}/confirm", {"note": "seen"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Payment.objects.get(pk=p.pk).confirmed_by, token.service_user)

    def test_an_xmr_review_payment_without_the_flag_is_not_held_by_its_asset(self):
        """The owner's change of 2026-11-09: the asset alone decides nothing here."""
        p = self.waiting("btcpay:S1:x3")
        Payment.objects.filter(pk=p.pk).update(state="needs_review", review_reason="btcpay_late",
                                               needs_staff_confirmation=False)
        client, _token = api(self.org)
        r = client.post(f"/api/v1/payments/{p.pk}/assign",
                        {"invoice_id": self.invoice.pk, "amount_minor": 10000, "note": "late, checked"},
                        format="json", HTTP_IDEMPOTENCY_KEY="k2")
        self.assertEqual(r.status_code, 200, r.content)


class ServiceGuardTests(Waiting):
    """The rule lives in the services, so every caller meets it, not only the API."""

    def not_people(self):
        User = get_user_model()
        service_user = ApiToken.issue(self.org, "svc", ["payments:confirm"])[0].service_user
        return {
            "nobody": None,
            "api service user": service_user,
            "inactive staff": User.objects.create_user("gone", password="x", is_staff=True, is_active=False),
            "active non-staff": User.objects.create_user("visitor", password="x"),
            "superuser without staff": User.objects.create_user("root2", password="x", is_superuser=True),
        }

    def test_confirm_refuses_anyone_but_an_active_staff_user(self):
        p = self.waiting()
        for label, user in self.not_people().items():
            with self.subTest(label), self.assertRaises(services.NeedsPerson):
                services.confirm_payment(p, user, "machine")
            self.assertUntouched(p)
        services.confirm_payment(p, self.staff, "seen in the wallet")
        fresh = Payment.objects.get(pk=p.pk)
        self.assertEqual((fresh.state, fresh.confirmed_by), ("confirmed", self.staff))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "paid")

    def test_assign_refuses_anyone_but_an_active_staff_user(self):
        p = self.waiting_in_review()
        for label, user in self.not_people().items():
            with self.subTest(label), self.assertRaises(services.NeedsPerson):
                services.assign_payment(p, self.invoice, user, amount_minor=10000, note="machine")
            self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")
        services.assign_payment(p, self.invoice, self.staff, amount_minor=10000, note="checked the wallet")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "confirmed")
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "paid")

    def test_reject_refuses_anyone_but_an_active_staff_user(self):
        p = self.waiting()
        for label, user in self.not_people().items():
            with self.subTest(label), self.assertRaises(services.NeedsPerson):
                services.reject_payment(p, user, "machine")
            self.assertUntouched(p)
        services.reject_payment(p, self.staff, "not ours")
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "rejected")

    def test_needs_person_is_an_invoice_error(self):
        """Every caller that already turns an InvoiceError into a refusal (the staff pages) keeps doing so."""
        from apps.invoices.errors import InvoiceError

        self.assertTrue(issubclass(services.NeedsPerson, InvoiceError))
        self.assertEqual(services.NeedsPerson.code, "needs_person")


class StaffPagesStillWorkTests(StaffBase):
    """A person on the staff pages confirms and assigns a payment waiting for them."""

    def setUp(self):
        super().setUp()
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})

    def waiting(self, state="detected"):
        return Payment.objects.create(rail="btcpay", network="monero", asset="XMR", rail_account=self.btcpay,
                                      external_id=f"btcpay:S1:{state}", received_amount="0.5", amount_minor=10000,
                                      state=state, invoice=self.invoice if state == "detected" else None,
                                      review_reason="btcpay_late" if state == "needs_review" else "",
                                      needs_staff_confirmation=True)

    def test_staff_confirm(self):
        p = self.waiting()
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/confirm/", {"note": "seen in the wallet"})
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual((Payment.objects.get(pk=p.pk).state, Payment.objects.get(pk=p.pk).confirmed_by),
                         ("confirmed", self.staff))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "paid")

    def test_staff_assign(self):
        p = self.waiting("needs_review")
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": str(self.invoice.pk), "amount": "100.00", "note": "checked the wallet"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        fresh = Payment.objects.get(pk=p.pk)
        self.assertEqual((fresh.state, fresh.invoice_id, fresh.confirmed_by), ("confirmed", self.invoice.pk, self.staff))


class LlmsTxtTests(GatewayBase):
    def test_llms_txt_says_only_a_person_moves_a_waiting_payment_on(self):
        text = " ".join(APIClient().get("/llms.txt").content.decode().split())
        self.assertNotIn("Only a person or an agent holding payments:confirm moves it on", text)
        self.assertIn('A payment waiting for a person ("needs_staff_confirmation": true) moves on only by a person', text)
        self.assertIn('409 "needs_person"', text)
        self.assertNotIn("Monero or Zcash", text)  # the flag decides, not the asset (the owner, 2026-11-09)


class ManualPaymentNeedsAPersonTests(Waiting):
    """F1 (review S1): "staff saw the money arrive" is said by a person. A token asserting that money arrived,
    with no chain or gateway behind it, is the same class as confirming a waiting payment (SEC-2)."""

    def test_a_token_cannot_record_a_manual_payment(self):
        for scopes in (("read", "payments:confirm"), ("admin",)):
            with self.subTest(scopes):
                client, _token = api(self.org, scopes=scopes)
                r = client.post(f"/api/v1/invoices/{self.invoice.pk}/payments",
                                {"amount_minor": 10000, "rail": "bank", "note": "wire seen"}, format="json",
                                HTTP_IDEMPOTENCY_KEY=f"m-{scopes[0]}")
                self.assertEqual((r.status_code, r.json()["error"]), (409, "needs_person"), r.content)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice, rail="bank").exists())
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "sent")
        self.assertFalse(WebhookEvent.objects.filter(type="payment.confirmed").exists())

    def test_the_service_refuses_anyone_but_an_active_staff_user(self):
        for label, user in ServiceGuardTests.not_people(self).items():
            with self.subTest(label), self.assertRaises(services.NeedsPerson):
                services.record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="", user=user)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice, rail="bank").exists())
        p = services.record_manual_payment(self.invoice, amount_minor=10000, rail="bank", note="wire", user=self.staff)
        self.assertEqual((p.state, p.confirmed_by), ("confirmed", self.staff))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "paid")

    def test_llms_txt_says_a_manual_payment_needs_a_person(self):
        text = " ".join(APIClient().get("/llms.txt").content.decode().split())
        self.assertNotIn("payments:confirm: record manual payments", text)
        self.assertIn("POST /api/v1/invoices/{id}/payments", text)
        self.assertIn("only a person records a manual payment", text)
