import time
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django_otp.oath import TOTP

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.payments.models import Declaration, Payment
from apps.payments.services import record_manual_payment
from apps.orgs.testing import add_bank
from apps.staff.models import StaffTOTPDevice
from apps.vault.testing import UnsealedVaultMixin


def token_for(device) -> str:
    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = time.time()
    return f"{t.token():0{device.digits}d}"


class StaffBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        self.device = StaffTOTPDevice.objects.create(user=self.staff, name="t", confirmed=True)
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        add_bank(self.org)
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján", preferred_language="en")
        inv, _ = create_invoice(self.org, self.contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        self.invoice, _v, self.pay = send_invoice(inv)
        OutboxMessage.objects.all().delete()

    def sign_in(self):
        self.client.force_login(self.staff)
        r = self.client.post("/verify/", {"token": token_for(self.device)})
        self.assertEqual(r.status_code, 302, r.content[:300])


class AccessTests(StaffBase):
    def test_staff_pages_require_otp(self):
        urls = ["/", f"/invoices/{self.invoice.pk}/"]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith("/login/"))
        self.client.force_login(self.staff)  # password only, no verified device
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith("/verify/"), r["Location"])
        r = self.client.post(f"/invoices/{self.invoice.pk}/void/", {"reason": "x"})
        self.assertEqual(r.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "sent")

    def test_wrong_token_does_not_verify(self):
        self.client.force_login(self.staff)
        r = self.client.post("/verify/", {"token": "000000"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_non_staff_gets_403_even_with_device(self):
        other = get_user_model().objects.create_user("cust", password="x")
        dev = StaffTOTPDevice.objects.create(user=other, name="t", confirmed=True)
        self.client.force_login(other)
        self.client.post("/verify/", {"token": token_for(dev)})
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_password_login_then_verify(self):
        r = self.client.post("/login/", {"username": "clerk", "password": "pw-clerk-1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/verify/"))
        self.client.post("/verify/", {"token": token_for(self.device)})
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_admin_is_for_verified_superusers_only(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/hidden-admin-xyz/").status_code, 404)  # staff, not superuser
        self.assertEqual(self.client.get("/hidden-admin-xyz/login/").status_code, 404)
        self.sign_in()
        self.assertEqual(self.client.get("/hidden-admin-xyz/").status_code, 404)
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.get("/hidden-admin-xyz/").status_code, 200)

    def test_pages_carry_nonce_csp(self):
        self.sign_in()
        r = self.client.get("/")
        self.assertIn("nonce-", r["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", r["Content-Security-Policy"])
        self.assertNotIn(b" onclick=", r.content)


class PageTests(StaffBase):
    def test_overpaid_flagged_on_needs_person_page(self):
        record_manual_payment(self.invoice, amount_minor=12000, rail="bank", note="", user=self.staff)
        self.sign_in()
        r = self.client.get("/")
        self.assertContains(r, f"/invoices/{self.invoice.pk}/")
        self.assertContains(r, "overpaid")

    def test_needs_person_lists_declarations_and_detected_payments(self):
        Declaration.objects.create(invoice=self.invoice, note="I paid")
        Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=100,
                               received_amount="1", state="detected")
        self.sign_in()
        r = self.client.get("/")
        self.assertContains(r, "Declared")
        self.assertContains(r, "detected")

    def test_record_confirm_reject_void_flow(self):
        self.sign_in()
        base = f"/invoices/{self.invoice.pk}/"
        self.assertContains(self.client.get(base), "Void invoice")
        r = self.client.post(base + "payments/", {"amount": "40.00", "rail": "bank", "note": "n"})
        self.assertEqual(r.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "partially_paid")
        self.assertEqual(self.client.post(base + "payments/", {"amount": "abc", "rail": "bank"}).status_code, 400)
        self.assertEqual(self.client.post(base + "payments/", {"amount": "0.001", "rail": "bank"}).status_code, 400)
        p = Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=6000,
                                   received_amount="60", state="detected")
        q = Payment.objects.create(invoice=self.invoice, rail="bank", asset="EUR", amount_minor=1,
                                   received_amount="0.01", state="detected")
        page = self.client.get(base)
        self.assertContains(page, "Confirm")
        self.client.post(f"/payments/{q.pk}/reject/", {"note": "stray"})
        self.client.post(f"/payments/{p.pk}/confirm/", {"note": ""})
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")
        self.assertEqual(AuditEntry.objects.filter(actor=self.staff).count(), 3)
        self.assertContains(self.client.get(base), "payment.confirm")

    def test_void_requires_reason(self):
        self.sign_in()
        base = f"/invoices/{self.invoice.pk}/void/"
        self.assertEqual(self.client.post(base, {"reason": "  "}).status_code, 400)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "sent")
        self.assertEqual(self.client.post(base, {"reason": "duplicate"}).status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "void")
        self.assertTrue(AuditEntry.objects.filter(action="invoice.void", actor=self.staff).exists())

    def test_get_on_action_urls_not_allowed(self):
        self.sign_in()
        self.assertEqual(self.client.get(f"/invoices/{self.invoice.pk}/void/").status_code, 405)


