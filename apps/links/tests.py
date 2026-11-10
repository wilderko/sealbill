"""Payment links (spec 2.2-2.3a): one rule per test, through the services and the public page."""
import datetime
import hashlib
import io
import re
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.contacts.models import Contact
from apps.contacts.services import find_or_create_contact
from apps.invoices import tokens
from apps.invoices.models import Invoice
from apps.invoices.money import totals
from apps.links import services as links
from apps.links.models import LinkAlert, LinkOrder, PaymentLink
from apps.notify.models import OutboxMessage
from apps.notify.services import flush_outbox
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.services import record_manual_payment
from apps.rails.models import RailAccount
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault
from apps.payments.testing import person

EVM = "0x" + "ab" * 20
EVM_US = "0x" + "cd" * 20
SITE = "https://pay.example"


def order(**kw) -> links.Order:
    base = {"email": "buyer@example.com", "us_tax_resident": "no", "name": "Typed Buyer"}
    return links.Order(**{**base, **kw})


class LinkBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(SITE_URL=SITE, STAFF_NOTIFY_EMAILS=["staff@seller.example"]))
        self.org = Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.", address="Main 1")
        self.us = Organization.objects.create(slug="us", legal_name="Liberation LLC", address="Wyoming",
                                              is_us_resident_issuer=True)
        self.bank = add_bank(self.org)
        self.us_bank = add_bank(self.us, label="US EUR account")
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                               networks=["arbitrum", "ethereum"], assets=["USDC", "ETH"])
        self.us_rail = RailAccount.objects.create(org=self.us, kind="evm", label="EVM US", address=EVM_US,
                                                  networks=["arbitrum", "ethereum"], assets=["USDC", "ETH"])
        self.link, self.token = self.make_link()

    def make_link(self, **fields):
        base = {"title": "Residency consultation", "currency": "EUR", "price_minor": 10000}
        return links.create_link(self.org, **{**base, **fields})

    def checkout(self, link=None, language="en", **kw):
        return links.checkout(link or self.link, order(**kw), language=language)

    def pay_fully(self, invoice):
        invoice.refresh_from_db()
        record_manual_payment(invoice, amount_minor=totals(invoice).total, rail="bank", note="", user=person())
        invoice.refresh_from_db()
        return invoice

    def later(self, **delta):
        at = timezone.now() + datetime.timedelta(**delta)
        return mock.patch("django.utils.timezone.now", return_value=at)

    def page(self, token=None, query="", **extra):
        return self.client.get(f"/l/{token or self.token}/{query}", **extra)

    def post(self, data, token=None, query="", **extra):
        return self.client.post(f"/l/{token or self.token}/{query}", data, **extra)

    def form(self, **kw):
        data = {"email": "buyer@example.com", "name": "Typed Buyer", "us_tax_resident": "no"}
        data.update(kw)
        return data


class TokenTests(LinkBase):
    def test_token_is_random_stored_as_hash_and_encrypted(self):
        self.assertGreaterEqual(len(self.token), 43)  # 32 random bytes, url-safe: 256 bits
        self.assertEqual(self.link.token_hash, hashlib.sha256(self.token.encode()).hexdigest())
        with connection.cursor() as cursor:
            cursor.execute("SELECT token FROM links_paymentlink WHERE id = %s", [self.link.pk])
            (stored,) = cursor.fetchone()
        self.assertNotIn(self.token, stored)  # ciphertext in the column
        self.assertTrue(stored.startswith("gAAAA"))
        self.assertEqual(PaymentLink.objects.get(pk=self.link.pk).token, self.token)
        self.assertEqual(links.resolve_token(self.token), self.link)
        self.assertIsNone(links.resolve_token(self.token[:-1] + ("A" if self.token[-1] != "A" else "B")))
        self.assertEqual(self.link.url_for(self.token), f"{SITE}/l/{self.token}/")

    def test_unknown_token_is_404(self):
        self.assertEqual(self.page(token="nope" * 11).status_code, 404)

    def test_rotating_replaces_the_token_and_is_audited(self):
        new = links.rotate_token(self.link, actor=None)
        self.assertNotEqual(new, self.token)
        self.assertIsNone(links.resolve_token(self.token))
        self.assertEqual(links.resolve_token(new), self.link)
        self.assertTrue(AuditEntry.objects.filter(action="link.rotate", object_pk=str(self.link.pk)).exists())


