import copy
import datetime
import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.payments.models import Payment
from apps.quotes import services as quotes
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails import http
from apps.rails.gateways import btcpay, registry
from apps.rails.models import RailAccount, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

FIX = Path(__file__).parent / "fixtures" / "btcpay"
SECRET = "whsec-test"
INV_ID = "Xg3SvYtDbiPfbCpJgYdbPv"


def load(name):
    return json.loads((FIX / name).read_text())


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.acct = RailAccount.objects.create(
            org=self.org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR", "ZEC"],
            config={"url": "https://btcpay.example/", "store_id": "ST0RE1", "speed_policy": "MediumSpeed"},
            credentials={"api_key": "KEY", "webhook_secret": SECRET})
        contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján", preferred_language="sk")
        inv = create_invoice(self.org, contact, currency="EUR", title="T", notes="N",
                             lines=[{"name": "S", "quantity": 1, "unit_price_minor": 10000}])[0]
        self.invoice = send_invoice(inv, email_client=False)[0]
        self.calls = []
        self.status = load("invoice_created.json")
        self.methods = load("payment_methods_btc.json")

    def fake_post(self, url, payload, *, headers=None, service, timeout=10, guard=None):
        self.calls.append(("POST", url, payload, dict(headers or {})))
        return load("invoice_created.json")

    def fake_get(self, url, *, params=None, headers=None, service, timeout=10, guard=None):
        self.calls.append(("GET", url, None, dict(headers or {})))
        if isinstance(self.status, Exception):
            raise self.status
        return copy.deepcopy(self.methods if url.endswith("payment-methods") else self.status)

    def patch_http(self):
        for name, fn in (("post_json", self.fake_post), ("get_json", self.fake_get)):
            p = mock.patch(f"apps.rails.http.{name}", side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def quote(self, asset="BTC", network="bitcoin", amount=123456, ext=INV_ID, **kw):
        return Quote.objects.create(
            invoice=self.invoice, rail_account=self.acct, network=network, asset=asset, address="",
            amount_base_units=str(amount), min_accept_base_units=str(amount), ceiling_base_units=str(amount),
            invoice_amount_minor=10000, issued_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(minutes=30), external_id=ext, **kw)

    def set_state(self, status, extra="None", paid="0", due=None, method=None):
        self.status = {**load("invoice_created.json"), "status": status, "additionalStatus": extra}
        self.methods = copy.deepcopy(self.methods)
        self.methods[0].update(totalPaid=paid, paymentMethodPaid=paid)  # one method: both fields agree
        if due is not None:
            self.methods[0]["amount"] = due
        if method:
            self.methods[0]["paymentMethodId"] = method

    def poll(self, quote):
        self.patch_http()
        registry.for_account(RailAccount.objects.get(pk=self.acct.pk)).poll(Quote.objects.get(pk=quote.pk))

    def payment(self):
        return Payment.objects.get(external_id=f"btcpay:ST0RE1:{INV_ID}")


class GatewayTests(Base):
    def test_create_target_fills_quote(self):
        self.patch_http()
        plain = Invoice.objects.only(*__import__("apps.invoices.services", fromlist=["x"]).INVOICE_PLAIN_FIELDS).get(
            pk=self.invoice.pk)
        q = Quote(invoice=plain, rail_account=self.acct, network="bitcoin", asset="BTC", address="",
                  invoice_amount_minor=10000, issued_at=timezone.now(),
                  expires_at=timezone.now() + datetime.timedelta(hours=3))
        registry.for_account(self.acct).create_target(q)
        method, url, body, headers = self.calls[0]
        self.assertEqual(url, "https://btcpay.example/api/v1/stores/ST0RE1/invoices")
        self.assertEqual(headers["Authorization"], "token KEY")
        self.assertEqual((body["amount"], body["currency"]), ("100.00", "EUR"))
        self.assertEqual(body["checkout"], {"expirationMinutes": 30, "speedPolicy": "MediumSpeed",
                                            "paymentMethods": ["BTC"]})
        self.assertEqual(body["metadata"]["invoicePk"], plain.pk)
        self.assertEqual((q.external_id, q.checkout_url), (INV_ID, "https://btcpay.example/i/" + INV_ID))
        self.assertEqual((q.amount_base_units, q.rate, q.rate_source), ("123456", "81000.12", "btcpay"))
        self.assertTrue(q.payment_uri.startswith("bitcoin:bc1q"))
        self.assertEqual(q.expires_at, datetime.datetime.fromtimestamp(1762086600, datetime.timezone.utc))

    def test_create_quote_end_to_end_and_unreachable(self):
        self.patch_http()
        q = quotes.create_quote(self.invoice, self.acct, "bitcoin", "BTC")
        self.assertEqual((q.state, q.external_id, q.amount_base_units), ("open", INV_ID, "123456"))
        with mock.patch("apps.rails.http.post_json", side_effect=http.Unavailable("btcpay", "timeout")):
            with self.assertLogs("apps.quotes.services", "WARNING"), self.assertRaises(QuoteError) as cm:
                quotes.create_quote(self.invoice, self.acct, "bitcoin", "BTC", refresh=True)
        self.assertEqual(cm.exception.code, "rail_unavailable")

    def test_sealed_vault_refuses_quote(self):
        acct = RailAccount.objects.only("id", "kind", "config").get(pk=self.acct.pk)
        vault.seal()
        with self.assertRaises(QuoteError) as cm:
            registry.for_account(acct)
        self.assertEqual(cm.exception.code, "rail_unavailable")

    def test_processing_then_settled_btc_confirms(self):
        q = self.quote()
        self.set_state("Processing", paid="0.00123456")
        self.poll(q)
        self.assertEqual(self.payment().state, "detected")
        self.set_state("Settled", paid="0.00123456")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.amount_minor, p.network), ("confirmed", 10000, "bitcoin"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

    def test_settled_xmr_is_confirmed_like_bitcoin(self):
        """Owner 2026-11-09: when the chain proves it (BTCPay Settled at the store's threshold) no person is needed."""
        self.methods = load("payment_methods_xmr.json")
        q = self.quote("XMR", "monero", 512345678901)
        self.set_state("Settled", paid="0.512345678901")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.needs_staff_confirmation, p.network), ("confirmed", False, "monero"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

    def test_settled_xmr_short_of_the_amount_goes_to_review(self):
        self.methods = load("payment_methods_xmr.json")
        q = self.quote("XMR", "monero", 512345678901)
        self.set_state("Settled", paid="0.4")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason), ("needs_review", "btcpay_partial"))

    def test_paid_over_sets_refund(self):
        q = self.quote()
        self.set_state("Settled", "PaidOver", paid="0.00133456")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.amount_minor, p.refund_due_base_units), ("confirmed", 10000, "10000"))

    def test_overpaid_beyond_the_ceiling_goes_to_review(self):
        q = self.quote()  # 123456 sats due; the organisation's ceiling is 10 % over: 135801
        self.set_state("Settled", "PaidOver", paid="0.00140000")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason, p.amount_minor, p.refund_due_base_units),
                         ("needs_review", "overpaid_beyond_ceiling", 10000, "16544"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")
        self.assertEqual(Invoice.objects.filter(pk=self.invoice.pk).values_list("status", flat=True).get(), "sent")

    def test_overpaid_up_to_the_ceiling_confirms_and_the_ceiling_is_the_organisations(self):
        q = self.quote()
        self.set_state("Settled", "PaidOver", paid="0.00135801")  # exactly at the ceiling
        self.poll(q)
        self.assertEqual((self.payment().state, self.payment().refund_due_base_units), ("confirmed", "12345"))
        Payment.objects.all().delete()
        Quote.objects.filter(pk=q.pk).update(state="open", closed_at=None)
        Invoice.objects.filter(pk=self.invoice.pk).update(status="sent", paid_at=None)
        Organization.objects.filter(pk=self.org.pk).update(settings={"overpay_ceiling_bps": 500})
        self.set_state("Settled", "None", paid="0.00130000")  # 5.3 % over a 5 % ceiling, flag or not
        self.poll(q)
        self.assertEqual((self.payment().state, self.payment().review_reason),
                         ("needs_review", "overpaid_beyond_ceiling"))

    def test_partial_goes_to_review_with_proportional_amount(self):
        q = self.quote()
        self.set_state("Expired", "PaidPartial", paid="0.00061728")  # exactly half of 0.00123456
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason, p.amount_minor), ("needs_review", "btcpay_partial", 5000))
        self.set_state("Expired", "PaidPartial", paid="0.00041152")  # a third: 3333.33 rounds down
        Payment.objects.all().delete()
        self.poll(q)
        self.assertEqual(self.payment().amount_minor, 3333)

    def test_marked_is_review(self):
        q = self.quote()
        self.set_state("Settled", "Marked")
        self.poll(q)
        self.assertEqual((self.payment().state, self.payment().review_reason), ("needs_review", "btcpay_marked"))

    def test_late_and_invalid_are_review(self):
        q = self.quote()
        self.set_state("Expired", "PaidLate", paid="0.00123456")
        self.poll(q)
        self.assertEqual(self.payment().review_reason, "btcpay_late")
        Payment.objects.all().delete()
        self.set_state("Invalid", paid="0.00123456")
        self.poll(q)
        self.assertEqual(self.payment().review_reason, "btcpay_invalid")

    def test_network_mismatch_is_review_never_confirmation(self):
        q = self.quote()
        self.set_state("Settled", paid="0.00123456", method="ZEC-CHAIN")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason), ("needs_review", "btcpay_invalid"))

    def test_expired_nothing_paid_expires_quote(self):
        q = self.quote()
        self.set_state("Expired")
        self.poll(q)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "expired")
        self.assertFalse(Payment.objects.exists())

    def test_unreachable_btcpay_records_health_not_unpaid(self):
        q = self.quote()
        self.status = http.Unavailable("btcpay", "timeout")
        self.patch_http()
        btcpay.btcpay_poll()
        h = WatcherHealth.objects.get(name=f"btcpay:{self.acct.pk}")
        self.assertEqual((h.consecutive_failures, h.last_error_kind), (1, "timeout"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")
        self.assertFalse(Payment.objects.exists())
        self.status = load("invoice_created.json")
        btcpay.btcpay_poll()
        self.assertIsNone(WatcherHealth.objects.get(name=f"btcpay:{self.acct.pk}").failing_since)

    def test_xmr_paid_on_a_btc_quote_goes_to_review(self):
        q = self.quote()
        self.methods = load("payment_methods_btc_xmr.json")  # BTC totalPaid>0 only by conversion
        self.status = {**load("invoice_created.json"), "status": "Settled"}
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason, p.network), ("needs_review", "btcpay_invalid", "monero"))
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")

    def test_an_unrequested_method_listed_goes_to_review(self):
        q = self.quote()
        self.methods = load("payment_methods_btc_xmr.json")
        self.methods[1]["paymentMethodPaid"] = "0"
        self.status = {**load("invoice_created.json"), "status": "Settled"}
        self.poll(q)
        self.assertEqual(self.payment().review_reason, "btcpay_invalid")

    def test_settled_btc_underpaid_goes_to_review(self):
        q = self.quote()
        self.set_state("Settled", paid="0.00061728")  # half; the store's tolerance must not decide
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.review_reason, p.amount_minor), ("needs_review", "btcpay_partial", 5000))

    def test_partial_credit_floors(self):
        q = self.quote()
        self.set_state("Expired", "PaidPartial", paid="0.00082304")  # 2/3: 6666.67
        self.poll(q)
        self.assertEqual(self.payment().amount_minor, 6666)

    def test_processing_on_another_network_goes_to_review(self):
        q = self.quote()
        self.set_state("Processing", paid="0.00123456", method="ZEC-CHAIN")
        self.poll(q)
        self.assertEqual((self.payment().state, self.payment().review_reason), ("needs_review", "btcpay_invalid"))

    def test_refund_is_set_only_with_paid_over(self):
        q = self.quote()
        self.set_state("Settled", "None", paid="0.00133456")
        self.poll(q)
        p = self.payment()
        self.assertEqual((p.state, p.refund_due_base_units), ("confirmed", ""))

    def test_cancelled_quote_is_polled_for_24h(self):
        now = timezone.now()
        q = self.quote(state="cancelled", closed_at=now - datetime.timedelta(hours=1))
        old = self.quote(ext="old", state="cancelled", closed_at=now - datetime.timedelta(hours=30))
        Quote.objects.filter(pk=old.pk).update(expires_at=now - datetime.timedelta(hours=31),
                                               read_at=now - datetime.timedelta(hours=29))  # read after expiry
        self.set_state("Settled", paid="0.00123456")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "confirmed")
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")
        self.assertTrue(all("old" not in c[1] for c in self.calls) and old.pk)

    def test_vault_sealed_mid_pass_is_silent(self):
        self.quote()

        def seal_then_refuse(account):
            vault.seal()
            raise QuoteError("rail_unavailable")

        with mock.patch.object(registry, "for_account", side_effect=seal_then_refuse):
            self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertFalse(WatcherHealth.objects.exists())

    def test_poll_skipped_while_sealed(self):
        self.quote()
        self.patch_http()
        vault.seal()
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(WatcherHealth.objects.exists())

    def test_three_failing_quotes_cannot_hide_a_paid_one_for_good(self):
        for ext in ("bad1", "bad2", "bad3"):
            self.quote(ext=ext)
        good = self.quote()
        self.set_state("Settled", paid="0.00123456")
        self.patch_http()
        real_state = registry.for_account

        class Gateway:
            speed = "MediumSpeed"

            def __init__(self, inner):
                self.inner = inner

            def poll(self, quote):
                if quote.external_id.startswith("bad"):
                    raise http.Unavailable("btcpay", "timeout")
                return self.inner.poll(quote)

        start = timezone.now()
        passes = 0
        with mock.patch.object(registry, "for_account", side_effect=lambda a: Gateway(real_state(a))):
            while Quote.objects.get(pk=good.pk).state != "paid" and passes < 4:
                with mock.patch.object(timezone, "now", return_value=start + datetime.timedelta(seconds=60 * passes)):
                    btcpay.btcpay_poll()
                passes += 1
        self.assertEqual(Quote.objects.get(pk=good.pk).state, "paid")

    def test_dead_btcpay_is_asked_three_times_then_the_pass_stops(self):
        for i in range(5):
            self.quote(ext=f"dead{i}")
        self.patch_http()
        self.status = http.Unavailable("btcpay", "timeout")
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertEqual(len(self.calls), 3)
        h = WatcherHealth.objects.get(name=f"btcpay:{self.acct.pk}")
        self.assertEqual((h.consecutive_failures, h.last_error_kind), (1, "timeout"))

    def test_poll_clears_check_soon_and_skips_old_expired(self):
        q = self.quote(check_soon=True)
        old = self.quote(ext="old", state="expired")
        Quote.objects.filter(pk=old.pk).update(expires_at=timezone.now() - datetime.timedelta(days=2),
                                               read_at=timezone.now() - datetime.timedelta(days=1))
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertFalse(Quote.objects.get(pk=q.pk).check_soon)
        self.assertTrue(all(INV_ID in c[1] for c in self.calls))