class CommandTests(StaffBase):
    def test_create_staff_totp(self):
        out = StringIO()
        other = get_user_model().objects.create_user("new", password="x", is_staff=True)
        call_command("create_staff_totp", "new", stdout=out)
        self.assertIn("otpauth://totp/", out.getvalue())
        self.assertTrue(StaffTOTPDevice.objects.filter(user=other, confirmed=True).exists())


class FixRound1Tests(StaffBase):
    def test_limited_login_body_same_for_right_and_wrong_password(self):
        from unittest import mock
        from django.core.cache import caches
        caches["ratelimit"].clear()
        for _ in range(10):
            self.client.post("/login/", {"username": "x", "password": "bad"})
        with mock.patch("django.contrib.auth.authenticate") as auth, \
                mock.patch("django.contrib.auth.forms.authenticate") as auth2:
            a = self.client.post("/login/", {"username": "clerk", "password": "pw-clerk-1"})
            b = self.client.post("/login/", {"username": "clerk", "password": "wrong"})
            self.assertFalse(auth.called or auth2.called)
        self.assertEqual((a.status_code, b.status_code), (429, 429))
        # the CSRF token and the CSP nonce differ per response by design; nothing else may
        strip = lambda r: __import__("re").sub(rb'(csrfmiddlewaretoken" value|nonce)="[^"]+"', b"", r.content)
        self.assertEqual(strip(a), strip(b))
        self.assertNotIn(b"correct username", a.content)

    def test_per_username_login_limit(self):
        from django.core.cache import caches
        from django.test import Client
        caches["ratelimit"].clear()
        # A browser that completed password and TOTP for the account before (audit WEB-2)...
        trusted = Client()
        trusted.force_login(self.staff)
        self.assertEqual(trusted.post("/verify/", {"token": token_for(self.device)}).status_code, 302)
        trusted.cookies.pop("sessionid")
        for i in range(10):
            self.client.post("/login/", {"username": "clerk", "password": "bad"}, REMOTE_ADDR=f"10.0.0.{i}")
        r = self.client.post("/login/", {"username": "clerk", "password": "pw-clerk-1"}, REMOTE_ADDR="10.9.9.9")
        self.assertEqual(r.status_code, 429)  # any other browser is held by the account's bucket
        # ...carries the device cookie, and is not.
        r = trusted.post("/login/", {"username": "clerk", "password": "pw-clerk-1"}, REMOTE_ADDR="10.9.9.8")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/verify/"))

    def test_amount_cap_and_garbage(self):
        self.sign_in()
        url = f"/invoices/{self.invoice.pk}/payments/"
        for bad in ("1e30", "1e999999999", "nan", "99999999999999", "-1"):
            self.assertEqual(self.client.post(url, {"amount": bad, "rail": "bank"}).status_code, 400, bad)
        self.assertEqual(Payment.objects.count(), 0)

    def test_cannot_void_with_confirmed_money_and_reverse_button(self):
        self.sign_in()
        base = f"/invoices/{self.invoice.pk}/"
        p = record_manual_payment(self.invoice, amount_minor=4000, rail="bank", note="", user=self.staff)
        page = self.client.get(base)
        self.assertNotContains(page, "Void invoice")
        self.assertContains(page, "Reverse")
        self.assertEqual(self.client.post(base + "void/", {"reason": "x"}).status_code, 409)
        self.assertEqual(self.client.post(f"/payments/{p.pk}/reverse/", {"reason": ""}).status_code, 400)
        self.assertEqual(self.client.post(f"/payments/{p.pk}/reverse/", {"reason": "bounced"}).status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.state, "reversed")
        self.assertContains(self.client.get(base), "40.00 EUR")  # amount_minor shown formatted

    def test_dismiss_declaration_view(self):
        d = Declaration.objects.create(invoice=self.invoice, note="claim")
        self.sign_in()
        r = self.client.post(f"/declarations/{d.pk}/dismiss/", {"reason": ""})
        self.assertEqual(r.status_code, 302)
        d.refresh_from_db()
        self.assertEqual(d.state, "dismissed")
        self.assertNotContains(self.client.get("/"), f"/invoices/{self.invoice.pk}/")

    def test_session_settings(self):
        from django.conf import settings
        self.assertEqual(settings.SESSION_COOKIE_AGE, 8 * 60 * 60)
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

    def test_replace_totp(self):
        call_command("create_staff_totp", "clerk", "--replace", stdout=StringIO())
        self.assertEqual(StaffTOTPDevice.objects.filter(user=self.staff).count(), 1)
        self.assertFalse(StaffTOTPDevice.objects.filter(pk=self.device.pk).exists())