class CheckoutTests(LinkBase):
    def test_fixed_price_makes_a_sent_invoice_with_one_line(self):
        invoice, pay = self.checkout()
        self.assertEqual((invoice.status, invoice.kind, invoice.currency), ("sent", "invoice", "EUR"))
        self.assertTrue(invoice.number)
        (line,) = invoice.lines.all()
        self.assertEqual((line.name, line.quantity, line.unit_price_minor, line.tax_rate_bps),
                         ("Residency consultation", 1, 10000, 0))
        self.assertEqual(totals(invoice).total, 10000)
        self.assertEqual((invoice.payment_link_id, invoice.coupon_id, invoice.org_id), (self.link.pk, None, self.org.pk))
        self.assertEqual(tokens.resolve(pay, "pay"), invoice)
        order_row = invoice.link_order
        self.assertEqual((order_row.hold, order_row.quantity, order_row.email, order_row.language),
                         ("reserved", 1, "buyer@example.com", "en"))
        self.assertAlmostEqual(order_row.reserved_until.timestamp(),
                               (timezone.now() + datetime.timedelta(minutes=60)).timestamp(), delta=5)
        self.assertEqual(invoice.snapshot["buyer"]["name"], "Typed Buyer")

    def test_payment_request_kind(self):
        link, _t = self.make_link(kind="payment_request")
        invoice, _pay = self.checkout(link)
        self.assertEqual((invoice.kind, invoice.number, invoice.status), ("payment_request", None, "sent"))

    def test_customer_chosen_amount_within_its_bounds(self):
        link, _t = self.make_link(customer_chooses=True, price_minor=None, min_minor=500, max_minor=50000)
        for amount, code in ((499, "amount_low"), (50001, "amount_high"), (0, "amount_low"), (None, "amount_low")):
            with self.assertRaises(links.CheckoutError) as caught:
                self.checkout(link, amount_minor=amount)
            self.assertEqual(caught.exception.code, code)
        invoice, _pay = self.checkout(link, amount_minor=1234)
        self.assertEqual((invoice.lines.get().unit_price_minor, totals(invoice).total), (1234, 1234))
        unbounded, _t = self.make_link(customer_chooses=True, price_minor=None)
        invoice, _pay = self.checkout(unbounded, amount_minor=7)
        self.assertEqual(totals(invoice).total, 7)
        self.assertEqual(Invoice.objects.filter(payment_link=link).count(), 1)  # the refusals made nothing

    def test_quantity_cap(self):
        with self.assertRaises(links.CheckoutError) as caught:
            self.checkout(quantity=2)  # the default cap is 1
        self.assertEqual(caught.exception.code, "quantity")
        three, _t = self.make_link(max_quantity=3)
        with self.assertRaises(links.CheckoutError):
            self.checkout(three, quantity=4)
        with self.assertRaises(links.CheckoutError):
            self.checkout(three, quantity=0)
        invoice, _pay = self.checkout(three, quantity=3)
        self.assertEqual((invoice.lines.get().quantity, totals(invoice).total), (3, 30000))
        unlimited, _t = self.make_link(max_quantity=0, price_minor=100)
        invoice, _pay = self.checkout(unlimited, quantity=200)
        self.assertEqual(totals(invoice).total, 20000)
        with self.assertRaises(links.CheckoutError):
            self.checkout(unlimited, quantity=201)

    def test_tax_is_added_and_the_coupon_comes_off_before_it(self):
        link, _t = self.make_link(tax_rate_bps=2000)
        invoice, _pay = self.checkout(link)
        self.assertEqual(totals(invoice).total, 12000)
        links.add_coupon(link, code="Spring", percent_bps=1000)
        invoice, _pay = self.checkout(link, coupon_code="spring")
        t = totals(invoice)
        self.assertEqual((t.subtotal, t.discount, t.tax, t.total), (10000, 1000, 1800, 10800))

    def test_a_coupon_is_a_line_discount_and_recorded_on_the_order(self):
        coupon = links.add_coupon(self.link, code="SPRING", percent_bps=2500, name="Spring sale")
        invoice, _pay = self.checkout(coupon_code="  spring ")
        self.assertEqual((invoice.coupon_id, invoice.lines.get().discount_rate_bps), (coupon.pk, 2500))
        self.assertEqual(totals(invoice).total, 7500)

    def test_unknown_inactive_and_expired_coupons_are_refused(self):
        links.add_coupon(self.link, code="OLD", percent_bps=1000, valid_until=timezone.now() - datetime.timedelta(minutes=1))
        off = links.add_coupon(self.link, code="OFF", percent_bps=1000)
        links.set_coupon_active(off, False, actor=None)
        other, _t = self.make_link()
        links.add_coupon(other, code="ELSEWHERE", percent_bps=1000)
        for code in ("NOPE", "OLD", "OFF", "ELSEWHERE"):
            with self.assertRaises(links.CheckoutError) as caught:
                self.checkout(coupon_code=code)
            self.assertEqual(caught.exception.code, "coupon_invalid", code)
        self.assertFalse(Invoice.objects.filter(payment_link=self.link).exists())

    def test_one_coupon_per_order(self):
        links.add_coupon(self.link, code="A", percent_bps=1000)
        links.add_coupon(self.link, code="B", percent_bps=1000)
        r = self.post(self.form(coupon="A, B"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Invoice.objects.filter(payment_link=self.link).exists())


class ReservationTests(LinkBase):
    def test_coupon_uses_are_held_for_sixty_minutes(self):
        links.add_coupon(self.link, code="FIRST", percent_bps=5000, max_uses=1)
        self.checkout(coupon_code="FIRST")
        with self.assertRaises(links.CheckoutError) as caught:
            self.checkout(coupon_code="FIRST", email="second@example.com")
        self.assertEqual(caught.exception.code, "coupon_used_up")
        with self.later(minutes=59):
            with self.assertRaises(links.CheckoutError):
                self.checkout(coupon_code="FIRST", email="second@example.com")
        with self.later(minutes=61):
            invoice, _pay = self.checkout(coupon_code="FIRST", email="second@example.com")
        self.assertEqual(totals(invoice).total, 5000)

    def test_a_paid_order_turns_its_hold_into_a_use(self):
        links.add_coupon(self.link, code="FIRST", percent_bps=5000, max_uses=1)
        invoice, _pay = self.checkout(coupon_code="FIRST")
        self.pay_fully(invoice)
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).hold, "used")
        with self.later(days=2):
            with self.assertRaises(links.CheckoutError) as caught:
                self.checkout(coupon_code="FIRST", email="second@example.com")
        self.assertEqual(caught.exception.code, "coupon_used_up")

    def test_the_worker_frees_lapsed_holds(self):
        invoice, _pay = self.checkout()
        with self.later(minutes=61):
            links.run_jobs()
        self.assertEqual(LinkOrder.objects.get(invoice=invoice).hold, "lapsed")

    def test_link_total_uses_count_holds_and_uses(self):
        link, token = self.make_link(max_uses=1)
        self.checkout(link)
        r = self.page(token=token)
        self.assertEqual(r.status_code, 410)
        self.assertContains(r, "This payment link is no longer available.", status_code=410)
        with self.assertRaises(links.CheckoutError) as caught:
            self.checkout(link, email="second@example.com")
        self.assertEqual(caught.exception.code, "unavailable")
        with self.later(minutes=61):
            self.assertEqual(self.page(token=token).status_code, 200)

    def test_a_late_payment_keeps_its_price_and_the_overshoot_is_one_staff_item(self):
        coupon = links.add_coupon(self.link, code="FIRST", percent_bps=5000, max_uses=1)
        first, _pay = self.checkout(coupon_code="FIRST")
        with self.later(minutes=61):
            second, _pay2 = self.checkout(coupon_code="FIRST", email="second@example.com")
            self.pay_fully(second)
            self.pay_fully(first)  # paid after its hold lapsed, and the only use went to the second
        first.refresh_from_db()
        self.assertEqual((first.status, totals(first).total), ("paid", 5000))
        self.assertEqual(LinkOrder.objects.get(invoice=first).hold, "used")
        alerts = LinkAlert.objects.filter(kind="over_cap")
        self.assertEqual([(a.link_id, a.coupon_id, a.invoice_id) for a in alerts], [(self.link.pk, coupon.pk, first.pk)])

    def test_a_link_cap_overshoot_is_its_own_item(self):
        link, _t = self.make_link(max_uses=1)
        first, _pay = self.checkout(link)
        with self.later(minutes=61):
            second, _pay2 = self.checkout(link, email="second@example.com")
            self.pay_fully(second)
            self.pay_fully(first)
        self.assertEqual([(a.link_id, a.coupon_id, a.invoice_id) for a in LinkAlert.objects.filter(kind="over_cap")],
                         [(link.pk, None, first.pk)])

    def test_a_paid_order_inside_its_hold_raises_nothing(self):
        coupon = links.add_coupon(self.link, code="FIRST", percent_bps=5000, max_uses=1)
        invoice, _pay = self.checkout(coupon_code="FIRST")
        self.pay_fully(invoice)
        self.assertFalse(LinkAlert.objects.exists())
        self.assertEqual(links.coupon_uses_taken(coupon), 1)


class AvailabilityTests(LinkBase):
    def assertGone(self, token):
        r = self.page(token=token)
        self.assertContains(r, "This payment link is no longer available.", status_code=410)
        r = self.post(self.form(), token=token)
        self.assertContains(r, "This payment link is no longer available.", status_code=410)

    def test_inactive_link(self):
        link, token = self.make_link(active=False)
        self.assertGone(token)
        self.assertFalse(Invoice.objects.filter(payment_link=link).exists())

    def test_expired_link(self):
        link, token = self.make_link(valid_until=timezone.now() - datetime.timedelta(seconds=1))
        self.assertGone(token)
        self.assertFalse(Invoice.objects.filter(payment_link=link).exists())

    def test_used_up_link(self):
        link, token = self.make_link(max_uses=1)
        self.pay_fully(self.checkout(link)[0])
        self.assertGone(token)
        self.assertEqual(Invoice.objects.filter(payment_link=link).count(), 1)


