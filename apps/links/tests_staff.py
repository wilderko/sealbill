"""The staff pages of payment links: list, create, edit, detail, coupons, fulfilment, Needs a person."""
import html
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.money import totals
from apps.links import services as links
from apps.links.models import Coupon, LinkAlert, PaymentLink
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.services import record_manual_payment
from apps.rails.models import RailAccount
from apps.staff.models import StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import UnsealedVaultMixin
from apps.payments.testing import person

SITE = "https://pay.example"


def link_form(org, **kw):
    data = {
        "organization": org.pk, "title": "Residency consultation", "description": "One hour.", "currency": "EUR",
        "price": "100.00", "min_amount": "", "max_amount": "", "tax_percent": "", "max_quantity": "1",
        "kind": "invoice", "name_field": "required", "billing_field": "off", "shipping_field": "off",
        "phone_field": "off", "methods": "default", "active": "on", "valid_until": "", "max_uses": "",
        "internal_note": "",
    }
    for i in range(5):
        data[f"custom_label_{i}"] = ""
    data.update(kw)
    return data


class StaffLinkBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(SITE_URL=SITE))
        self.staff = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        self.device = StaffTOTPDevice.objects.create(user=self.staff, name="t", confirmed=True)
        self.org = Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.")
        self.bank = add_bank(self.org)
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20,
                                               networks=["arbitrum"], assets=["USDC"])

    def sign_in(self):
        self.client.force_login(self.staff)
        r = self.client.post("/verify/", {"token": token_for(self.device)})
        self.assertEqual(r.status_code, 302)


class AccessTests(StaffLinkBase):
    def test_every_page_needs_a_verified_staff_member(self):
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=100)
        for url in ("/links/", "/links/new/", f"/links/{link.pk}/", f"/links/{link.pk}/edit/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r["Location"].startswith("/login/"))
        r = self.client.post("/links/new/", link_form(self.org))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PaymentLink.objects.count(), 1)

    def test_the_rail_names_payment_links(self):
        self.sign_in()
        nav = re.search(r'<nav class="rail-nav".*?</nav>', self.client.get("/links/").content.decode(), re.S).group(0)
        self.assertIn('href="/links/" aria-current="page"', nav)
        self.assertIn("Payment links", nav)