class OutboxOnNeedsPersonTests(StaffBase):
    """I4: undelivered e-mail is listed for a person, with a retry button."""

    def test_failed_and_old_queued_messages_are_listed_and_retried(self):
        import datetime

        from django.utils import timezone

        from apps.notify import services as notify

        ctx = {"ref": "2026-0001", "name": "A", "total": "1 EUR", "admin_url": "x"}
        failed = notify.enqueue("declared_staff", to=["a@example.com"], invoice=self.invoice, language="en", context=ctx)
        stuck = notify.enqueue("receipt", to=["a@example.com"], invoice=self.invoice, language="en", context=ctx)
        fresh = notify.enqueue("partial", to=["a@example.com"], invoice=self.invoice, language="en", context=ctx)
        OutboxMessage.objects.filter(pk=failed.pk).update(state="failed", attempts=9)
        OutboxMessage.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - datetime.timedelta(hours=2))
        self.sign_in()
        page = self.client.get("/")
        self.assertContains(page, f"/outbox/{failed.pk}/retry/")
        self.assertContains(page, f"/outbox/{stuck.pk}/retry/")
        self.assertNotContains(page, f"/outbox/{fresh.pk}/retry/")  # under an hour old: not yet a problem
        self.assertNotContains(page, "a@example.com")
        self.assertEqual(self.client.get(f"/outbox/{failed.pk}/retry/").status_code, 405)
        r = self.client.post(f"/outbox/{failed.pk}/retry/")
        self.assertEqual(r.status_code, 302)
        m = OutboxMessage.objects.get(pk=failed.pk)
        self.assertEqual((m.state, m.attempts), ("queued", 0))
        self.assertTrue(AuditEntry.objects.filter(action="outbox.retry", actor=self.staff,
                                                  object_pk=str(failed.pk)).exists())


class UsFlagOnNeedsPersonTests(StaffBase):
    """I6: a contact that became a US tax resident has open invoices from the wrong issuer."""

    def test_open_invoices_of_flagged_contacts_are_listed(self):
        from apps.invoices.models import Invoice
        from apps.orgs.testing import add_bank

        us = Organization.objects.create(slug="us", is_us_resident_issuer=True)
        add_bank(us)
        draft = create_invoice(self.org, self.contact, currency="EUR",
                               lines=[{"name": "S", "quantity": 1, "unit_price_minor": 100}])[0]
        paid = create_invoice(self.org, self.contact, currency="EUR",
                              lines=[{"name": "S", "quantity": 1, "unit_price_minor": 100}])[0]
        Invoice.objects.filter(pk=paid.pk).update(status="paid")
        self.sign_in()
        section = lambda: self.client.get("/").content.decode().split("US tax residents")[1].split("<h2>")[0]
        self.assertNotIn(f"/invoices/{self.invoice.pk}/", section())
        self.contact.us_tax_resident = "yes"
        self.contact.save()
        right = create_invoice(self.org, self.contact, currency="EUR",
                               lines=[{"name": "S", "quantity": 1, "unit_price_minor": 100}])[0]
        self.assertEqual(right.org, us)
        listed = section()
        self.assertIn(f"/invoices/{self.invoice.pk}/", listed)  # sent, from eu
        self.assertIn(f"/invoices/{draft.pk}/", listed)
        self.assertNotIn(f"/invoices/{paid.pk}/", listed)
        self.assertNotIn(f"/invoices/{right.pk}/", listed)


