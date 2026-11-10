"""Needs a person for crypto: reviews, XMR/ZEC confirmation, refunds, blind watchers."""
import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Declaration, Payment
from apps.payments.services import assign_payment, confirm_payment
from apps.rails import health, jobs
from apps.rails.models import PendingTransfer, RailAccount, ScanGap, WatchCursor, WatcherHealth
from apps.rails.networks import NETWORKS
from apps.staff.tests import StaffBase

TX = "0x" + "1f" * 32
PAYER = "0x" + "cd" * 20
USDC = NETWORKS["arbitrum"].assets["USDC"]


class CryptoBase(StaffBase):
    def setUp(self):
        super().setUp()
        self.acct = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20,
                                               networks=["arbitrum"], assets=["USDC", "ETH"])

    def review(self, *, invoice=True, minor=0, reason="no_matching_invoice", **kw):
        fields = dict(rail="evm", network="arbitrum", asset="USDC", amount_minor=minor,
                      received_amount="108.27", tx_hash=TX, state="needs_review", review_reason=reason,
                      amount_base_units="108270000", invoice=self.invoice if invoice else None,
                      rail_account=self.acct, final=True)
        fields.update(kw)
        return Payment.objects.create(**fields)

    def other_org(self):
        """A second organization whose first invoice has the same number as ours."""
        org = Organization.objects.create(slug="us", legal_name="Other LLC", address="Elm 2")
        add_bank(org)
        contact, _ = find_or_create_contact(org, "b@example.com", legal_name="Bob", preferred_language="en")
        inv, _ = create_invoice(org, contact, currency="EUR",
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])
        invoice = send_invoice(inv)[0]
        acct = RailAccount.objects.create(org=org, kind="evm", label="B", address="0x" + "ee" * 20,
                                          networks=["arbitrum"], assets=["USDC"])
        OutboxMessage.objects.all().delete()
        return org, invoice, acct


class NeedsPersonPageTests(CryptoBase):
    def test_needs_person_lists_review_refund_xmr_and_failing_watchers(self):
        review = self.review()
        confirmable = self.review(minor=5000, reason="ambiguous", tx_hash="0x" + "2f" * 32)
        xmr = Payment.objects.create(rail="btcpay", network="", asset="XMR", amount_minor=10000,
                                     received_amount="0.5", state="detected", needs_staff_confirmation=True,
                                     invoice=self.invoice, external_id="xmr-1")
        refund = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                                        received_amount="110", state="confirmed", invoice=self.invoice,
                                        refund_due_base_units="1500000", tx_hash="0x" + "3f" * 32)
        health.failed("scan:evm:arbitrum:7", "gap")
        health.failed("btcpay:2", "refused")
        WatcherHealth.objects.filter(name="btcpay:2").update(failing_since=None)  # recovered: not listed
        self.sign_in()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn("<title>Needs a person</title>", page)
        self.assertIn("No open invoice matches this payment.", page)  # the sentence, not the code
        self.assertNotIn("no_matching_invoice", page)
        self.assertIn("https://arbiscan.io/tx/" + TX, page)
        self.assertIn(f"/payments/{review.pk}/assign/", page)
        self.assertIn(f"/payments/{review.pk}/reject/", page)
        self.assertNotIn(f"/payments/{review.pk}/confirm/", page)  # zero credit: assign only
        self.assertIn(f"/payments/{confirmable.pk}/confirm/", page)
        self.assertIn(f"/payments/{confirmable.pk}/assign/", page)
        self.assertIn(f"/payments/{xmr.pk}/confirm/", page)
        self.assertIn(f"/payments/{xmr.pk}/reject/", page)
        self.assertNotIn(f"/payments/{xmr.pk}/assign/", page)
        self.assertIn(f"/payments/{refund.pk}/refund/", page)
        self.assertIn("Watchers that could not look", page)
        self.assertIn("scan:evm:arbitrum:7", page)
        self.assertNotIn("btcpay:2", page)


class StuckGatewayPaymentTests(CryptoBase):
    def test_a_gateway_payment_detected_over_6_hours_ago_is_listed(self):
        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"})
        stuck = Payment.objects.create(rail="btcpay", network="bitcoin", asset="BTC", amount_minor=10000,
                                       received_amount="0.00123456", state="detected", invoice=self.invoice,
                                       external_id="btcpay:S1:stuck", rail_account=btcpay)
        fresh = Payment.objects.create(rail="lightning", network="lightning", asset="BTC", amount_minor=10000,
                                       received_amount="0.001", state="detected", invoice=self.invoice,
                                       external_id="ln:fresh")
        Payment.objects.filter(pk=stuck.pk).update(detected_at=timezone.now() - datetime.timedelta(hours=7))
        Payment.objects.filter(pk=fresh.pk).update(detected_at=timezone.now() - datetime.timedelta(hours=5))
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Gateway payments seen but not settled", page)
        self.assertIn("btcpay:S1:stuck", page)
        self.assertIn(f"/payments/{stuck.pk}/confirm/", page)
        self.assertNotIn("ln:fresh", page)


