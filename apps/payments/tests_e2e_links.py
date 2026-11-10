"""End to end through a payment link (spec 4): staff create the link on the staff pages, a buyer opens
it with a coupon in the URL, checks out, lands on the order's pay page, pays it on a fake chain, and the
worker settles it: paid, receipt carrying the links, webhooks, the CSV row, and "to fulfil" until staff
mark it fulfilled. Once per document kind, once for a US tax resident, and once for an order nobody pays.
"""
import re
from urllib.parse import urlsplit

from django.core import mail

from apps.audit.models import AuditEntry
from apps.invoices import tokens
from apps.invoices.models import Invoice
from apps.invoices.money import totals
from apps.links.models import LinkOrder, PaymentLink
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.e2e_helpers import CLIENT, CLIENT_NAME, SITE, Clock, E2EBase, evm_address
from apps.rails.models import RailAccount
from apps.rails.networks import NETWORKS

MINE = evm_address("link-mine")
THEIRS = evm_address("link-us")
PAYER = evm_address("link-payer")
USDC_ARB = NETWORKS["arbitrum"].assets["USDC"].contract


class LinkE2EBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.node = self.evm_node("arbitrum")
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM wallet", address=MINE,
                                               networks=["arbitrum"], assets=["USDC"])
        self.us = Organization.objects.create(slug="us", legal_name="Liberation LLC", address="Wyoming",
                                              is_us_resident_issuer=True)
        add_bank(self.us)
        self.us_rail = RailAccount.objects.create(org=self.us, kind="evm", label="US wallet", address=THEIRS,
                                                  networks=["arbitrum"], assets=["USDC"])
        self.work("chain_scan")  # the first pass marks where each stream starts
        self.staff = self.staff_client()

    # -- staff
    def create_link(self, **fields) -> tuple[int, str]:
        data = {
            "organization": self.org.pk, "title": "Residency consultation", "description": "One hour, online.",
            "currency": "EUR", "price": "100.00", "min_amount": "", "max_amount": "", "tax_percent": "",
            "max_quantity": "1", "kind": "invoice", "name_field": "required", "billing_field": "optional",
            "shipping_field": "off", "phone_field": "off", "methods": "custom",
            "method_types": ["chain:arbitrum:USDC"], "fulfillment": "on", "active": "on", "valid_until": "",
            "max_uses": "", "internal_note": "",
            **{f"custom_label_{i}": "" for i in range(5)},
        }
        data.update(fields)
        r = self.staff.post("/links/new/", data)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        pk = int(re.search(r"/links/(\d+)/", r["Location"]).group(1))
        page = self.staff.get(f"/links/{pk}/").content.decode()
        token = re.search(re.escape(SITE) + r"/l/([A-Za-z0-9_-]+)/", page).group(1)
        return pk, token

    def add_coupon(self, link_pk: int, code: str, percent: str):
        r = self.staff.post(f"/links/{link_pk}/coupons/", {"code": code, "name": "Autumn", "percent": percent,
                                                                   "max_uses": "10", "valid_until": "",
                                                                   "internal_note": ""})
        self.assertEqual(r.status_code, 302, r.content[:2000])

    # -- the buyer
    def buy(self, token: str, *, query: str = "", us: str = "no", **fields) -> str:
        page = self.client.get(f"/l/{token}/{query}")
        self.assertEqual(page.status_code, 200)
        data = {"email": CLIENT, "name": CLIENT_NAME, "us_tax_resident": us, "billing_line1": "Hlavná 1",
                "billing_city": "Bratislava", "billing_country": "Slovakia", **fields}
        r = self.client.post(f"/l/{token}/{query}", data)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertFalse(mail.outbox, "nothing is mailed before payment")
        return urlsplit(r["Location"]).path

    def pay_on_chain(self, pay: str, method: str):
        quote = self.quote(pay, method, "arbitrum", "USDC")
        self.node.send_token(USDC_ARB, quote.address, int(quote.amount_base_units), sender=PAYER)
        self.node.finalize()
        self.run_worker_once()
        return quote

    def invoice_of(self, pay: str) -> Invoice:
        return tokens.resolve(pay.strip("/").split("/")[1], "pay")

    def receipt_links(self, invoice: Invoice):
        receipt = next(m for m in mail.outbox if m.to == [CLIENT] and m.subject.startswith("Receipt"))
        view = re.search(re.escape(SITE) + r"/i/([A-Za-z0-9_-]+)/", receipt.body)
        self.assertIsNotNone(view, receipt.body)
        self.assertEqual(tokens.resolve(view.group(1), "view"), invoice)
        self.assertIn(CLIENT_NAME, receipt.body)
        return receipt

    def fulfil(self, link_pk: int, invoice: Invoice):
        page = self.staff.get(f"/links/{link_pk}/").content.decode()
        self.assertIn("To fulfil", page)
        order = LinkOrder.objects.get(invoice=invoice)
        r = self.staff.post(f"/link-orders/{order.pk}/fulfil/")
        self.assertEqual(r.status_code, 302)
        order.refresh_from_db()
        self.assertIsNotNone(order.fulfilled_at)
        self.assertIn("link_order.fulfil", self.actions("links.linkorder", order.pk))
        self.assertNotIn("1 to fulfil", self.staff.get("/links/").content.decode())