class NeedsPersonTitleTests(StaffBase):
    def test_needs_person_title_is_only_the_page_name(self):
        import re

        self.sign_in()
        html = self.client.get("/").content.decode()
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
        self.assertEqual(title.strip(), "Needs a person")
        main = re.search(r"<main.*?</main>", html, re.S).group(0)
        self.assertIn("E-mail not delivered", main)
        self.assertIn("Declared payments", main)


class DismissFailedTests(StaffBase):
    """A person may close a failed e-mail or webhook delivery for good: audited (who and why, never
    to whom), never sent afterwards, and the health check counts it no more."""

    def failed_letter(self):
        from apps.invoices.services import queue_invoice_letter

        msg = queue_invoice_letter(self.invoice, "a@example.com", "view-token-x", "pay-token-x")
        OutboxMessage.objects.filter(pk=msg.pk).update(state="failed", attempts=9, last_error="SMTPException")
        return msg

    def failed_delivery(self):
        from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

        endpoint = WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/secret-path?tok=1",
                                                  secret="s", events=["*"])
        event = WebhookEvent.objects.create(type="invoice.sent", org=self.org)
        return WebhookDelivery.objects.create(event=event, endpoint=endpoint, state="failed", attempts=9,
                                              last_error_kind="http_503")

    def test_dismissing_a_failed_letter_cancels_it_for_good(self):
        from django.core import mail

        from apps.notify import services as notify

        msg = self.failed_letter()
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn(f"/outbox/{msg.pk}/dismiss/", page)
        self.assertEqual(self.client.get(f"/outbox/{msg.pk}/dismiss/").status_code, 405)
        r = self.client.post(f"/outbox/{msg.pk}/dismiss/", {"reason": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(OutboxMessage.objects.get(pk=msg.pk).state, "failed")
        r = self.client.post(f"/outbox/{msg.pk}/dismiss/", {"reason": "Client has the PDF from the portal"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(OutboxMessage.objects.get(pk=msg.pk).state, "cancelled")
        entry = AuditEntry.objects.get(action="outbox.dismiss", object_pk=str(msg.pk))
        self.assertEqual(entry.actor, self.staff)
        self.assertEqual(entry.detail, {"kind": "invoice", "reason": "Client has the PDF from the portal"})
        self.assertNotIn("a@example.com", str(entry.detail))
        self.assertNotIn(f"/outbox/{msg.pk}/", self.client.get("/").content.decode())
        # never resent: neither a retry (the button, the admin action) nor the outbox brings it back
        self.assertEqual(self.client.post(f"/outbox/{msg.pk}/retry/").status_code, 302)
        self.assertEqual(notify.retry_now(OutboxMessage.objects.filter(pk=msg.pk)), 0)
        notify.flush_outbox()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutboxMessage.objects.get(pk=msg.pk).state, "cancelled")
        r = self.client.post(f"/outbox/{msg.pk}/dismiss/", {"reason": "again"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(AuditEntry.objects.filter(action="outbox.dismiss").count(), 1)

    def test_only_a_failed_message_can_be_dismissed(self):
        msg = self.failed_letter()
        OutboxMessage.objects.filter(pk=msg.pk).update(state="queued", attempts=2,
                                                       created_at=self.invoice.sent_at.replace(year=2020))
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn(f"/outbox/{msg.pk}/retry/", page)  # an old queued one is listed ...
        self.assertNotIn(f"/outbox/{msg.pk}/dismiss/", page)  # ... but cannot be dismissed
        self.assertEqual(self.client.post(f"/outbox/{msg.pk}/dismiss/", {"reason": "x"}).status_code, 409)
        self.assertEqual(OutboxMessage.objects.get(pk=msg.pk).state, "queued")
        self.assertFalse(AuditEntry.objects.filter(action="outbox.dismiss").exists())

    def test_dismissing_a_failed_webhook_delivery(self):
        from apps.webhooks import delivery as webhook_delivery
        from apps.webhooks.models import WebhookDelivery

        d = self.failed_delivery()
        self.sign_in()
        self.assertIn(f"/webhooks/{d.pk}/dismiss/", self.client.get("/").content.decode())
        self.assertEqual(self.client.post(f"/webhooks/{d.pk}/dismiss/", {}).status_code, 400)
        r = self.client.post(f"/webhooks/{d.pk}/dismiss/", {"reason": "Receiver retired"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WebhookDelivery.objects.get(pk=d.pk).state, "cancelled")
        entry = AuditEntry.objects.get(action="webhook.dismiss", object_pk=str(d.pk))
        self.assertEqual(entry.actor, self.staff)
        self.assertEqual(entry.detail, {"endpoint": d.endpoint_id, "event": "invoice.sent",
                                        "reason": "Receiver retired"})
        self.assertNotIn("hook.example", str(entry.detail))
        self.assertEqual(webhook_delivery.retry_now(WebhookDelivery.objects.filter(pk=d.pk)), 0)
        self.assertEqual(self.client.post(f"/webhooks/{d.pk}/dismiss/", {"reason": "x"}).status_code, 409)

    def test_health_is_ok_again_after_dismissing(self):
        import io

        from django.core.management import call_command

        from apps.core.tests_health import beat

        beat()
        msg, d = self.failed_letter(), self.failed_delivery()
        body = self.client.get("/healthz").json()
        self.assertEqual((body["status"], body["outbox_failed"], body["webhooks_failed"]), ("degraded", 1, 1))
        self.sign_in()
        self.client.post(f"/outbox/{msg.pk}/dismiss/", {"reason": "sent by hand"})
        self.client.post(f"/webhooks/{d.pk}/dismiss/", {"reason": "receiver retired"})
        body = self.client.get("/healthz").json()
        self.assertEqual((body["status"], body["outbox_failed"], body["webhooks_failed"]), ("ok", 0, 0))
        out = io.StringIO()
        call_command("healthcheck", "--no-network", stdout=out)  # exits 1 (SystemExit) on any problem
        self.assertEqual(out.getvalue(), "")


class KeepOverpaymentTests(StaffBase):
    """Owner 2026-11-09: a few dollars too much is kept, not refunded; a person closes the item, and
    the invoice stays overpaid because that is what the money says."""

    def overpay(self, amount=12000):
        record_manual_payment(self.invoice, amount_minor=amount, rail="bank", note="", user=self.staff)
        self.invoice.refresh_from_db()

    def keep(self, note="2 EUR too much, not worth a refund"):
        return self.client.post(f"/invoices/{self.invoice.pk}/keep-overpayment/", {"note": note})

    def test_keeping_it_takes_the_invoice_off_the_needs_person_page(self):
        self.overpay()
        self.sign_in()
        self.assertContains(self.client.get(f"/invoices/{self.invoice.pk}/"), "keep-overpayment")
        r = self.keep()
        self.assertEqual(r.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "overpaid")
        self.assertEqual(self.invoice.overpayment_kept_minor, 12000)
        self.assertEqual(self.invoice.overpayment_kept_by, self.staff)
        self.assertIsNotNone(self.invoice.overpayment_kept_at)
        entry = AuditEntry.objects.get(action="invoice.overpayment_keep")
        self.assertEqual(entry.detail["note"], "2 EUR too much, not worth a refund")
        self.assertEqual(entry.detail["excess_minor"], 2000)
        self.assertNotContains(self.client.get("/"), f"/invoices/{self.invoice.pk}/")
        page = self.client.get(f"/invoices/{self.invoice.pk}/")
        self.assertContains(page, "Overpayment kept")
        self.assertNotContains(page, "keep-overpayment")

    def test_a_note_is_required(self):
        self.overpay()
        self.sign_in()
        self.assertEqual(self.keep(note="  ").status_code, 409)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.overpayment_kept_at)

    def test_only_an_overpaid_invoice_can_have_its_overpayment_kept(self):
        self.sign_in()
        self.assertEqual(self.keep().status_code, 409)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.overpayment_kept_at)
        self.assertFalse(AuditEntry.objects.filter(action="invoice.overpayment_keep").exists())

    def test_more_money_after_keeping_brings_it_back_to_a_person(self):
        self.overpay()
        self.sign_in()
        self.keep()
        record_manual_payment(self.invoice, amount_minor=500, rail="bank", note="", user=self.staff)
        self.assertContains(self.client.get("/"), f"/invoices/{self.invoice.pk}/")

    def test_staff_only(self):
        self.overpay()
        r = self.keep()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/login/"))
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.overpayment_kept_at)