class UsResidentTests(LinkBase):
    def stored(self, flag):
        contact, _ = find_or_create_contact(self.org, "buyer@example.com", legal_name="Stored Name", us_tax_resident=flag)
        return contact

    def test_typed_yes_stored_yes(self):
        contact = self.stored("yes")
        invoice, _pay = self.checkout(us_tax_resident="yes")
        self.assertEqual((invoice.org_id, invoice.requested_by_org_id, invoice.issued_by_rule), (self.us.pk, self.org.pk, True))
        self.assertFalse(LinkAlert.objects.exists())
        self.assertEqual(Contact.objects.get(pk=contact.pk).us_tax_resident, "yes")

    def test_typed_yes_stored_no_routes_to_the_us_issuer_and_raises_an_item(self):
        contact = self.stored("no")
        invoice, _pay = self.checkout(us_tax_resident="yes")
        self.assertEqual(invoice.org_id, self.us.pk)
        (alert,) = LinkAlert.objects.all()
        self.assertEqual((alert.kind, alert.link_id, alert.invoice_id), ("us_mismatch", self.link.pk, invoice.pk))
        self.assertEqual(Contact.objects.get(pk=contact.pk).us_tax_resident, "no")  # nothing stored changes

    def test_typed_no_stored_yes_routes_on_the_typed_answer_and_raises_an_item(self):
        """Owner's ruling 2026-11-04 (spec 2.3a item 3 revised): a public link reveals nothing stored, so
        the order goes where the buyer's answer sends it, and staff are asked to void and reissue it."""
        contact = self.stored("yes")
        invoice, _pay = self.checkout(us_tax_resident="no")
        self.assertEqual((invoice.org_id, invoice.requested_by_org_id, invoice.issued_by_rule),
                         (self.org.pk, self.org.pk, False))
        self.assertEqual(invoice.contact_id, contact.pk)
        (alert,) = LinkAlert.objects.all()
        self.assertEqual((alert.kind, alert.link_id, alert.invoice_id), ("us_mismatch", self.link.pk, invoice.pk))
        self.assertEqual(Contact.objects.get(pk=contact.pk).us_tax_resident, "yes")  # nothing stored changes

    def test_typed_no_stored_yes_shows_the_links_organization_as_seller(self):
        self.stored("yes")
        invoice, pay = self.checkout(us_tax_resident="no")
        page = self.client.get(f"/p/{pay}/").content.decode()
        self.assertTrue("Liberation Travel s.r.o." in page and "Liberation LLC" not in page, "the pay page's seller")
        from apps.invoices.pdf import render_invoice_pdf
        from apps.invoices.tests_audit import pdf_text

        text = pdf_text(render_invoice_pdf(Invoice.objects.get(pk=invoice.pk), "en"))
        self.assertTrue("Liberation Travel s.r.o." in text and "Liberation LLC" not in text, "the PDF's seller")

    def test_a_stored_yes_is_not_revealed_by_a_missing_us_issuer(self):
        self.stored("yes")
        self.us.is_us_resident_issuer = False
        self.us.save()
        invoice, _pay = self.checkout(us_tax_resident="no")  # no "no US issuer" refusal to probe with
        self.assertEqual(invoice.org_id, self.org.pk)

    def test_typed_no_stored_yes_is_not_to_fulfil_until_the_item_is_resolved(self):
        from django.contrib.auth import get_user_model

        clerk = get_user_model().objects.create_user("clerk", is_staff=True)
        link, _t = self.make_link(fulfillment=True)
        self.stored("yes")
        invoice = self.pay_fully(self.checkout(link, us_tax_resident="no")[0])
        order = LinkOrder.objects.get(invoice=invoice)
        self.assertFalse(links.to_fulfil(LinkOrder.objects.filter(pk=order.pk)).exists())
        with self.assertRaises(links.LinkError) as caught:
            links.mark_fulfilled(order, clerk)
        self.assertIn("US-resident issuer", str(caught.exception))
        links.resolve_alert(LinkAlert.objects.get(kind="us_mismatch"), clerk)
        self.assertTrue(links.to_fulfil(LinkOrder.objects.filter(pk=order.pk)).exists())
        links.mark_fulfilled(order, clerk)
        self.assertIsNotNone(LinkOrder.objects.get(pk=order.pk).fulfilled_at)

    def test_typed_yes_stored_no_is_to_fulfil_once_paid(self):
        link, _t = self.make_link(fulfillment=True)
        self.stored("no")
        invoice = self.pay_fully(self.checkout(link, us_tax_resident="yes")[0])
        self.assertEqual(LinkAlert.objects.get().kind, "us_mismatch")  # staff check the flag; the order is right
        self.assertTrue(links.to_fulfil(LinkOrder.objects.filter(invoice=invoice)).exists())

    def test_typed_no_stored_no(self):
        self.stored("no")
        invoice, _pay = self.checkout(us_tax_resident="no")
        self.assertEqual((invoice.org_id, invoice.issued_by_rule), (self.org.pk, False))
        self.assertFalse(LinkAlert.objects.exists())

    def test_a_new_buyer_who_says_yes(self):
        invoice, _pay = self.checkout(us_tax_resident="yes")
        self.assertEqual(invoice.org_id, self.us.pk)
        native = Contact.objects.get(org=self.org, copy_for_org__isnull=True)
        self.assertEqual(native.us_tax_resident, "yes")
        self.assertEqual(invoice.contact.copy_for_org_id, self.org.pk)
        self.assertFalse(LinkAlert.objects.exists())

    def test_the_database_keeps_one_open_no_issuer_item_per_link(self):
        from django.db import IntegrityError, transaction

        LinkAlert.objects.create(kind="no_us_issuer", link=self.link)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LinkAlert.objects.create(kind="no_us_issuer", link=self.link)  # what a racing checkout would do
        LinkAlert.objects.update(resolved_at=timezone.now())
        LinkAlert.objects.create(kind="no_us_issuer", link=self.link)  # a new episode after it was resolved

    def test_no_us_issuer_refuses_and_tells_staff_once(self):
        self.us.is_us_resident_issuer = False
        self.us.save()
        for _ in range(2):
            with self.assertRaises(links.CheckoutError) as caught:
                self.checkout(us_tax_resident="yes")
            self.assertEqual(caught.exception.code, "no_us_issuer")
        self.assertEqual(LinkAlert.objects.filter(kind="no_us_issuer").count(), 1)
        self.assertFalse(Invoice.objects.filter(payment_link=self.link).exists())