class LinkLifecycleTests(LinkE2EBase):
    def test_invoice_link_with_a_coupon_paid_in_usdc_on_arbitrum(self):
        link_pk, token = self.create_link()
        self.add_coupon(link_pk, "AUTUMN", "10")
        self.assertIn('value="AUTUMN"', self.client.get(f"/l/{token}/?promoCode=AUTUMN").content.decode())
        pay = self.buy(token, query="?promoCode=AUTUMN", coupon="AUTUMN")
        invoice = self.invoice_of(pay)
        self.assertEqual((invoice.kind, invoice.org_id, invoice.payment_link_id), ("invoice", self.org.pk, link_pk))
        self.assertEqual(totals(invoice).total, 9000)
        page = self.pay_page(pay)
        self.assertIn("USDC on Arbitrum One", page)
        self.assertIn("Keep this page", page)
        self.assertNotIn("bank", "".join(invoice.offered_methods))  # the link's custom set: the chain only
        quote = self.pay_on_chain(pay, self.rail.method_key)
        (payment,) = self.payments(invoice.pk)
        self.assertEqual((payment.quote_id, payment.amount_minor), (quote.pk, 9000))
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).hold, "used")
        self.assertPaidEverywhere(invoice.pk, payment, pay)
        self.receipt_links(invoice)
        self.assertIn("invoice.create", self.actions("invoices.invoice", invoice.pk))
        self.fulfil(link_pk, invoice)

    def test_payment_request_link(self):
        link_pk, token = self.create_link(kind="payment_request")
        pay = self.buy(token)
        invoice = self.invoice_of(pay)
        self.assertEqual((invoice.kind, invoice.number), ("payment_request", None))
        self.assertIn("Payment request", self.pay_page(pay))
        self.pay_on_chain(pay, self.rail.method_key)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "paid")
        self.run_worker_once()  # the outbox
        receipt = self.receipt_links(invoice)
        self.assertIn(f"#{invoice.pk}", receipt.subject)
        kinds = {(p["type"], p["data"].get("invoice_id")) for p in self.deliver_webhooks()}
        self.assertIn(("invoice.paid", invoice.pk), kinds)
        (payment,) = self.payments(invoice.pk)
        rows = [row for row in self.csv_rows() if row["payment_id"] == str(payment.pk)]
        self.assertEqual([(r["state"], r["rail"], r["network"]) for r in rows], [("confirmed", "evm", "arbitrum")])
        self.fulfil(link_pk, invoice)

    def test_a_us_tax_resident_is_billed_by_the_us_issuer_on_its_own_wallet(self):
        link_pk, token = self.create_link()
        pay = self.buy(token, us="yes")
        invoice = self.invoice_of(pay)
        self.assertEqual((invoice.org_id, invoice.requested_by_org_id, invoice.issued_by_rule),
                         (self.us.pk, self.org.pk, True))
        self.assertEqual(invoice.offered_methods, [self.us_rail.method_key])
        page = self.pay_page(pay)
        self.assertIn("Liberation LLC", page)
        self.assertNotIn("Liberation Travel s.r.o.", page)
        quote = self.pay_on_chain(pay, self.us_rail.method_key)
        self.assertEqual(quote.address, THEIRS)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "paid")
        self.run_worker_once()
        self.receipt_links(invoice)
        self.fulfil(link_pk, invoice)


class UnpaidOrderTests(LinkE2EBase):
    def test_an_unpaid_order_is_voided_after_seven_days_and_a_late_payment_goes_to_review(self):
        clock = Clock(self)
        _pk, token = self.create_link()
        pay = self.buy(token)
        invoice = self.invoice_of(pay)
        quote = self.quote(pay, self.rail.method_key, "arbitrum", "USDC")
        clock.advance(31 * 60)  # the worker expires the quote on time, as it does every minute
        self.run_worker_once()
        clock.advance(7 * 86400)
        self.run_worker_once()  # its amount is released (held a day after it closed); then the order waits no longer
        self.run_worker_once()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "void")
        entry = AuditEntry.objects.get(action="invoice.void", object_pk=str(invoice.pk))
        self.assertEqual(entry.actor_username, "system")
        self.assertIn("voided", self.pay_page(pay))
        self.node.send_token(USDC_ARB, MINE, int(quote.amount_base_units), sender=PAYER)
        self.node.finalize()
        self.run_worker_once()
        # The void waited for the quote's amount to be released (final review I1), so the late transfer
        # is matched to no invoice: it goes to review for a person to assign, and is never credited.
        (payment,) = self.payments()
        self.assertInReview(payment, "no_matching_invoice", invoice_pk=None)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "void")
        self.assertTrue(PaymentLink.objects.filter(pk=_pk).exists())