class ActionTests(CryptoBase):
    def test_assign_from_needs_person_confirms(self):
        p = self.review(invoice=False)
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": self.invoice.number or str(self.invoice.pk), "amount": "100.00",
                              "note": "Paid for the March invoice"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        p.refresh_from_db()
        self.assertEqual((p.state, p.invoice_id, p.amount_minor), ("confirmed", self.invoice.pk, 10000))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")
        self.assertTrue(AuditEntry.objects.filter(action="payment.assign", object_pk=str(p.pk)).exists())

    def test_assign_by_id_and_refusals(self):
        p = self.review(invoice=False)
        self.sign_in()
        for data in ({"invoice": "999999", "amount": "10", "note": "n"},
                     {"invoice": str(self.invoice.pk), "amount": "abc", "note": "n"},
                     {"invoice": str(self.invoice.pk), "amount": "10", "note": ""},
                     {"invoice": str(self.invoice.pk), "amount": "10.001", "note": "n"}):
            r = self.client.post(f"/payments/{p.pk}/assign/", data)
            self.assertEqual(r.status_code, 400, data)
            self.assertContains(r, "Needs a person", status_code=400)
        p.refresh_from_db()
        self.assertEqual(p.state, "needs_review")
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": str(self.invoice.pk), "amount": "100", "note": "n"})
        self.assertEqual(r.status_code, 302)

    def test_assign_never_crosses_organizations(self):
        _org, theirs, their_acct = self.other_org()
        self.assertEqual(theirs.number, self.invoice.number)  # numbers are unique per organization only
        p = self.review(invoice=False, rail_account=their_acct)
        # The service refuses another organization's invoice, whoever calls it.
        with self.assertRaises(InvoiceError):
            assign_payment(p, self.invoice, self.staff, amount_minor=10000, note="wrong org")
        self.sign_in()
        # Our invoice's id is not an invoice of the payment's organization.
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": str(self.invoice.pk), "amount": "100", "note": "n"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")
        # The shared number names the payment's own organization's invoice, never ours.
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": theirs.number, "amount": "100", "note": "their March invoice"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        p.refresh_from_db()
        self.assertEqual((p.state, p.invoice_id), ("confirmed", theirs.pk))
        statuses = dict(Invoice.objects.filter(pk__in=[self.invoice.pk, theirs.pk]).values_list("pk", "status"))
        self.assertEqual(statuses, {self.invoice.pk: "sent", theirs.pk: "paid"})
        self.assertEqual(set(OutboxMessage.objects.values_list("invoice_id", flat=True)), {theirs.pk})

    def test_assign_lookup_spans_the_organizations_sharing_the_address(self):
        _org, theirs, their_acct = self.other_org()
        RailAccount.objects.filter(pk=their_acct.pk).update(address=self.acct.address)  # one wallet, two orgs
        p = self.review(invoice=False, to_address=self.acct.address)
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/assign/",  # the shared number names both: refused
                             {"invoice": theirs.number, "amount": "100", "note": "n"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "More than one invoice", status_code=400)
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": str(theirs.pk), "amount": "100", "note": "n"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Payment.objects.get(pk=p.pk).invoice_id, theirs.pk)

    def test_assign_refuses_a_reference_naming_two_invoices(self):
        _org, theirs, _acct = self.other_org()
        p = self.review(invoice=False, rail_account=None)  # no organization to scope the lookup to
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": theirs.number, "amount": "100", "note": "n"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "More than one invoice", status_code=400)
        self.assertEqual(Payment.objects.get(pk=p.pk).state, "needs_review")
        r = self.client.post(f"/payments/{p.pk}/assign/",
                             {"invoice": str(theirs.pk), "amount": "100", "note": "by id"})
        self.assertEqual(r.status_code, 302)

    def test_acknowledge_refund_from_needs_person(self):
        p = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                                   received_amount="110", state="confirmed", invoice=self.invoice,
                                   refund_due_base_units="1500000", tx_hash=TX)
        self.sign_in()
        r = self.client.post(f"/payments/{p.pk}/refund/", {"note": ""})
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"/payments/{p.pk}/refund/", {"note": "Sent back by hand"})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertIsNotNone(p.refund_acknowledged_at)
        self.assertEqual(p.refund_acknowledged_by, self.staff)
        self.assertNotIn(f"/payments/{p.pk}/refund/", self.client.get("/").content.decode())

    def test_actions_require_staff_and_post(self):
        p = self.review(invoice=False)
        ref = Payment.objects.create(rail="evm", amount_minor=1, received_amount="1", state="confirmed",
                                     invoice=self.invoice, refund_due_base_units="5")
        urls = [f"/payments/{p.pk}/assign/", f"/payments/{ref.pk}/refund/"]
        for url in urls:  # anonymous
            self.assertEqual(self.client.post(url, {"note": "x"}).status_code, 302)
        self.sign_in()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 405)
        # A signed-in non-staff account is refused.
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("plain", password="pw-plain-1")
        self.client.logout()
        self.client.force_login(user)
        for url in urls:
            self.assertEqual(self.client.post(url, {"note": "x"}).status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.state, "needs_review")