class MethodTypeTests(LinkBase):
    def test_types_map_onto_whichever_organization_issues(self):
        link, _t = self.make_link(methods="custom", method_types=["chain:arbitrum:USDC", "bank:EUR"])
        mine, _p = self.checkout(link)
        self.assertEqual(sorted(mine.offered_methods), sorted([f"bank:{self.bank.pk}", f"rail:{self.rail.pk}"]))
        self.assertEqual(mine.offered_pairs, [["arbitrum", "USDC"]])
        theirs, _p = self.checkout(link, us_tax_resident="yes", email="us@example.com")
        self.assertEqual(sorted(theirs.offered_methods), sorted([f"bank:{self.us_bank.pk}", f"rail:{self.us_rail.pk}"]))

    def test_the_pay_page_offers_only_the_chosen_pairs(self):
        from apps.portal import crypto
        from apps.quotes.errors import QuoteError
        from apps.quotes.services import create_quote

        link, _t = self.make_link(methods="custom", method_types=["chain:arbitrum:USDC"])
        invoice, _p = self.checkout(link)
        self.assertEqual([(o["network"], o["asset"]) for o in crypto.options(invoice)], [("arbitrum", "USDC")])
        with self.assertRaises(QuoteError) as caught:
            create_quote(invoice, self.rail, "ethereum", "ETH")
        self.assertEqual(caught.exception.code, "asset_not_enabled")

    def test_default_methods_offer_everything_the_issuer_has(self):
        invoice, _p = self.checkout()
        self.assertEqual(invoice.offered_pairs, [])
        self.assertEqual(sorted(invoice.offered_methods), sorted([f"bank:{self.bank.pk}", f"rail:{self.rail.pk}"]))

    def test_nothing_to_offer_refuses_and_raises_one_item(self):
        link, token = self.make_link(methods="custom", method_types=["fiat:revolut"])
        for _ in range(2):
            with self.assertRaises(links.CheckoutError) as caught:
                self.checkout(link)
            self.assertEqual(caught.exception.code, "no_methods")
        self.assertEqual(LinkAlert.objects.filter(kind="no_methods", link=link).count(), 1)
        self.assertFalse(Invoice.objects.filter(payment_link=link).exists())
        self.assertFalse(Contact.objects.exists())  # nothing was made for a checkout that cannot be paid
        r = self.post(self.form(), token=token)
        self.assertContains(r, "This payment link cannot be paid right now.", status_code=503)

    def test_unknown_types_are_refused_when_saving(self):
        with self.assertRaises(links.LinkError):
            self.make_link(methods="custom", method_types=["chain:nowhere:USDC"])
        with self.assertRaises(links.LinkError):
            self.make_link(methods="custom", method_types=[])


class ContactTests(LinkBase):
    def test_an_existing_contact_is_neither_shown_nor_changed(self):
        contact, _ = find_or_create_contact(
            self.org, "Buyer@Example.com", legal_name="Stored Name", display_name="Stored Display",
            address={"line1": "Stored Street 9", "city": "Stored City", "country": "SK"}, phone="+421 900 000 000",
            preferred_language="de", us_tax_resident="no", tax_ids="SK-STORED-1",
        )
        before = {f: getattr(contact, f) for f in ("legal_name", "display_name", "address", "phone",
                                                    "preferred_language", "us_tax_resident", "tax_ids")}
        link, token = self.make_link(billing_field="optional")
        r = self.post(self.form(email="buyer@example.com", name="Typed Buyer", billing_line1="Typed Road 1",
                                billing_city="Typed Town", billing_country="Austria"), token=token, query="?lang=en")
        self.assertEqual(r.status_code, 302, r.content[:500])
        invoice = Invoice.objects.get(payment_link=link)
        self.assertEqual(invoice.contact_id, contact.pk)
        fresh = Contact.objects.get(pk=contact.pk)
        self.assertEqual({f: getattr(fresh, f) for f in before}, before)
        self.assertEqual(fresh.emails.count(), 1)
        page = self.client.get(r["Location"]).content.decode()
        for stored in ("Stored Name", "Stored Display", "Stored Street", "Stored City", "SK-STORED-1", "+421"):
            self.assertNotIn(stored, page)
        self.assertIn("Typed Buyer", page)
        self.assertIn("Typed Road 1", page)
        self.assertNotIn('lang="de"', page)

    def test_the_pay_page_speaks_the_checkout_language_not_the_contacts(self):
        find_or_create_contact(self.org, "buyer@example.com", legal_name="Stored Name", preferred_language="de")
        r = self.post(self.form(), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("lang=", r["Location"])
        page = self.client.get(r["Location"], HTTP_ACCEPT_LANGUAGE="de")
        self.assertContains(page, '<html lang="fr"')

    def test_a_new_contact_is_made_from_what_was_typed(self):
        link, _t = self.make_link(billing_field="required", phone_field="optional")
        self.checkout(link, name="New Person", billing={"line1": "Road 2", "city": "Graz", "country": "Austria"},
                      phone="+43 1", language="de")
        contact = Contact.objects.get(org=self.org)
        self.assertEqual((contact.legal_name, contact.phone, contact.preferred_language, contact.us_tax_resident),
                         ("New Person", "+43 1", "de", "no"))
        self.assertEqual(contact.address["city"], "Graz")


class LetterTests(LinkBase):
    def test_no_email_before_payment(self):
        r = self.post(self.form())
        self.assertEqual(r.status_code, 302)
        self.assertFalse(OutboxMessage.objects.exists())
        flush_outbox()
        self.assertEqual(mail.outbox, [])

    def test_receipt_and_partial_letter_carry_the_links(self):
        invoice, pay = self.checkout(language="sk")
        record_manual_payment(invoice, amount_minor=4000, rail="bank", note="", user=person())
        flush_outbox()
        (partial,) = mail.outbox
        self.assertEqual(partial.to, ["buyer@example.com"])
        self.assertIn(f"{SITE}/p/{pay}/", partial.body)
        self.assertIn("Typed Buyer", partial.body)
        self.assertIn("čiastočnú platbu", partial.body)  # in the checkout's language
        mail.outbox = []
        record_manual_payment(invoice, amount_minor=6000, rail="bank", note="", user=person())
        flush_outbox()
        (receipt,) = mail.outbox
        view = re.search(re.escape(SITE) + r"/i/([A-Za-z0-9_-]+)/", receipt.body)
        self.assertIsNotNone(view, receipt.body)
        self.assertEqual(tokens.resolve(view.group(1), "view"), invoice)
        links.run_jobs()  # the receipt is out: the copies are forgotten
        stored = Invoice.objects.filter(pk=invoice.pk).values_list("view_token_copy", "pay_token_copy").get()
        self.assertEqual(stored, (None, None))

    def test_a_nameless_order_is_greeted_without_a_name(self):
        from django.conf import settings
        from django.template.loader import render_to_string
        from django.utils import translation

        link, _t = self.make_link(name_field="off")
        invoice, _pay = self.checkout(link, name="", language="sk")
        record_manual_payment(invoice, amount_minor=4000, rail="bank", note="", user=person())
        flush_outbox()  # the partial letter
        record_manual_payment(invoice, amount_minor=6000, rail="bank", note="", user=person())
        flush_outbox()  # the receipt
        self.assertEqual(len(mail.outbox), 2)
        for letter in mail.outbox:
            self.assertTrue(letter.body.startswith("Dobrý deň,"), letter.body[:40])
            html = letter.alternatives[0][0]
            self.assertIn("<p>Dobrý deň,</p>", html)
        for code, _name in settings.LANGUAGES:
            with translation.override(code):
                hello = translation.gettext("Hello,")
                dear = translation.gettext("Dear %(name)s,") % {"name": ""}
                for kind in ("receipt", "partial", "invoice"):
                    for ext in ("txt", "html"):
                        text = render_to_string(f"email/{kind}.{ext}", {"name": "", "language": code, "org": "O",
                                                                         "ref": "1", "total": "1", "paid": "1",
                                                                         "remaining": "1"})
                        self.assertIn(hello, text, (code, kind, ext))
                        self.assertNotIn(dear, text, (code, kind, ext))
            if code != "en":
                self.assertNotEqual(hello, "Hello,", code)

    def test_reissued_links_reach_the_letters(self):
        from apps.invoices.services import reissue_links

        invoice, old_pay = self.checkout()
        _view, new_pay = reissue_links(invoice)
        record_manual_payment(invoice, amount_minor=4000, rail="bank", note="", user=person())
        flush_outbox()
        (partial,) = mail.outbox
        self.assertIn(f"{SITE}/p/{new_pay}/", partial.body)
        self.assertNotIn(old_pay, partial.body)

    def test_letters_go_to_the_typed_address(self):
        contact, _ = find_or_create_contact(self.org, "primary@example.com", legal_name="Stored Name")
        from apps.contacts.models import ContactEmail

        ContactEmail.objects.create(contact=contact, address="second@example.com")
        invoice, _pay = self.checkout(email="second@example.com")
        self.assertEqual(invoice.contact_id, contact.pk)
        self.pay_fully(invoice)
        flush_outbox()
        (receipt,) = mail.outbox
        self.assertEqual(receipt.to, ["second@example.com"])
        self.assertNotIn("Stored Name", receipt.body)

    def test_an_ordinary_invoice_keeps_no_links(self):
        from apps.invoices.services import create_invoice, send_invoice

        contact, _ = find_or_create_contact(self.org, "plain@example.com", legal_name="Plain")
        draft, _ = create_invoice(self.org, contact, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])
        sent, _v, _p = send_invoice(draft, email_client=False)
        self.assertEqual(Invoice.objects.filter(pk=sent.pk).values_list("view_token_copy", "pay_token_copy").get(),
                         (None, None))
        self.pay_fully(sent)
        flush_outbox()
        (receipt,) = mail.outbox
        self.assertNotIn("/i/", receipt.body)

    def test_staff_are_told_of_each_paid_order_when_the_link_asks(self):
        link, _t = self.make_link(notify_staff=True)
        invoice, _pay = self.checkout(link)
        self.assertFalse(OutboxMessage.objects.filter(kind="order_paid_staff").exists())
        self.pay_fully(invoice)
        (msg,) = OutboxMessage.objects.filter(kind="order_paid_staff")
        self.assertEqual((msg.recipient, msg.plain_context), ("staff", {"link_id": link.pk, "invoice_id": invoice.pk}))
        flush_outbox()
        letter = next(m for m in mail.outbox if m.to == ["staff@seller.example"])
        self.assertIn(invoice.number, letter.subject)
        self.assertIn(f"/links/{link.pk}/", letter.body)
        self.assertNotIn("Typed Buyer", letter.body)
        self.assertNotIn("100", letter.body)

    def test_no_staff_letter_when_the_link_does_not_ask(self):
        self.pay_fully(self.checkout()[0])
        self.assertFalse(OutboxMessage.objects.filter(kind="order_paid_staff").exists())