class StrandedPaymentTests(Base):
    """A gateway quote whose money may still be on its way is polled whatever its age and never
    released (final review I2)."""

    def test_processing_payment_is_still_watched_after_25_hours_and_confirms(self):
        q = self.quote()
        self.set_state("Processing", paid="0.00123456")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "detected")
        # The confirmations take longer than the day an amount is held.
        long_ago = timezone.now() - datetime.timedelta(hours=25)
        Quote.objects.filter(pk=q.pk).update(state="expired", expires_at=long_ago, closed_at=long_ago)
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "expired")
        self.set_state("Settled", paid="0.00123456")
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "confirmed")
        self.assertEqual(Invoice.objects.filter(pk=self.invoice.pk).values_list("status", flat=True).get(), "paid")

    def test_a_quote_never_read_since_it_expired_is_watched_until_it_is(self):
        long_ago = timezone.now() - datetime.timedelta(hours=25)
        q = self.quote(state="expired", closed_at=long_ago)  # BTCPay was down, or the vault sealed, all day
        Quote.objects.filter(pk=q.pk).update(expires_at=long_ago)
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)
        self.set_state("Expired")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertGreaterEqual(Quote.objects.get(pk=q.pk).read_at, long_ago)
        # Read since it expired, nothing seen: it is released and no longer asked about.
        self.assertEqual(quotes.release_quotes(timezone.now()), 1)
        self.calls.clear()
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertEqual(self.calls, [])

    def test_a_quote_read_only_before_it_expired_is_still_watched(self):
        long_ago = timezone.now() - datetime.timedelta(hours=25)
        q = self.quote(state="cancelled", closed_at=long_ago)
        Quote.objects.filter(pk=q.pk).update(expires_at=long_ago, read_at=long_ago - datetime.timedelta(minutes=1))
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)
        self.set_state("Settled", paid="0.00123456")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "confirmed")

    def test_a_failed_read_does_not_count(self):
        long_ago = timezone.now() - datetime.timedelta(hours=25)
        q = self.quote(state="expired", closed_at=long_ago)
        Quote.objects.filter(pk=q.pk).update(expires_at=long_ago)
        self.status = http.Unavailable("btcpay", "timeout")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertIsNone(Quote.objects.get(pk=q.pk).read_at)
        self.assertEqual(quotes.release_quotes(timezone.now()), 0)