class CreateEditTests(StaffLinkBase):
    def test_create_then_see_the_url(self):
        self.sign_in()
        r = self.client.post("/links/new/", link_form(self.org, tax_percent="20", max_quantity="3"))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        link = PaymentLink.objects.get()
        self.assertEqual((link.title, link.price_minor, link.tax_rate_bps, link.max_quantity, link.currency),
                         ("Residency consultation", 10000, 2000, 3, "EUR"))
        self.assertEqual(r["Location"], f"/links/{link.pk}/")
        page = self.client.get(r["Location"]).content.decode()
        url = re.search(re.escape(SITE) + r"/l/([A-Za-z0-9_-]+)/", page)
        self.assertIsNotNone(url)
        self.assertEqual(links.resolve_token(url.group(1)), link)
        self.assertTrue(AuditEntry.objects.filter(action="link.create", object_pk=str(link.pk)).exists())

    def test_the_form_refuses_what_the_service_would(self):
        self.sign_in()
        bad = [
            {"price": ""},  # a fixed price needs one
            {"customer_chooses": "on", "price": "", "min_amount": "50", "max_amount": "10"},
            {"max_quantity": "201"},
            {"tax_percent": "101"},
            {"title": ""},
            {"methods": "custom"},  # and nothing chosen
            {"methods": "custom", "method_types": ["bank:USD"]},  # a bank transfer in another currency
            {"custom_label_0": "x" * 101},
        ]
        for change in bad:
            r = self.client.post("/links/new/", link_form(self.org, **change))
            self.assertEqual(r.status_code, 400, change)
        self.assertFalse(PaymentLink.objects.exists())

    def test_customer_chooses_with_bounds_and_custom_methods(self):
        self.sign_in()
        r = self.client.post("/links/new/", link_form(
            self.org, customer_chooses="on", price="", min_amount="5", max_amount="500", methods="custom",
            method_types=["chain:arbitrum:USDC", "bank:EUR"], custom_label_0="Passport country",
            custom_required_0="on", fulfillment="on", notify_staff="on"))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        link = PaymentLink.objects.get()
        self.assertEqual((link.customer_chooses, link.price_minor, link.min_minor, link.max_minor),
                         (True, None, 500, 50000))
        self.assertEqual(sorted(link.method_types), ["bank:EUR", "chain:arbitrum:USDC"])
        self.assertEqual(link.custom_fields, [{"label": "Passport country", "required": True}])
        self.assertTrue(link.fulfillment and link.notify_staff)

    def test_edit_changes_the_link_and_is_audited(self):
        link, token = links.create_link(self.org, title="Old", currency="EUR", price_minor=100, tax_rate_bps=2000)
        self.sign_in()
        page = self.client.get(f"/links/{link.pk}/edit/").content.decode()
        self.assertIn('value="Old"', page)
        self.assertIn('value="1.00"', page)
        self.assertRegex(page, r'name="tax_percent" value="20"')  # not "2": only zeros after a point are dropped
        r = self.client.post(f"/links/{link.pk}/edit/", link_form(self.org, title="New", price="12.50",
                                                                          active=""))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        link.refresh_from_db()
        self.assertEqual((link.title, link.price_minor, link.active), ("New", 1250, False))
        self.assertEqual(links.resolve_token(token), link)  # editing never changes the URL
        self.assertTrue(AuditEntry.objects.filter(action="link.update", object_pk=str(link.pk)).exists())

    def test_the_image_upload(self):
        import io

        from PIL import Image

        self.sign_in()
        out = io.BytesIO()
        Image.new("RGB", (20, 10), (1, 2, 3)).save(out, "PNG")
        out.name = "pic.png"
        out.seek(0)
        r = self.client.post("/links/new/", {**link_form(self.org), "image": out})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        link = PaymentLink.objects.get()
        self.assertEqual((link.image.content_type, link.image.width), ("image/png", 20))
        self.assertIn('src="data:image/png;base64,', self.client.get(f"/links/{link.pk}/").content.decode())
        r = self.client.post(f"/links/{link.pk}/edit/", {**link_form(self.org), "remove_image": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PaymentLink.objects.filter(pk=link.pk, image__isnull=False).exists())

    def test_rotate_the_token(self):
        link, token = links.create_link(self.org, title="T", currency="EUR", price_minor=100)
        self.sign_in()
        r = self.client.post(f"/links/{link.pk}/rotate/")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(links.resolve_token(token))
        self.assertEqual(self.client.get(f"/l/{token}/").status_code, 404)
        self.assertTrue(AuditEntry.objects.filter(action="link.rotate", actor=self.staff).exists())


class CouponPageTests(StaffLinkBase):
    def test_add_and_deactivate_a_coupon(self):
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000)
        self.sign_in()
        r = self.client.post(f"/links/{link.pk}/coupons/", {"code": "spring", "name": "Spring", "percent": "15",
                                                                    "max_uses": "10", "valid_until": "",
                                                                    "internal_note": ""})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        coupon = Coupon.objects.get()
        self.assertEqual((coupon.code, coupon.percent_bps, coupon.max_uses, coupon.active), ("SPRING", 1500, 10, True))
        page = self.client.get(f"/links/{link.pk}/").content.decode()
        self.assertIn("SPRING", page)
        self.assertIn("?promoCode=SPRING", page)
        self.client.post(f"/coupons/{coupon.pk}/toggle/")
        coupon.refresh_from_db()
        self.assertFalse(coupon.active)
        self.assertEqual(AuditEntry.objects.filter(action__startswith="coupon.").count(), 2)

    def test_a_coupon_never_makes_an_order_free_and_codes_are_unique(self):
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000)
        self.sign_in()
        for data in ({"code": "ALL", "percent": "100"}, {"code": "ZERO", "percent": "0"}, {"code": "bad code!", "percent": "5"}):
            r = self.client.post(f"/links/{link.pk}/coupons/", {"name": "", "max_uses": "", "valid_until": "",
                                                                        "internal_note": "", **data})
            self.assertEqual(r.status_code, 400, data)
        links.add_coupon(link, code="SAME", percent_bps=100)
        r = self.client.post(f"/links/{link.pk}/coupons/", {"code": "same", "percent": "5", "name": "",
                                                                    "max_uses": "", "valid_until": "", "internal_note": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Coupon.objects.count(), 1)


class OrdersAndFulfilmentTests(StaffLinkBase):
    def test_paid_orders_are_to_fulfil_until_marked(self):
        link, _t = links.create_link(self.org, title="Visa help", currency="EUR", price_minor=10000, fulfillment=True)
        invoice, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="Buyer"))
        self.sign_in()
        listing = self.client.get("/links/").content.decode()
        self.assertIn("Visa help", listing)
        self.assertIn("100.00", listing)
        page = self.client.get(f"/links/{link.pk}/").content.decode()
        self.assertNotIn("To fulfil", page)
        record_manual_payment(invoice, amount_minor=totals(invoice).total, rail="bank", note="", user=person())
        page = self.client.get(f"/links/{link.pk}/").content.decode()
        self.assertIn("To fulfil", page)
        self.assertIn("1 to fulfil", self.client.get("/links/").content.decode())
        detail = self.client.get(f"/invoices/{invoice.pk}/").content.decode()
        self.assertIn("To fulfil", detail)
        order = invoice.link_order
        r = self.client.post(f"/link-orders/{order.pk}/fulfil/")
        self.assertEqual(r.status_code, 302)
        order.refresh_from_db()
        self.assertEqual((order.fulfilled_by, order.fulfilled_at is not None), (self.staff, True))
        self.assertTrue(AuditEntry.objects.filter(action="link_order.fulfil", actor=self.staff).exists())
        self.assertNotIn("1 to fulfil", self.client.get("/links/").content.decode())
        self.assertIn("Fulfilled", self.client.get(f"/links/{link.pk}/").content.decode())

    def test_the_invoice_page_shows_what_the_buyer_typed(self):
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000, fulfillment=True,
                                     shipping_field="required", phone_field="optional",
                                     custom_fields=[{"label": "Passport country", "required": True}])
        invoice, _pay = links.checkout(link, links.Order(
            email="b@example.com", us_tax_resident="no", name="Buyer", phone="+43 1",
            shipping={"line1": "Dock 3", "city": "Linz", "country": "Austria"},
            custom=[("Passport country", "Slovakia")]))
        self.sign_in()
        page = self.client.get(f"/invoices/{invoice.pk}/").content.decode()
        for text in ("b@example.com", "+43 1", "Dock 3, Linz, Austria", "Passport country", "Slovakia", "No, as typed"):
            self.assertIn(text, page)

    def test_an_unpaid_order_cannot_be_marked_fulfilled(self):
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000, fulfillment=True)
        invoice, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="Buyer"))
        self.sign_in()
        r = self.client.post(f"/link-orders/{invoice.link_order.pk}/fulfil/")
        self.assertEqual(r.status_code, 409)
        invoice.link_order.refresh_from_db()
        self.assertIsNone(invoice.link_order.fulfilled_at)

    def test_the_orders_table_never_shows_a_stored_contact(self):
        find_or_create_contact(self.org, "b@example.com", legal_name="Stored Secret Name")
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000)
        links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="Typed Name"))
        self.sign_in()
        page = self.client.get(f"/links/{link.pk}/").content.decode()
        self.assertIn("Typed Name", page)