class VoidTests(LinkBase):
    def test_unpaid_orders_are_voided_after_seven_days_by_the_system(self):
        invoice, pay = self.checkout()
        with self.later(days=6, hours=23):
            links.run_jobs()
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "sent")
        with self.later(days=7, minutes=1):
            links.run_jobs()
        fresh = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual(fresh.status, "void")
        entry = AuditEntry.objects.get(action="invoice.void", object_pk=str(invoice.pk))
        self.assertEqual((entry.actor_username, entry.system_detail["reason"]), ("system", "unpaid_link_order"))
        self.assertEqual(Invoice.objects.filter(pk=invoice.pk).values_list("pay_token_copy", flat=True).get(), None)
        self.assertContains(self.client.get(f"/p/{pay}/"), "voided")

    def test_the_void_and_its_audit_entry_stand_or_fall_together(self):
        invoice, _pay = self.checkout()
        with self.later(days=8), mock.patch("apps.audit.services.record_system", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                links.run_jobs()
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "sent")  # no void without its record
        with self.later(days=8):
            links.run_jobs()
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "void")
        self.assertEqual(AuditEntry.objects.filter(action="invoice.void", object_pk=str(invoice.pk)).count(), 1)

    def test_a_void_by_staff_forgets_the_links_at_once(self):
        from apps.invoices.services import void_invoice

        invoice, _pay = self.checkout()
        void_invoice(invoice, "duplicate order", None)
        self.assertEqual(Invoice.objects.filter(pk=invoice.pk).values_list("view_token_copy", "pay_token_copy").get(),
                         (None, None))

    def test_paid_partly_paid_and_ordinary_invoices_are_left_alone(self):
        paid, _p = self.checkout()
        self.pay_fully(paid)
        partly, _p = self.checkout(email="partly@example.com")
        record_manual_payment(partly, amount_minor=100, rail="bank", note="", user=person())
        from apps.invoices.services import create_invoice, send_invoice

        contact, _ = find_or_create_contact(self.org, "plain@example.com", legal_name="Plain")
        draft, _ = create_invoice(self.org, contact, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])
        plain, _v, _p = send_invoice(draft, email_client=False)
        with self.later(days=30):
            links.run_jobs()
        self.assertEqual([Invoice.objects.get(pk=i.pk).status for i in (paid, partly, plain)],
                         ["paid", "partially_paid", "sent"])

    def test_an_order_being_paid_is_not_voided(self):
        from apps.quotes.models import Quote

        invoice, _p = self.checkout()
        with self.later(days=8):
            Quote.objects.create(invoice=invoice, rail_account=self.rail, network="arbitrum", asset="USDC",
                                 address=EVM, amount_base_units="1", min_accept_base_units="1",
                                 ceiling_base_units="2", invoice_amount_minor=10000, issued_at=timezone.now(),
                                 expires_at=timezone.now() + datetime.timedelta(minutes=30))
            links.run_jobs()
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "sent")