class FinalityTests(CryptoBase):
    def test_staff_cannot_confirm_a_chain_payment_before_it_is_final(self):
        waiting = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                                         received_amount="108.27", amount_base_units="108270000", tx_hash=TX,
                                         state="detected", invoice=self.invoice, rail_account=self.acct, final=False)
        xmr = Payment.objects.create(rail="btcpay", asset="XMR", amount_minor=10000, received_amount="0.5",
                                     state="detected", needs_staff_confirmation=True, invoice=self.invoice,
                                     external_id="xmr-1")
        with self.assertRaises(InvoiceError):
            confirm_payment(waiting, self.staff, "looks paid")
        self.sign_in()
        page = self.client.get(f"/invoices/{self.invoice.pk}/").content.decode()
        self.assertNotIn(f"/payments/{waiting.pk}/confirm/", page)
        self.assertIn(f"/payments/{waiting.pk}/reject/", page)
        self.assertIn(f"/payments/{xmr.pk}/confirm/", page)  # nothing watches XMR: a person confirms it
        r = self.client.post(f"/payments/{waiting.pk}/confirm/", {"note": "looks paid"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Payment.objects.get(pk=waiting.pk).state, "detected")
        # A review payment waiting on the chain is neither confirmed nor assigned by hand.
        review = self.review(minor=5000, reason="ambiguous", final=False, tx_hash="0x" + "4f" * 32)
        page = self.client.get("/").content.decode()
        self.assertNotIn(f"/payments/{review.pk}/confirm/", page)
        self.assertNotIn(f"/payments/{review.pk}/assign/", page)
        self.assertIn(f"/payments/{review.pk}/reject/", page)
        with self.assertRaises(InvoiceError):
            assign_payment(review, self.invoice, self.staff, amount_minor=5000, note="n")
        # Once the chain says final, staff may.
        Payment.objects.filter(pk=waiting.pk).update(final=True)
        self.assertIn(f"/payments/{waiting.pk}/confirm/",
                      self.client.get(f"/invoices/{self.invoice.pk}/").content.decode())
        r = self.client.post(f"/payments/{waiting.pk}/confirm/", {"note": "final now"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Payment.objects.get(pk=waiting.pk).state, "confirmed")


class PageSafetyTests(CryptoBase):
    def corrupt(self, model, pk, column):
        with connection.cursor() as cur:
            cur.execute(f"UPDATE {model._meta.db_table} SET {column} = %s WHERE id = %s",
                        ["gAAAAABnot-a-token-from-this-key", pk])

    def test_an_undecryptable_value_does_not_break_the_page(self):
        review = self.review(evidence_note="hand note")
        refund = Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                                        received_amount="110", state="confirmed", invoice=self.invoice,
                                        refund_due_base_units="1500000", tx_hash="0x" + "3f" * 32,
                                        evidence_note="x")
        declaration = Declaration.objects.create(invoice=self.invoice, note="I paid", tx_hash=TX)
        self.corrupt(Payment, review.pk, "evidence_note")
        self.corrupt(Payment, refund.pk, "evidence_note")
        self.corrupt(Declaration, declaration.pk, "note")
        self.corrupt(Invoice, self.invoice.pk, "title")
        self.sign_in()
        with self.assertNoLogs("apps.vault.fields", "ERROR"):  # nothing encrypted is even loaded
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn(f"/payments/{review.pk}/assign/", page)
        self.assertIn(f"/payments/{refund.pk}/refund/", page)
        self.assertIn(self.invoice.number, page)

    def test_each_section_is_capped_and_says_so(self):
        for i in range(3):
            self.review(tx_hash=f"0x{i:064x}")
        self.sign_in()
        with mock.patch("apps.staff.views.NEEDS_PERSON_CAP", 2):
            page = self.client.get("/").content.decode()
        self.assertIn("Showing 2 of 3.", page)
        self.assertEqual(page.count("/assign/"), 2)
        self.assertNotIn("Showing", self.client.get("/").content.decode())

    def test_refunds_show_the_amount_in_the_asset_and_the_sender(self):
        Payment.objects.create(rail="evm", network="arbitrum", asset="USDC", amount_minor=10000,
                               received_amount="110", state="confirmed", invoice=self.invoice,
                               refund_due_base_units="1500000", tx_hash=TX, from_address=PAYER)
        Payment.objects.create(rail="lightning", network="lightning", asset="BTC", amount_minor=10000,
                               received_amount="0.0011", state="confirmed", invoice=self.invoice,
                               refund_due_base_units="1500", external_id="ln:1")
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("1.50 USDC", page)  # 1500000 base units at 6 decimals
        self.assertIn(PAYER, page)
        self.assertIn("0.000015 BTC", page)  # satoshis


class GapAndQueueTests(CryptoBase):
    def test_native_gap_is_durable_until_acknowledged(self):
        from apps.rails.tests_evm import FakeChain

        # A native look no longer skips blocks (final review F7): the gap is a rise no transaction explains.
        chain = FakeChain(head=1_000)
        chain.balance = 1
        WatchCursor.objects.create(network="arbitrum", address=self.acct.address.lower(),
                                   stream=f"evm-native:{self.acct.pk}", position="10",
                                   extra={"balance": "0", "nonce": 0})
        with mock.patch("apps.rails.http.post_json", side_effect=chain), \
                self.assertLogs("apps.rails.chains.evm", "WARNING"):
            jobs.chain_scan()
        gap = ScanGap.objects.get()
        self.assertEqual((gap.network, gap.from_block, gap.to_block, gap.reason), ("arbitrum", 11, 1000,
                                                                                  "unexplained_balance"))
        scan = WatcherHealth.objects.get(name=f"scan:evm:arbitrum:{self.acct.pk}")
        self.assertIsNone(scan.failing_since)  # the scan worked; the gap is its own record
        self.sign_in()
        url = f"/gaps/{gap.pk}/acknowledge/"
        page = self.client.get("/").content.decode()
        self.assertIn("Blocks not scanned", page)
        self.assertIn(url, page)
        self.assertIn('<td class="num" data-label="To block">1000</td>', page)
        with mock.patch("apps.rails.http.post_json", side_effect=chain):
            jobs.chain_scan()  # a clean scan clears nothing a person has not seen
        self.assertIn(url, self.client.get("/").content.decode())
        # POST, staff only, with CSRF.
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(Client().post(url).status_code, 302)  # anonymous: to the login page
        plain = Client()
        plain.force_login(get_user_model().objects.create_user("plain", password="pw-plain-1"))
        self.assertEqual(plain.post(url).status_code, 403)
        self.client.handler.enforce_csrf_checks = True
        self.assertEqual(self.client.post(url, {"note": "x"}).status_code, 403)
        self.client.handler.enforce_csrf_checks = False
        self.assertIsNone(ScanGap.objects.get(pk=gap.pk).acknowledged_at)
        r = self.client.post(url, {"note": "Checked the explorer: nothing in those blocks"})
        self.assertEqual(r.status_code, 302)
        gap.refresh_from_db()
        self.assertIsNotNone(gap.acknowledged_at)
        self.assertEqual(gap.acknowledged_by, self.staff)
        entry = AuditEntry.objects.get(action="scan_gap.acknowledge", object_pk=str(gap.pk))
        self.assertEqual(entry.actor, self.staff)
        self.assertEqual(entry.detail["note"], "Checked the explorer: nothing in those blocks")
        self.assertNotIn(url, self.client.get("/").content.decode())
        self.assertEqual(self.client.post(url, {"note": "again"}).status_code, 409)

    def test_needs_person_lists_transfers_not_yet_recorded(self):
        PendingTransfer.objects.create(
            account=self.acct, network="arbitrum", contract=USDC.contract, to_address=self.acct.address,
            from_address=PAYER, amount_base_units="108270000", tx_hash=TX, log_index=2, block_number=5,
            block_time=timezone.now() - datetime.timedelta(minutes=3), final=True, attempts=4,
            last_error_kind="OperationalError")
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Transfers seen but not recorded", page)
        self.assertIn("108.27 USDC", page)
        self.assertIn("https://arbiscan.io/tx/" + TX, page)
        self.assertIn("OperationalError", page)
        self.assertIn(">4<", page)