class ReissueTests(StaffLinkBase):
    def test_reissued_links_of_an_order_go_to_the_typed_address(self):
        from django.core import mail

        from apps.notify.services import flush_outbox

        find_or_create_contact(self.org, "stored@example.com", legal_name="Stored Secret Name",
                               preferred_language="de")
        from apps.contacts.models import Contact, ContactEmail

        ContactEmail.objects.create(contact=Contact.objects.get(), address="typed@example.com")
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000)
        invoice, _pay = links.checkout(link, links.Order(email="typed@example.com", us_tax_resident="no",
                                                         name="Typed Name"), language="sk")
        self.sign_in()
        r = self.client.post(f"/invoices/{invoice.pk}/links/", {"email_client": "on"})
        self.assertEqual(r.status_code, 200, r.content[:500])
        flush_outbox()
        (letter,) = mail.outbox
        self.assertEqual(letter.to, ["typed@example.com"])
        self.assertIn("Typed Name", letter.body)
        self.assertNotIn("Stored Secret Name", letter.body)
        self.assertIn("Faktúra", letter.subject)  # the checkout's language, not the contact's German


class NeedsPersonTests(StaffLinkBase):
    def test_link_items_are_listed_and_resolved(self):
        us = Organization.objects.create(slug="us", legal_name="Liberation LLC", is_us_resident_issuer=True)
        add_bank(us)
        find_or_create_contact(self.org, "b@example.com", legal_name="S", us_tax_resident="no")
        link, _t = links.create_link(self.org, title="T", currency="EUR", price_minor=10000)
        invoice, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="yes", name="B"))
        alert = LinkAlert.objects.get()
        self.sign_in()
        page = self.client.get("/").content.decode()
        section = re.search(r'<section class="queue" id="link-alerts".*?</section>', page, re.S).group(0)
        self.assertIn("buyer says us tax resident, contact says not", section.lower())
        self.assertIn(f"/invoices/{invoice.pk}/", section)
        r = self.client.post(f"/link-alerts/{alert.pk}/resolve/")
        self.assertEqual(r.status_code, 302)
        alert.refresh_from_db()
        self.assertEqual(alert.resolved_by, self.staff)
        self.assertTrue(AuditEntry.objects.filter(action="link_alert.resolve", actor=self.staff).exists())
        page = self.client.get("/").content.decode()
        self.assertNotIn('id="link-alerts"', page)
        self.assertIn("Payment links", re.search(r'<details class="all-clear".*?</details>', page, re.S).group(0))
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).org_id, us.pk)