class VoidWaitsWhileNobodyCouldLookTests(LinkBase):
    """Final review I1: "could not look" is not "not paid". The 7-day void waits while a quote of the
    order still holds its amount, while a watcher of a rail it was quoted on is failing or behind, and
    while blocks of that rail are not scanned; it goes ahead once they clear."""

    def quoted(self, *, closed_days_ago=None, state="expired", network="arbitrum", rail=None):
        from apps.quotes.models import Quote

        invoice, _p = self.checkout()
        sent = Invoice.objects.filter(pk=invoice.pk).values_list("sent_at", flat=True).get()
        issued = sent + datetime.timedelta(days=6)
        quote = Quote.objects.create(
            invoice=invoice, rail_account=rail or self.rail, network=network, asset="USDC", address=EVM,
            amount_base_units="1", min_accept_base_units="1", ceiling_base_units="2", invoice_amount_minor=10000,
            issued_at=issued, expires_at=issued + datetime.timedelta(minutes=30), state=state,
            closed_at=issued + datetime.timedelta(minutes=30))
        return invoice, quote

    def status(self, invoice):
        return Invoice.objects.get(pk=invoice.pk).status

    def test_a_quote_still_holding_its_amount_keeps_the_order(self):
        from apps.quotes.models import Quote

        invoice, quote = self.quoted()  # closed on day 6, its amount held for a day after
        with self.later(days=7, minutes=1):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "sent")
        with self.later(days=8):  # the hold ran out, but the release job has not freed it yet
            links.run_jobs()
        self.assertEqual(self.status(invoice), "sent")
        Quote.objects.filter(pk=quote.pk).update(state="released")
        with self.later(days=8):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "void")

    def test_a_failing_or_lagging_watcher_of_the_quoted_rail_keeps_the_order(self):
        from apps.quotes.models import Quote
        from apps.rails import health

        for name in (f"scan:evm:arbitrum:{self.rail.pk}", f"ingest:evm:arbitrum:{self.rail.pk}"):
            for kind in ("timeout", "backlog"):
                invoice, quote = self.quoted()
                Quote.objects.filter(pk=quote.pk).update(state="released")
                health.failed(name, kind)
                health.failed(f"scan:evm:ethereum:{self.rail.pk}", "timeout")  # another network: no bearing
                with self.later(days=9):
                    links.run_jobs()
                self.assertEqual(self.status(invoice), "sent", (name, kind))
                health.ok(name)
                with self.later(days=9):
                    links.run_jobs()
                self.assertEqual(self.status(invoice), "void", (name, kind))

    def test_a_failing_gateway_of_a_gateway_quote_keeps_the_order(self):
        from apps.quotes.models import Quote
        from apps.rails import health

        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"})
        invoice, quote = self.quoted(network="bitcoin", rail=btcpay)
        Quote.objects.filter(pk=quote.pk).update(state="released", address="", read_at=timezone.now())
        health.failed(f"btcpay:{btcpay.pk}", "timeout")
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "sent")
        health.ok(f"btcpay:{btcpay.pk}")
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "void")

    def test_blocks_not_scanned_on_the_quoted_rail_keep_the_order_until_acknowledged(self):
        from django.contrib.auth import get_user_model

        from apps.quotes.models import Quote
        from apps.rails import health
        from apps.rails.models import ScanGap

        invoice, quote = self.quoted()
        Quote.objects.filter(pk=quote.pk).update(state="released")
        elsewhere = ScanGap.objects.create(network="ethereum", address=EVM, from_block=1, to_block=9)
        gap = ScanGap.objects.create(network="arbitrum", address=EVM.upper().replace("0X", "0x"), from_block=11,
                                     to_block=8000)
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "sent")
        health.acknowledge_gap(gap, get_user_model().objects.create_user("clerk", is_staff=True), "checked")
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "void")
        self.assertIsNone(ScanGap.objects.get(pk=elsewhere.pk).acknowledged_at)


class VoidReadsOnlyWatchersThatStillLookTests(LinkBase):
    """Residual R3: a BTCPay account refusing new quotes for its speed policy can still read, and a
    retired account nothing scans any more leaves a stale failing row: neither keeps the void waiting."""

    quoted = VoidWaitsWhileNobodyCouldLookTests.quoted
    status = VoidWaitsWhileNobodyCouldLookTests.status

    def released(self, **kw):
        from apps.quotes.models import Quote

        invoice, quote = self.quoted(**kw)
        Quote.objects.filter(pk=quote.pk).update(state="released", address=kw.get("address", quote.address),
                                                 read_at=timezone.now())
        return invoice

    def test_a_speed_policy_refusal_is_not_could_not_look(self):
        from apps.rails import health

        btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                            config={"url": "https://btcpay.example", "store_id": "S1"})
        invoice = self.released(network="bitcoin", rail=btcpay)
        health.failed(f"btcpay:{btcpay.pk}", "speed_policy")
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "void")

    def test_a_retired_accounts_stale_failing_row_does_not_keep_the_order(self):
        from apps.rails import health

        invoice = self.released()
        RailAccount.objects.filter(pk=self.rail.pk).update(active=False)  # retired; no unreleased quote left
        health.failed(f"scan:evm:arbitrum:{self.rail.pk}", "timeout")  # its last scan failed, then nothing more
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "void")

    def test_a_switched_off_account_still_watched_for_another_quote_still_counts(self):
        from apps.quotes.models import Quote
        from apps.rails import health

        invoice = self.released()
        other, _p = self.checkout(email="other@example.com")
        Quote.objects.create(invoice=other, rail_account=self.rail, network="arbitrum", asset="USDC", address=EVM,
                             amount_base_units="9", min_accept_base_units="9", ceiling_base_units="9",
                             invoice_amount_minor=10000, issued_at=timezone.now(),
                             expires_at=timezone.now() + datetime.timedelta(minutes=30))
        RailAccount.objects.filter(pk=self.rail.pk).update(active=False)  # still scanned for that open quote
        health.failed(f"scan:evm:arbitrum:{self.rail.pk}", "timeout")
        with self.later(days=9):
            links.run_jobs()
        self.assertEqual(self.status(invoice), "sent")