class WebhookTests(Base):
    def url(self, pk=None):
        return f"/hooks/btcpay/{pk or self.acct.pk}/"

    def post(self, body, secret=SECRET, pk=None, sig=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {}
        sig = sig if sig is not None else "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if sig:
            headers["HTTP_BTCPAY_SIG"] = sig
        return self.client.post(self.url(pk), raw, content_type="application/json", **headers)

    def test_webhook_bad_signature_401(self):
        q = self.quote()
        with self.assertLogs("apps.rails.views", "WARNING") as logs:
            self.assertEqual(self.post({"invoiceId": INV_ID}, secret="wrong").status_code, 401)
            self.assertEqual(self.post({"invoiceId": INV_ID}, sig="").status_code, 401)
        self.assertNotIn(INV_ID, "".join(logs.output))
        self.assertFalse(Quote.objects.get(pk=q.pk).check_soon)

    def test_webhook_good_signature_only_flags_check_soon(self):
        q = self.quote()
        other = self.quote(ext="other")
        r = self.post({"invoiceId": INV_ID, "type": "InvoiceSettled", "status": "Settled"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Quote.objects.get(pk=q.pk).check_soon)
        self.assertFalse(Quote.objects.get(pk=other.pk).check_soon)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "open")

    def test_webhook_flags_a_cancelled_quote(self):
        cancelled = self.quote(state="cancelled", closed_at=timezone.now())
        paid = self.quote(ext="paid", state="paid")
        released = self.quote(ext="rel", state="released")
        self.assertEqual(self.post({"invoiceId": INV_ID}).status_code, 200)
        self.assertTrue(Quote.objects.get(pk=cancelled.pk).check_soon)
        for other, ext in ((paid, "paid"), (released, "rel")):
            self.post({"invoiceId": ext})
            self.assertFalse(Quote.objects.get(pk=other.pk).check_soon)

    def test_webhook_flags_a_closed_quote_whose_payment_is_still_detected(self):
        q = self.quote(state="released")  # released before the fix that keeps such a quote
        Payment.objects.create(invoice=self.invoice, quote=q, rail="btcpay", network="bitcoin", asset="BTC",
                               amount_minor=10000, received_amount=Decimal("0.00123456"), state="detected",
                               external_id=f"btcpay:ST0RE1:{INV_ID}", rail_account=self.acct)
        self.assertEqual(self.post({"invoiceId": INV_ID}).status_code, 200)
        self.assertTrue(Quote.objects.get(pk=q.pk).check_soon)
        self.set_state("Settled", paid="0.00123456")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "confirmed")

    def test_webhook_empty_secret_refused(self):
        RailAccount.objects.filter(pk=self.acct.pk).update()
        acct = RailAccount.objects.get(pk=self.acct.pk)
        acct.credentials = {"api_key": "KEY", "webhook_secret": ""}
        acct.save()
        q = self.quote()
        with self.assertLogs("apps.rails.views", "WARNING") as logs:
            self.assertEqual(self.post({"invoiceId": INV_ID}, secret="").status_code, 401)
        self.assertIn("bad signature", "".join(logs.output))
        self.assertFalse(Quote.objects.get(pk=q.pk).check_soon)

    def test_webhook_compares_with_compare_digest(self):
        self.quote()
        with mock.patch("apps.rails.views.hmac.compare_digest", wraps=hmac.compare_digest) as cd:
            self.assertEqual(self.post({"invoiceId": INV_ID}).status_code, 200)
        self.assertTrue(cd.called)

    def test_webhook_rate_limit_arguments(self):
        with mock.patch("apps.rails.views.rate_limited", return_value=False) as rl:
            self.post({"invoiceId": INV_ID})
        rl.assert_called_once_with("btcpay-hook", "127.0.0.1", 120, 60)

    def test_webhook_unknowns_answer_200_and_change_nothing(self):
        q = self.quote()
        self.assertEqual(self.post({"invoiceId": "nope"}).status_code, 200)
        self.assertEqual(self.post({"invoiceId": INV_ID}, pk=999999).status_code, 200)
        self.assertEqual(self.post(b"not json").status_code, 200)
        self.assertEqual(self.post([1, 2]).status_code, 200)
        self.assertFalse(Quote.objects.get(pk=q.pk).check_soon)

    def test_webhook_post_only_and_size_limit(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)
        self.assertEqual(self.post(b"x" * (1024 * 1024 + 1)).status_code, 413)

    def test_webhook_rate_limited(self):
        with mock.patch("apps.rails.views.rate_limited", return_value=True):
            self.assertEqual(self.post({"invoiceId": INV_ID}).status_code, 429)

    def test_webhook_503_while_sealed(self):
        vault.seal()
        r = self.post({"invoiceId": INV_ID})
        self.assertEqual((r.status_code, r.json()), (503, {"error": "vault_sealed"}))