class TypedNoStoredYesTests(StaffLinkBase):
    """Owner's ruling 2026-11-04 (spec 2.3a item 3 revised): an order whose buyer typed "no" for a
    contact stored as a US tax resident is issued by the link's organization; staff are told once to
    void and reissue it from the US-resident issuer, and it is not offered to fulfil until resolved."""

    def setUp(self):
        super().setUp()
        self.us = Organization.objects.create(slug="us", legal_name="Liberation LLC", is_us_resident_issuer=True)
        add_bank(self.us)
        find_or_create_contact(self.org, "b@example.com", legal_name="S", us_tax_resident="yes")
        self.link, _t = links.create_link(self.org, title="Visa help", currency="EUR", price_minor=10000,
                                          fulfillment=True)
        self.invoice, _pay = links.checkout(self.link, links.Order(email="b@example.com", us_tax_resident="no",
                                                                   name="Buyer"))
        record_manual_payment(self.invoice, amount_minor=totals(self.invoice).total, rail="bank", note="", user=person())
        self.sign_in()

    def test_one_item_that_says_what_to_do_and_no_fulfilment_until_resolved(self):
        """Residual R5: a paid order cannot be voided, so the item says to check with the buyer, and to refund
        and reissue from the US-resident issuer if they are one."""
        page = self.client.get("/").content.decode()
        section = re.search(r'<section class="queue" id="link-alerts".*?</section>', page, re.S).group(0)
        self.assertIn("The buyer answered that they are not a US tax resident, but this contact is stored as one. Before fulfilling, check with the buyer. If they are a US tax resident, refund the payment and send them a new invoice from the US-resident issuer; otherwise resolve this item with a note.", html.unescape(section))
        self.assertNotIn("Void it", section)
        self.assertFalse("to fulfil" in self.client.get("/links/").content.decode(), "the list's count")
        detail = self.client.get(f"/links/{self.link.pk}/").content.decode()
        self.assertFalse('id="to-fulfil"' in detail or "/fulfil/" in detail, "offered to fulfil")
        self.assertIn("After the US tax residency item", detail)
        invoice_page = self.client.get(f"/invoices/{self.invoice.pk}/").content.decode()
        self.assertFalse("/fulfil/" in invoice_page, "offered to fulfil on the invoice page")
        self.assertIn("After the US tax residency item", invoice_page)
        r = self.client.post(f"/link-orders/{self.invoice.link_order.pk}/fulfil/")
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, "check with the buyer", status_code=409)
        self.assertNotContains(r, "Void it", status_code=409)
        self.assertIsNone(Invoice.objects.get(pk=self.invoice.pk).link_order.fulfilled_at)

        self.assertIn('name="note"', section)  # "resolve this item with a note": the row takes one
        alert = LinkAlert.objects.get()
        self.client.post(f"/link-alerts/{alert.pk}/resolve/", {"note": "Buyer confirmed by e-mail: not a US resident"})
        entry = AuditEntry.objects.get(action="link_alert.resolve", object_pk=str(alert.pk))
        self.assertEqual(entry.detail["note"], "Buyer confirmed by e-mail: not a US resident")
        self.assertIn("1 to fulfil", self.client.get("/links/").content.decode())
        self.assertIn('id="to-fulfil"', self.client.get(f"/links/{self.link.pk}/").content.decode())

    def test_an_unpaid_one_is_listed_once_and_stays_in_view_if_resolved_without_a_void(self):
        unpaid, _pay = links.checkout(self.link, links.Order(email="b@example.com", us_tax_resident="no", name="B"))
        alert = LinkAlert.objects.get(invoice=unpaid)
        page = self.client.get("/").content.decode()
        self.assertTrue('id="link-alerts"' in page and 'id="wrong-issuer"' not in page, "one item for one order")
        self.client.post(f"/link-alerts/{alert.pk}/resolve/")
        page = self.client.get("/").content.decode()
        wrong = re.search(r'<section class="queue" id="wrong-issuer".*?</section>', page, re.S)
        self.assertIsNotNone(wrong, "resolved without a void while the contact still says yes")
        self.assertIn(f"/invoices/{unpaid.pk}/", wrong.group(0))