class PublicPageTests(LinkBase):
    def test_the_page_shows_the_link(self):
        link, token = self.make_link(description="One hour with a lawyer.", tax_rate_bps=2000, max_quantity=5)
        links.add_coupon(link, code="SPRING", percent_bps=1000)
        r = self.page(token=token)
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        for text in ("Residency consultation", "One hour with a lawyer.", "Liberation Travel s.r.o.", "100.00",
                     'name="quantity"', 'name="us_tax_resident"', 'name="coupon"', 'name="email"'):
            self.assertIn(text, page)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(r["X-Robots-Tag"], "noindex")
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])

    def test_promo_code_prefills_the_coupon_without_saying_whether_it_is_valid(self):
        """A GET must not be an oracle for coupon codes: valid or not, the page is the same apart from the
        value filled in. The code is checked only by the POST, which counts against the checkout limits."""
        links.add_coupon(self.link, code="SPRING", percent_bps=1500)
        valid = self.page(query="?promoCode=SPRING").content.decode()
        self.assertRegex(valid, r'name="coupon"[^>]*value="SPRING"')
        guess = self.page(query="?promoCode=WINTER").content.decode()
        self.assertRegex(guess, r'name="coupon"[^>]*value="WINTER"')
        strip = lambda html: re.sub(r'value="(SPRING|WINTER)"|csrfmiddlewaretoken" value="[^"]+"|nonce="[^"]+"|'
                                    r'name="next" value="[^"]+"', "", html)
        self.assertEqual(strip(valid), strip(guess))
        self.assertNotIn("15 %", valid)

    def test_a_link_without_coupons_asks_for_none(self):
        page = self.page(query="?promoCode=SPRING").content.decode()
        self.assertNotIn('name="coupon"', page)
        off = links.add_coupon(self.link, code="OFF", percent_bps=1000)
        links.set_coupon_active(off, False, actor=None)
        self.assertNotIn('name="coupon"', self.page().content.decode())

    def test_checkout_redirects_to_the_pay_page_in_the_page_language(self):
        r = self.post(self.form(), query="?lang=sk")
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r["Location"], r"^/p/[A-Za-z0-9_-]+/\?lang=sk$")
        page = self.client.get(r["Location"])
        self.assertContains(page, 'lang="sk"')

    def test_the_pay_page_of_an_order_says_to_keep_it(self):
        r = self.post(self.form())
        self.assertContains(self.client.get(r["Location"]), "Keep this page")
        from apps.invoices.services import create_invoice, send_invoice

        contact, _ = find_or_create_contact(self.org, "plain@example.com", legal_name="Plain")
        draft, _ = create_invoice(self.org, contact, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])
        _sent, _v, pay = send_invoice(draft, email_client=False)
        self.assertNotContains(self.client.get(f"/p/{pay}/"), "Keep this page")

    def test_required_fields_and_answers_are_kept_on_the_order(self):
        link, token = self.make_link(billing_field="required", shipping_field="optional", phone_field="required",
                                     custom_fields=[{"label": "Passport country", "required": True},
                                                    {"label": "Anything else?", "required": False}])
        r = self.post(self.form(), token=token)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Invoice.objects.filter(payment_link=link).exists())
        r = self.post(self.form(billing_line1="Road 1", billing_city="Graz", billing_country="Austria", phone="+43 1",
                                shipping_line1="Dock 3", shipping_city="Linz", shipping_country="Austria",
                                custom_0="Slovakia"), token=token)
        self.assertEqual(r.status_code, 302, r.content[:800])
        answers = Invoice.objects.get(payment_link=link).link_order.answers
        self.assertEqual(answers["phone"], "+43 1")
        self.assertEqual(answers["shipping"]["city"], "Linz")
        self.assertEqual(answers["custom"], [{"label": "Passport country", "value": "Slovakia"}])

    def test_the_us_question_is_always_asked(self):
        data = self.form()
        del data["us_tax_resident"]
        self.assertEqual(self.post(data).status_code, 400)
        self.assertFalse(Invoice.objects.exists())

    def test_customer_chosen_amount_on_the_page(self):
        link, token = self.make_link(customer_chooses=True, price_minor=None, min_minor=1000, max_minor=5000)
        page = self.page(token=token).content.decode()
        self.assertIn('name="amount"', page)
        self.assertIn("10.00", page)
        self.assertEqual(self.post(self.form(amount="9.99"), token=token).status_code, 400)
        r = self.post(self.form(amount="25.50"), token=token)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(totals(Invoice.objects.get(payment_link=link)).total, 2550)

    def test_nine_languages(self):
        from django.conf import settings

        english = self.page(query="?lang=en").content.decode()
        self.assertIn("Continue to payment", english)
        for code, _name in settings.LANGUAGES:
            r = self.page(query=f"?lang={code}")
            self.assertContains(r, f'<html lang="{code}"')
            if code != "en":
                self.assertNotContains(r, "Continue to payment")
                self.assertNotContains(r, "Are you a US tax resident?")
            gone = self.page(token=self.make_link(active=False)[1], query=f"?lang={code}")
            self.assertEqual(gone.status_code, 410)
            if code != "en":
                self.assertNotContains(gone, "no longer available", status_code=410)


class TokenNeverLoggedTests(LinkBase):
    """Django logs every answer of 400 and above with its path; on /l/ the path is the token (I-1)."""

    def logged(self, call):
        """What production's console handler would write for `call`: the records django.request emits,
        through that handler's own filters (production logs this logger at ERROR, so the 503 reaches the
        journal; a 4xx reaches it wherever the level is lower)."""
        from django.utils.module_loading import import_string

        from apps.core.tests_settings import production_settings

        conf = production_settings().LOGGING
        filters = [import_string(conf["filters"][name]["()"])() for name in conf["handlers"]["console"]["filters"]]
        with self.assertLogs("django.request", "WARNING") as cm:
            response = call()
        lines = []
        for record in cm.records:
            self.assertTrue(all(f.filter(record) for f in filters))
            lines.append(record.getMessage())
        return response, lines

    def test_refused_checkouts_and_gone_links_log_no_token(self):
        _gone_link, gone = self.make_link(active=False)
        cases = [
            lambda: self.post({}),  # 400: an invalid form
            lambda: self.page(token=gone),  # 410
        ]
        for call in cases:
            response, lines = self.logged(call)
            self.assertTrue(lines)
            for line in lines:
                self.assertNotIn(self.token, line)
                self.assertNotIn(gone, line)
                self.assertIn("/l/<token>/", line)

    def test_a_sealed_answer_logs_no_token(self):
        vault.seal()
        response, lines = self.logged(lambda: self.page())
        self.assertEqual(response.status_code, 503)
        self.assertTrue(lines)
        self.assertFalse([line for line in lines if self.token in line])

    def test_every_capability_prefix_is_redacted_and_marked_in_apache(self):
        import logging

        from django.conf import settings

        from apps.core.client_pages import CAPABILITY_PREFIXES
        from apps.core.logfilters import RedactCapabilityTokens

        conf = (settings.BASE_DIR / "deploy/apache/pay-invoicing-ssl.conf.example").read_text()
        letters = "|".join(prefix.strip("/") for prefix in CAPABILITY_PREFIXES)
        self.assertIn(f'<LocationMatch "^/({letters})/">', conf)
        self.assertIn(f'SetEnvIf Request_URI "^/({letters})/" capability', conf)
        for prefix in CAPABILITY_PREFIXES:
            record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, "Gone: %s",
                                       (f"{prefix}Zr4_8xQ-tok3n/",), None)
            RedactCapabilityTokens().filter(record)
            self.assertEqual(record.getMessage(), f"Gone: {prefix}<token>/", prefix)

    def test_the_theme_switch_after_a_refused_checkout_returns_to_the_link(self):
        page = self.post({}).content.decode()  # a POST with no Referer
        self.assertIn(f'name="next" value="/l/{self.token}/"', page)


class AbuseTests(LinkBase):
    def test_five_submissions_per_ten_minutes_per_address(self):
        for _ in range(5):
            self.assertEqual(self.post({}, REMOTE_ADDR="203.0.113.7").status_code, 400)
        self.assertEqual(self.post(self.form(), REMOTE_ADDR="203.0.113.7").status_code, 429)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(self.post(self.form(), REMOTE_ADDR="203.0.113.8").status_code, 302)

    def test_fifty_completed_checkouts_per_hour_per_link(self):
        """Final review m4: only a completed checkout counts toward the link's 50 an hour. Refused forms
        count toward the address's 5 in 10 minutes alone, so garbage cannot use up a link for its buyers."""
        for i in range(60):
            self.assertEqual(self.post({}, REMOTE_ADDR=f"198.51.100.{i}").status_code, 400)
        for i in range(50):
            r = self.post(self.form(email=f"b{i}@example.com"), REMOTE_ADDR=f"192.0.2.{i}")
            self.assertEqual(r.status_code, 302, i)
        self.assertEqual(self.post(self.form(), REMOTE_ADDR="192.0.2.200").status_code, 429)
        self.assertEqual(Invoice.objects.filter(payment_link=self.link).count(), 50)
        _other, token = self.make_link()
        self.assertEqual(self.post(self.form(), token=token, REMOTE_ADDR="192.0.2.201").status_code, 302)

    def test_the_service_enforces_the_bounds_whatever_the_form_says(self):
        link, _t = self.make_link(customer_chooses=True, price_minor=None, max_minor=100)
        with self.assertRaises(links.CheckoutError):
            self.checkout(link, amount_minor=10**13)


class SealedTests(LinkBase):
    def test_the_payment_path_hook_runs_sealed(self):
        """`order_paid` runs inside `apply_payments`, which a watcher may reach while the vault is sealed."""
        from django.db import transaction

        from apps.invoices.services import lock_invoice

        link, _t = self.make_link(max_uses=1, notify_staff=True)
        invoice, _pay = self.checkout(link)
        with self.later(minutes=61):
            other, _p = self.checkout(link, email="other@example.com")
            self.pay_fully(other)
            vault.seal()
            with transaction.atomic():
                links.order_paid(lock_invoice(invoice.pk))
            links.run_jobs()
        self.assertEqual(LinkOrder.objects.filter(invoice_id=invoice.pk).values_list("hold", flat=True).get(), "used")
        self.assertEqual(LinkAlert.objects.get(kind="over_cap").invoice_id, invoice.pk)
        self.assertTrue(OutboxMessage.objects.filter(kind="order_paid_staff", invoice_id=invoice.pk).exists())

    def test_sealed_vault_answers_503(self):
        vault.seal()
        for r in (self.page(), self.post(self.form())):
            self.assertContains(r, "Temporarily unavailable", status_code=503)
            self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(Invoice.objects.count(), 0)


class ImageTests(LinkBase):
    def jpeg_with_exif(self, size=(64, 32)):
        from PIL import Image

        img = Image.new("RGB", size, (200, 30, 30))
        exif = Image.Exif()
        exif[0x010F] = "SecretCam"  # Make
        exif[0x0131] = "GPS-ish metadata"  # Software
        out = io.BytesIO()
        img.save(out, "JPEG", exif=exif.tobytes())
        return out.getvalue()

    def test_an_image_is_reencoded_without_metadata_and_served_in_the_page(self):
        raw = self.jpeg_with_exif()
        self.assertIn(b"SecretCam", raw)
        links.set_image(self.link, SimpleUploadedFile("a.jpg", raw, "image/jpeg"), actor=None)
        import base64

        stored = self.link.image
        data = base64.b64decode(stored.data)
        self.assertNotIn(b"SecretCam", data)
        self.assertEqual((stored.content_type, stored.width, stored.height), ("image/jpeg", 64, 32))
        page = self.page().content.decode()
        self.assertIn('src="data:image/jpeg;base64,', page)
        self.assertNotIn("/media/", page)

    def test_a_png_loses_its_colour_profile_and_text(self):
        from PIL import Image, PngImagePlugin

        meta = PngImagePlugin.PngInfo()
        meta.add_text("Comment", "SecretCam location")
        out = io.BytesIO()
        Image.new("RGBA", (8, 8), (1, 2, 3, 128)).save(out, "PNG", pnginfo=meta, icc_profile=b"SecretProfile" * 8)
        raw = out.getvalue()
        self.assertIn(b"iCCP", raw)
        links.set_image(self.link, SimpleUploadedFile("a.png", raw, "image/png"), actor=None)
        import base64

        data = base64.b64decode(self.link.image.data)
        self.assertEqual(self.link.image.content_type, "image/png")
        for marker in (b"iCCP", b"tEXt", b"SecretCam", b"eXIf"):
            self.assertNotIn(marker, data)

    def test_a_decompression_bomb_is_refused_before_it_is_decoded(self):
        """A tiny file can declare a huge picture: the size is read from the header and refused before a
        single pixel is decoded (M-2)."""
        import warnings

        from PIL import Image, ImageFile

        for side in (13000, 7000):  # 169 Mpx: Pillow's own bomb error; 49 Mpx: only our cap catches it
            out = io.BytesIO()
            Image.new("1", (side, side)).save(out, "PNG")
            self.assertLess(len(out.getvalue()), 2 * 1024 * 1024)
            with mock.patch.object(ImageFile.ImageFile, "load", side_effect=AssertionError("decoded")), \
                    warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with self.assertRaises(links.LinkError):
                    links.set_image(self.link, SimpleUploadedFile("b.png", out.getvalue(), "image/png"), actor=None)
        self.assertFalse(PaymentLink.objects.filter(pk=self.link.pk, image__isnull=False).exists())

    def test_the_pixel_cap_holds_from_start_up_not_from_the_first_link_image(self):
        """Final review m2: the process-wide cap was lowered only when a link image was first set, so an
        org logo or PDF image read before that met Pillow's own, larger limit. It is set in ready()."""
        import os
        import subprocess
        import sys

        from django.conf import settings

        code = ("import django; django.setup()\n"
                "from PIL import Image; print(Image.MAX_IMAGE_PIXELS)\n")
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"}
        done = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stdout.strip(), str(links.MAX_IMAGE_PIXELS))

    def test_too_large_or_not_an_image_is_refused(self):
        with self.assertRaises(links.LinkError):
            links.set_image(self.link, SimpleUploadedFile("a.png", b"not an image", "image/png"), actor=None)
        big = SimpleUploadedFile("b.jpg", b"\xff\xd8" + b"0" * (2 * 1024 * 1024 + 1), "image/jpeg")
        with self.assertRaises(links.LinkError):
            links.set_image(self.link, big, actor=None)
        from PIL import Image

        gif = io.BytesIO()
        Image.new("RGB", (4, 4)).save(gif, "GIF")
        with self.assertRaises(links.LinkError):
            links.set_image(self.link, SimpleUploadedFile("c.gif", gif.getvalue(), "image/gif"), actor=None)
        self.assertFalse(PaymentLink.objects.get(pk=self.link.pk).__class__.objects.filter(image__isnull=False).exists())


class MigrationTests(UnsealedVaultMixin, TransactionTestCase):
    """The new invoice columns migrate a database that already holds sent invoices (Review Focus 2)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_duplicate_issuerless_items_are_closed_before_the_constraint(self):
        target = [("links", "0001_initial")]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        old = executor.loader.project_state(target).apps
        org = old.get_model("orgs", "Organization").objects.create(slug="m")
        link = old.get_model("links", "PaymentLink").objects.create(org=org, token_hash="c" * 64, currency="EUR")
        Alert = old.get_model("links", "LinkAlert")
        first = Alert.objects.create(kind="no_us_issuer", link=link)
        Alert.objects.create(kind="no_us_issuer", link=link)
        self.migrate_to_latest()
        open_rows = list(LinkAlert.objects.filter(resolved_at__isnull=True).values_list("pk", flat=True))
        self.assertEqual(open_rows, [first.pk])
        self.assertEqual(LinkAlert.objects.count(), 2)

    def test_the_invoice_columns_migrate_a_database_with_sent_invoices(self):
        target = [("invoices", "0008_blank_unkeyed_request_fingerprints"), ("links", None)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        old = executor.loader.project_state([target[0]]).apps
        org = old.get_model("orgs", "Organization").objects.create(slug="m")
        contact = old.get_model("contacts", "Contact").objects.create(org=org)
        inv = old.get_model("invoices", "Invoice").objects.create(
            org=org, requested_by_org=org, contact=contact, currency="EUR", issue_date=datetime.date(2026, 1, 1),
            status="sent", number="2026-0001", view_token_hash="a" * 64, pay_token_hash="b" * 64)
        self.migrate_to_latest()
        row = Invoice.objects.filter(pk=inv.pk).values("payment_link_id", "coupon_id", "offered_pairs",
                                                       "view_token_copy", "status").get()
        self.assertEqual(row, {"payment_link_id": None, "coupon_id": None, "offered_pairs": [],
                               "view_token_copy": None, "status": "sent"})