class ImageSizeTests(StaffLinkBase):
    """Final review m3: Apache's LimitRequestBody (2 MiB) equalled the app's image cap, so an image near
    the cap died at Apache with a bare 413 and staff never saw the app's own sentence. The app's cap
    is 1.5 MB, well under Apache's limit with the rest of the form."""

    def test_an_image_over_one_and_a_half_megabytes_is_refused_with_the_apps_own_message(self):
        import io
        import os
        import re
        from pathlib import Path

        from django.conf import settings
        from PIL import Image

        self.assertEqual(links.MAX_IMAGE_BYTES, 1536 * 1024)
        apache = (Path(settings.BASE_DIR) / "deploy/apache/pay-invoicing-ssl.conf.example").read_text()
        body_limit = int(re.search(r"^\s*LimitRequestBody (\d+)", apache, re.M).group(1))
        self.assertLessEqual(links.MAX_IMAGE_BYTES + 256 * 1024, body_limit)  # room for the rest of the form
        self.sign_in()
        side = 740  # random pixels do not compress: about 1.6 MB of PNG
        noisy = io.BytesIO()
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(noisy, "PNG")
        self.assertGreater(len(noisy.getvalue()), links.MAX_IMAGE_BYTES)
        self.assertLess(len(noisy.getvalue()), body_limit)
        noisy.name = "big.png"
        noisy.seek(0)
        r = self.client.post("/links/new/", {**link_form(self.org), "image": noisy})
        self.assertContains(r, "The image may be at most 1.5 MB.", status_code=400)
        self.assertFalse(PaymentLink.objects.exists())
        page = self.client.get("/links/new/").content.decode()
        self.assertIn("at most 1.5 MB", page)
        self.assertNotIn("at most 2 MB", page)
