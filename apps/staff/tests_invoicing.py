"""Creating, editing, sending and re-linking invoices on the staff pages."""
import datetime
import email
import re
import tempfile
import uuid
from email import policy
from shutil import rmtree

from django.core import mail
from django.db import connection
from django.test import override_settings

from apps.audit.models import AuditEntry
from apps.contacts.services import find_or_create_contact
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.keys.models import EncryptionKey
from apps.keys.tests_delivery import ALICE, ALICE_FPR, fixture, header, parsed, parts_of, pgp_decrypt
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.staff.tests import StaffBase
from apps.vault.testing import TEST_VAULT_PASSWORD

from apps.invoices.errors import InvoiceError as InvoiceErrorForTest  # noqa: E402

LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 133700}


class InvoicingBase(StaffBase):
    def setUp(self):
        super().setUp()
        self.us = Organization.objects.create(slug="us", legal_name="Seller LLC", is_us_resident_issuer=True,
                                              email_from="billing@seller-llc.example")
        add_bank(self.us)
        self.sign_in()

    def payload(self, **over):
        data = {
            "organization": self.org.pk, "kind": "invoice", "email": "new@example.com", "legal_name": "New Client",
            "display_name": "", "tax_ids": "", "address_line1": "", "address_line2": "", "address_city": "",
            "address_zip": "", "address_state": "", "address_country": "", "preferred_language": "en",
            "us_tax_resident": "no", "currency": "EUR", "issue_date": "2026-11-03", "due_days": "14",
            "title": "Work", "notes": "", "form_token": str(uuid.uuid4()),
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-name": "Consulting", "lines-0-description": "", "lines-0-quantity": "2",
            "lines-0-unit_price": "12.34", "lines-0-tax_percent": "0", "lines-0-discount_percent": "0",
            "lines-1-name": "", "lines-1-description": "", "lines-1-quantity": "", "lines-1-unit_price": "",
            "lines-1-tax_percent": "", "lines-1-discount_percent": "",
        }
        data.update(over)
        if "methods" not in over:
            issuer = self.us if data["us_tax_resident"] == "yes" else self.org
            bank = issuer.bank_accounts.filter(currency=data["currency"]).first() or issuer.bank_accounts.first()
            data["methods"] = [f"bank:{bank.pk}"]
        return data

    def newest(self):
        return Invoice.objects.order_by("-pk").first()

    def draft(self):
        return create_invoice(self.org, self.contact, currency="EUR", lines=[LINE])[0]


class CreateTests(InvoicingBase):
    def test_create_invoice_with_lines_in_major_units(self):
        r = self.client.post("/invoices/new/", self.payload())
        self.assertEqual(r.status_code, 302, r.content[:2000])
        inv = self.newest()
        self.assertEqual(r["Location"], f"/invoices/{inv.pk}/")
        line = inv.lines.get()
        self.assertEqual(line.unit_price_minor, 1234)
        self.assertEqual(str(line.quantity), "2.00")
        from apps.invoices.money import totals
        self.assertEqual(totals(inv).total, 2468)
        self.assertEqual(inv.due_date, datetime.date(2026, 11, 17))
        self.assertEqual(inv.contact.legal_name, "New Client")
        self.assertTrue(inv.offered_methods)  # the issuer's bank account by default

    def test_excess_precision_refused(self):
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-unit_price": "12.345"}))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "at most 2 decimal places", status_code=400)
        self.assertFalse(Invoice.objects.filter(status="draft").exists())
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-unit_price": "999999999999"}))
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-unit_price": "1e2"}))
        self.assertEqual(r.status_code, 400)

    def test_zero_decimal_currency_takes_whole_units_only(self):
        r = self.client.post("/invoices/new/", self.payload(currency="JPY", **{"lines-0-unit_price": "100.5"}))
        self.assertEqual(r.status_code, 400)

    def test_invalid_input_rerenders_with_the_same_token(self):
        token = str(uuid.uuid4())
        r = self.client.post("/invoices/new/", self.payload(form_token=token, email="not-an-address"))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, f'value="{token}"', status_code=400)

    def test_us_resident_contact_is_issued_by_us_issuer_and_page_says_why(self):
        r = self.client.post("/invoices/new/", self.payload(us_tax_resident="yes"))
        self.assertEqual(r.status_code, 302)
        inv = self.newest()
        self.assertEqual(inv.org, self.us)
        self.assertEqual(inv.requested_by_org, self.org)
        self.assertTrue(inv.issued_by_rule)
        page = self.client.get(r["Location"])
        self.assertContains(page, "US tax resident rule")
        self.assertContains(page, "Issued by <strong>Seller LLC</strong>")  # the issuer's name, not its slug
        flag = AuditEntry.objects.filter(action="contact.create").latest("pk")
        self.assertEqual(flag.detail["us_tax_resident"], "yes")

    def test_changing_the_us_flag_of_an_existing_contact_is_audited(self):
        self.client.post("/invoices/new/", self.payload(email="a@example.com", us_tax_resident="yes"))
        entry = AuditEntry.objects.filter(action="contact.update").latest("pk")
        self.assertEqual(entry.detail["us_tax_resident"], ["unknown", "yes"])

    def test_double_submit_creates_one_invoice(self):
        data = self.payload()
        first = self.client.post("/invoices/new/", data)
        second = self.client.post("/invoices/new/", data)
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(Invoice.objects.filter(status="draft").count(), 1)

    def test_payment_request_created_without_number(self):
        r = self.client.post("/invoices/new/", self.payload(kind="payment_request", due_days=""))
        self.assertEqual(r.status_code, 302)
        inv = self.newest()
        self.assertEqual(inv.kind, "payment_request")
        self.assertIsNone(inv.number)
        self.assertIsNone(inv.due_date)
        self.client.post(f"/invoices/{inv.pk}/send/")
        inv.refresh_from_db()
        self.assertEqual(inv.status, "sent")
        self.assertIsNone(inv.number)

    def test_a_chosen_method_must_belong_to_the_issuer(self):
        other = Organization.objects.create(slug="other", legal_name="Other")
        foreign = add_bank(other)
        r = self.client.post("/invoices/new/", self.payload(methods=[f"bank:{foreign.pk}"]))
        self.assertEqual(r.status_code, 400)

    def test_methods_partial_follows_currency_and_us_flag(self):
        r = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                         "us_tax_resident": "no", "email": "x@example.com"})
        self.assertContains(r, "bank:")
        self.assertContains(r, "Issued by")
        self.assertEqual(r.context["issuer"], self.org)
        self.assertNotContains(r, "<html")
        r = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                         "us_tax_resident": "yes"})
        self.assertContains(r, "US tax resident rule")
        self.assertEqual(r.context["issuer"], self.us)
        usd = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "USD",
                                                           "us_tax_resident": "no"})
        self.assertNotContains(usd, "bank:")
        bad = self.client.get("/invoices/methods/", {"organization": "x", "currency": "EUR"})
        self.assertEqual(bad.status_code, 400)

    def test_the_form_loads_htmx_under_the_nonce_without_inline_handlers(self):
        r = self.client.get("/invoices/new/")
        self.assertContains(r, "htmx.min.js")
        self.assertNotRegex(r.content.decode(), r"\son[a-z]+=")
        self.assertNotIn("<script>", r.content.decode())


class AmountTextTests(InvoicingBase):
    def setUp(self):
        super().setUp()
        add_bank(self.org, currency="PYG")

    def post_price(self, price, currency="EUR"):
        return self.client.post("/invoices/new/", self.payload(currency=currency, **{"lines-0-unit_price": price}))

    def test_thousands_separator_refused_eur(self):
        for text in ("1,000", "1.000", "1,000.00", "12.500"):
            r = self.post_price(text)
            self.assertEqual(r.status_code, 400, text)
        self.assertContains(self.post_price("1,000"), "no thousands separators", status_code=400)

    def test_thousands_separator_refused_pyg(self):
        for text in ("100.000", "100,000", "1.5"):
            self.assertEqual(self.post_price(text, "PYG").status_code, 400, text)
        r = self.post_price("100000", "PYG")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest().lines.get().unit_price_minor, 100000)

    def test_comma_decimal_accepted_eur(self):
        r = self.post_price("12,34")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.newest().lines.get().unit_price_minor, 1234)

    def test_quantity_thousands_separator_refused(self):
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-quantity": "1,000"}))
        self.assertEqual(r.status_code, 400)

    def test_ambiguous_amounts_name_both_readings(self):
        for over, currency, places in (({"lines-0-unit_price": "12.345"}, "EUR", 2),
                                       ({"lines-0-unit_price": "100.5"}, "PYG", 0),
                                       ({"lines-0-tax_percent": "0.125"}, "EUR", 2),
                                       ({"lines-0-unit_price": "12.3456"}, "EUR", 2)):
            r = self.client.post("/invoices/new/", self.payload(currency=currency, **over))
            self.assertContains(r, f"at most {places} decimal places, and no thousands separators",
                                status_code=400, msg_prefix=str(over))

    def test_leading_point_is_a_decimal(self):
        from apps.staff.forms import parse_minor
        self.assertEqual(parse_minor(".5", "EUR"), 50)
        self.assertEqual(parse_minor(",5", "EUR"), 50)
        r = self.client.post("/invoices/new/", self.payload(**{
            "lines-0-unit_price": ".5", "lines-0-quantity": ".5", "lines-0-tax_percent": ".5",
            "lines-0-discount_percent": ".5"}))
        self.assertEqual(r.status_code, 302, r.content[:2000])
        line = self.newest().lines.get()
        self.assertEqual((line.unit_price_minor, str(line.quantity), line.tax_rate_bps, line.discount_rate_bps),
                         (50, "0.50", 50, 50))
        for text in (".", ",", "", "5."):
            self.assertEqual(self.post_price(text).status_code, 400, text)

    def test_parse_minor_uses_the_same_rules(self):
        from django import forms
        from apps.staff.forms import parse_minor
        self.assertEqual(parse_minor("12,34", "EUR"), 1234)
        for text in ("1,000", "1.000"):
            with self.assertRaises(forms.ValidationError):
                parse_minor(text, "EUR")
        with self.assertRaises(forms.ValidationError):
            parse_minor("100.000", "PYG")


class ContactRulingTests(InvoicingBase):
    def test_unknown_answer_never_downgrades_a_stored_yes(self):
        c, _ = find_or_create_contact(self.org, "us@example.com", legal_name="Sam", us_tax_resident="yes")
        r = self.client.post("/invoices/new/", self.payload(email="us@example.com", us_tax_resident="unknown",
                                                              methods=[f"bank:{self.us.bank_accounts.first().pk}"]))
        self.assertEqual(r.status_code, 302, r.content[:1500])
        inv = self.newest()
        self.assertEqual(inv.org, self.us)
        c.refresh_from_db()
        self.assertEqual(c.us_tax_resident, "yes")

    def test_unknown_answer_keeps_a_stored_no(self):
        c, _ = find_or_create_contact(self.org, "no@example.com", us_tax_resident="no")
        self.client.post("/invoices/new/", self.payload(email="no@example.com", us_tax_resident="unknown"))
        c.refresh_from_db()
        self.assertEqual(c.us_tax_resident, "no")

    def test_methods_partial_shows_the_stored_answer_for_the_email(self):
        find_or_create_contact(self.org, "us@example.com", us_tax_resident="yes")
        r = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                         "us_tax_resident": "unknown", "email": "us@example.com"})
        self.assertEqual(r.context["issuer"], self.us)
        self.assertContains(r, "Stored answer")

    def test_methods_partial_shows_the_stored_answer_while_the_field_is_blank(self):
        find_or_create_contact(self.org, "us@example.com", us_tax_resident="yes")
        r = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                         "us_tax_resident": "", "email": "us@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["issuer"], self.us)
        self.assertContains(r, "Stored answer")
        r = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                         "us_tax_resident": "", "email": "nobody@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["issuer"], self.org)
        bad = self.client.get("/invoices/methods/", {"organization": self.org.pk, "currency": "EUR",
                                                           "us_tax_resident": "maybe"})
        self.assertEqual(bad.status_code, 400)
        # The form re-rendered with the answer still blank names the same issuer as the partial.
        r = self.client.post("/invoices/new/", self.payload(email="us@example.com", us_tax_resident="",
                                                                  methods=[f"bank:{self.us.bank_accounts.first().pk}"]))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.context["issuer"], self.us)

    def test_contact_edits_reach_the_issuer_copy(self):
        self.client.post("/invoices/new/", self.payload(email="us@example.com", us_tax_resident="yes",
                                                              legal_name="Old Name", title="first"))
        r = self.client.post("/invoices/new/", self.payload(email="us@example.com", us_tax_resident="yes",
                                                                  legal_name="New Name", title="second"))
        self.assertEqual(r.status_code, 302)
        inv = self.newest()
        self.assertEqual(inv.org, self.us)
        self.assertEqual(inv.contact.legal_name, "New Name")

    def test_partial_address_merges_with_stored(self):
        self.client.post("/invoices/new/", self.payload(
            email="addr@example.com", address_line1="Main 1", address_city="Praha", address_country="CZ"))
        self.client.post("/invoices/new/", self.payload(email="addr@example.com", address_city="Brno"))
        from apps.contacts.services import find_contact
        self.assertEqual(find_contact(self.org, "addr@example.com").address,
                         {"line1": "Main 1", "city": "Brno", "country": "CZ"})

    def test_no_method_ticked_is_an_error(self):
        r = self.client.post("/invoices/new/", self.payload(methods=[]))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "at least one payment method", status_code=400)

    def test_zero_total_refused(self):
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-unit_price": "0"}))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "greater than zero", status_code=400)
        r = self.client.post("/invoices/new/", self.payload(**{"lines-0-discount_percent": "100"}))
        self.assertEqual(r.status_code, 400)


class LifecycleTests(InvoicingBase):
    def test_send_twice_is_harmless(self):
        inv = self.draft()
        self.client.post(f"/invoices/{inv.pk}/send/", {"email_client": "on"})
        inv.refresh_from_db()
        self.assertEqual(inv.status, "sent")
        number, sent_at = inv.number, inv.sent_at
        queued = OutboxMessage.objects.count()
        r = self.client.post(f"/invoices/{inv.pk}/send/", {"email_client": "on"})
        self.assertContains(r, "already sent", status_code=409)
        inv.refresh_from_db()
        self.assertEqual((inv.number, inv.sent_at), (number, sent_at))
        self.assertEqual(OutboxMessage.objects.count(), queued)
        self.org.refresh_from_db()
        self.assertEqual(self.org.next_number, 3)  # the setUp invoice took one, this one took one

    def test_send_without_email_queues_nothing(self):
        inv = self.draft()
        self.client.post(f"/invoices/{inv.pk}/send/", {})
        self.assertEqual(OutboxMessage.objects.count(), 0)

    def test_edit_and_delete_only_drafts(self):
        inv = self.draft()
        page = self.client.get(f"/invoices/{inv.pk}/edit/")
        self.assertContains(page, "Consulting")
        data = self.payload(title="Renamed", **{"lines-0-name": "Changed", "lines-0-unit_price": "5.00",
                                                 "lines-0-quantity": "3"})
        data.update({"organization": self.org.pk, "email": ALICE_EMAIL})
        r = self.client.post(f"/invoices/{inv.pk}/edit/", data)
        self.assertEqual(r.status_code, 302, r.content[:2000])
        inv = Invoice.objects.get(pk=inv.pk)
        self.assertEqual(inv.title, "Renamed")
        line = inv.lines.get()
        self.assertEqual((line.name, line.unit_price_minor), ("Changed", 500))

        sent = self.invoice  # sent in setUp
        self.assertEqual(self.client.get(f"/invoices/{sent.pk}/edit/").status_code, 409)
        self.assertEqual(self.client.post(f"/invoices/{sent.pk}/edit/", data).status_code, 409)
        self.assertEqual(self.client.post(f"/invoices/{sent.pk}/delete/").status_code, 409)
        self.assertTrue(Invoice.objects.filter(pk=sent.pk).exists())
        sent.refresh_from_db()
        self.assertNotEqual(sent.title, "Renamed")

        r = self.client.post(f"/invoices/{inv.pk}/delete/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Invoice.objects.filter(pk=inv.pk).exists())
        self.assertTrue(AuditEntry.objects.filter(action="invoice.delete", object_pk=str(inv.pk)).exists())

    def test_reissue_links_shows_links_once_and_can_email_them(self):
        inv = self.invoice
        old = Invoice.objects.get(pk=inv.pk).view_token_hash
        r = self.client.post(f"/invoices/{inv.pk}/links/", {})
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        view = re.search(r"/i/([\w-]+)/", text)
        pay = re.search(r"/p/([\w-]+)/", text)
        self.assertTrue(view and pay, text[:3000])
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertNotIn(view.group(1), r.get("Location", ""))
        self.assertNotEqual(Invoice.objects.get(pk=inv.pk).view_token_hash, old)
        self.assertEqual(r["Cache-Control"], "no-store")
        # the links are on the response only: the detail page does not repeat them
        self.assertNotIn(view.group(1), self.client.get(f"/invoices/{inv.pk}/").content.decode())
        self.assertFalse(any(view.group(1) in str(e.detail) for e in AuditEntry.objects.all()))

        r = self.client.post(f"/invoices/{inv.pk}/links/", {"email_client": "on"})
        self.assertContains(r, "old links")
        self.assertEqual(OutboxMessage.objects.filter(kind="invoice").count(), 1)
        notify.flush_outbox()
        sent = mail.outbox[-1]
        body = sent.body + "".join(content for content, _mime in sent.alternatives)
        shown = re.search(r"/i/([\w-]+)/", r.content.decode()).group(1)
        self.assertIn(shown, body)

    def test_reissue_warns_and_will_not_email_from_the_wrong_issuer(self):
        inv = self.invoice
        self.contact.us_tax_resident = "yes"
        self.contact.save(update_fields=["us_tax_resident"])
        before = Invoice.objects.get(pk=inv.pk).view_token_hash
        page = self.client.get(f"/invoices/{inv.pk}/links/")
        self.assertContains(page, "US tax resident")
        self.assertContains(page, "will not be e-mailed")
        r = self.client.post(f"/invoices/{inv.pk}/links/", {"email_client": "on"})
        self.assertEqual(r.status_code, 409)
        self.assertContains(r, "US tax resident", status_code=409)
        self.assertEqual(Invoice.objects.get(pk=inv.pk).view_token_hash, before)  # nothing reissued
        self.assertEqual(OutboxMessage.objects.count(), 0)
        r = self.client.post(f"/invoices/{inv.pk}/links/", {})  # shown here only: still allowed
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(Invoice.objects.get(pk=inv.pk).view_token_hash, before)
        self.assertEqual(OutboxMessage.objects.count(), 0)

    def test_reissue_cancels_letters_carrying_the_dead_links(self):
        from apps.invoices.services import queue_invoice_letter

        inv = self.invoice
        queued = queue_invoice_letter(inv, "a@example.com", "OLDVIEWTOKEN", "OLDPAYTOKEN")
        failed = queue_invoice_letter(inv, "a@example.com", "OLDVIEWTOKEN", "OLDPAYTOKEN")
        OutboxMessage.objects.filter(pk=failed.pk).update(state="failed", attempts=9)
        other = self.draft()
        send_invoice(other)  # another invoice's letter is not touched
        r = self.client.post(f"/invoices/{inv.pk}/links/", {"email_client": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(OutboxMessage.objects.get(pk=queued.pk).state, "cancelled")
        self.assertEqual(OutboxMessage.objects.get(pk=failed.pk).state, "cancelled")  # a retry would send dead links
        self.assertEqual(OutboxMessage.objects.filter(invoice=other, state="queued").count(), 1)
        self.assertTrue(AuditEntry.objects.filter(action="outbox.cancelled", object_pk=str(queued.pk)).exists())
        notify.flush_outbox()
        bodies = [m.body for m in mail.outbox if m.to == ["a@example.com"]]
        self.assertEqual(len(bodies), 2)  # the new letter, and the other invoice's
        self.assertFalse(any("OLDVIEWTOKEN" in b for b in bodies))
        shown = re.search(r"/i/([\w-]+)/", r.content.decode()).group(1)
        self.assertTrue(any(shown in b for b in bodies))

    def _endpoint(self, events=("*",)):
        from unittest import mock

        from apps.webhooks import delivery
        from apps.webhooks.tests import PUBLIC_IP, addrinfo

        with mock.patch("socket.getaddrinfo", return_value=addrinfo(PUBLIC_IP)):
            return delivery.create_endpoint(self.org, "https://hooks.example.com/in", events=list(events))

    def test_staff_reissue_emits_links_reissued_with_the_invoice_id_only(self):
        from apps.webhooks.models import WebhookDelivery, WebhookEvent

        self._endpoint()
        WebhookEvent.objects.all().delete()
        inv = self.invoice
        r = self.client.post(f"/invoices/{inv.pk}/links/", {})
        self.assertEqual(r.status_code, 200)
        events = list(WebhookEvent.objects.filter(type="invoice.links_reissued"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, {"invoice_id": inv.pk, "status": "sent"})
        self.assertEqual(WebhookDelivery.objects.filter(event=events[0]).count(), 1)

    def test_a_refused_staff_reissue_emits_nothing(self):
        from apps.webhooks.models import WebhookEvent

        self._endpoint()
        WebhookEvent.objects.all().delete()
        r = self.client.post(f"/invoices/{self.draft().pk}/links/", {})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(WebhookEvent.objects.filter(type="invoice.links_reissued").exists())

    def test_a_reissue_that_rolls_back_leaves_no_event(self):
        from unittest import mock

        from apps.webhooks.models import WebhookEvent

        self._endpoint()
        WebhookEvent.objects.all().delete()
        with mock.patch("apps.staff.views.queue_invoice_letter", side_effect=InvoiceErrorForTest("x")):
            self.client.post(f"/invoices/{self.invoice.pk}/links/", {"email_client": "on"})
        self.assertFalse(WebhookEvent.objects.filter(type="invoice.links_reissued").exists())

    def test_reissue_refused_for_a_draft(self):
        r = self.client.post(f"/invoices/{self.draft().pk}/links/", {})
        self.assertEqual(r.status_code, 409)

    def test_get_on_links_does_not_reissue(self):
        before = Invoice.objects.get(pk=self.invoice.pk).view_token_hash
        r = self.client.get(f"/invoices/{self.invoice.pk}/links/")
        self.assertEqual(r.status_code, 200)  # a page that asks, nothing changed
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).view_token_hash, before)

    def test_detail_links_to_keys_and_offers_actions(self):
        page = self.client.get(f"/invoices/{self.invoice.pk}/")
        self.assertContains(page, f"/contacts/{self.invoice.contact_id}/keys/")
        self.assertContains(page, f"/invoices/{self.invoice.pk}/links/")
        d = self.draft()
        page = self.client.get(f"/invoices/{d.pk}/")
        self.assertContains(page, f"/invoices/{d.pk}/send/")
        self.assertContains(page, f"/invoices/{d.pk}/edit/")


ALICE_EMAIL = "a@example.com"


class ListTests(InvoicingBase):
    def test_list_filters_and_caps(self):
        self.draft()
        r = self.client.get("/invoices/")
        self.assertContains(r, self.invoice.number)
        self.assertContains(r, "Ján")
        r = self.client.get("/invoices/", {"status": "draft"})
        self.assertNotContains(r, self.invoice.number)
        r = self.client.get("/invoices/", {"q": self.invoice.number})
        self.assertContains(r, self.invoice.number)
        r = self.client.get("/invoices/", {"organization": self.us.pk})
        self.assertNotContains(r, self.invoice.number)
        self.assertEqual(self.client.get("/invoices/", {"organization": "zz", "status": "zz"}).status_code, 200)

    def test_list_survives_undecryptable_contact_name(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE contacts_contact SET legal_name = %s, display_name = %s",
                        ["gAAAAABnotavalidtoken", "gAAAAABnotavalidtoken"])
        with self.assertLogs("apps.vault.fields", "ERROR"):
            r = self.client.get("/invoices/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.invoice.number)
        self.assertContains(r, "—")

    @override_settings()
    def test_list_is_capped(self):
        from apps.staff import views
        old = views.NEEDS_PERSON_CAP
        views.NEEDS_PERSON_CAP = 1
        self.addCleanup(setattr, views, "NEEDS_PERSON_CAP", old)
        self.draft()
        self.assertContains(self.client.get("/invoices/"), "Showing 1 of 2")


class AccessTests(InvoicingBase):
    def test_all_actions_need_staff_and_post(self):
        inv = self.draft()
        actions = [f"/invoices/{inv.pk}/delete/", f"/invoices/{inv.pk}/send/"]
        pages = ["/invoices/", "/invoices/new/", f"/invoices/{inv.pk}/edit/",
                 f"/invoices/{inv.pk}/links/", "/invoices/methods/"]
        for url in actions:
            self.assertEqual(self.client.get(url).status_code, 405, url)
        self.client.logout()
        for url in actions + pages:
            for method in (self.client.get, self.client.post):
                r = method(url)
                self.assertEqual(r.status_code, 302, url)
                self.assertTrue(r["Location"].startswith("/login/"), url)
        self.assertTrue(Invoice.objects.filter(pk=inv.pk, status="draft").exists())
        self.client.force_login(self.staff)  # password only, no second factor
        for url in actions + pages:
            self.assertTrue(self.client.post(url)["Location"].startswith("/verify/"), url)


class EncryptedBase(InvoicingBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(rmtree, self.tmp, ignore_errors=True)
        o = override_settings(GPG_TMP_DIR=self.tmp, STAFF_NOTIFY_EMAILS=["ops@example.com"])
        o.enable()
        self.addCleanup(o.disable)

    def _us_contact_with_key(self, state="active", failed_while=""):
        contact, _ = find_or_create_contact(self.org, ALICE, legal_name="Alica Nováková", preferred_language="sk",
                                            us_tax_resident="yes")
        key = EncryptionKey.objects.create(contact=contact, kind="pgp", material=fixture("alice_pub.asc"),
                                           fingerprint=ALICE_FPR, state=state, source="staff",
                                           failed_while=failed_while)
        return contact, key


class EncryptedUsIssuerTests(EncryptedBase):

    def test_us_issued_invoice_to_a_contact_with_a_key_is_encrypted(self):
        contact, _ = find_or_create_contact(self.org, ALICE, legal_name="Alica Nováková", preferred_language="sk",
                                            us_tax_resident="yes")
        original = EncryptionKey.objects.create(contact=contact, kind="pgp", material=fixture("alice_pub.asc"),
                                                fingerprint=ALICE_FPR, state="active", source="staff")
        inv = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        self.assertEqual(inv.org, self.us)
        copy_key = inv.contact.encryption_keys.get(state="active")
        self.assertEqual((copy_key.kind, copy_key.fingerprint, copy_key.source, copy_key.mirrors_id),
                         ("pgp", ALICE_FPR, "mirror", original.pk))
        send_invoice(inv)
        notify.flush_outbox()
        sent = mail.outbox[-1]
        outer = parsed(sent)
        self.assertEqual(outer.get_content_type(), "multipart/encrypted")
        self.assertNotIn(b"Consulting", sent.message().as_bytes())
        armored = outer.get_payload()[1].get_payload()
        inner = email.message_from_bytes(pgp_decrypt(armored), policy=policy.default)
        self.assertIn("Alica Nováková", parts_of(inner)["text/plain"])

    def test_an_existing_copy_without_a_key_gets_the_key(self):
        contact, _ = find_or_create_contact(self.org, ALICE, legal_name="Alica", us_tax_resident="yes")
        first = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        self.assertFalse(first.contact.encryption_keys.exists())
        EncryptionKey.objects.create(contact=contact, kind="pgp", material=fixture("alice_pub.asc"),
                                     fingerprint=ALICE_FPR, state="active", source="staff")
        second = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        self.assertEqual(second.contact_id, first.contact_id)
        self.assertEqual(second.contact.encryption_keys.filter(state="active").count(), 1)

    def test_failed_original_key_blocks_the_issuer_copy_too(self):
        contact, _ = self._us_contact_with_key("failed", "active")
        inv = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        send_invoice(inv)
        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        self.assertEqual(len(mail.outbox), 0)  # nothing went out in plaintext
        self.assertEqual(OutboxMessage.objects.get(kind="invoice").state, "failed")

    def test_revoking_the_original_key_reaches_the_issuer_copy(self):
        from apps.keys import services as keys
        contact, key = self._us_contact_with_key()
        first = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        self.assertTrue(first.contact.encryption_keys.filter(state="active").exists())
        keys.revoke_key(key, None)
        second = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        self.assertFalse(second.contact.encryption_keys.filter(state__in=("active", "failed")).exists())

    def test_replacing_the_original_key_reaches_the_issuer_copy(self):
        contact, key = self._us_contact_with_key()
        create_invoice(self.org, contact, currency="EUR", lines=[LINE])
        EncryptionKey.objects.filter(pk=key.pk).update(state="replaced")
        EncryptionKey.objects.create(contact=contact, kind="pgp", material=fixture("alice_pub.asc"),
                                     fingerprint="B" * 40, state="active", source="staff")
        inv = create_invoice(self.org, contact, currency="EUR", lines=[LINE])[0]
        active = inv.contact.encryption_keys.get(state="active")
        self.assertEqual(active.fingerprint, "B" * 40)


class MirrorFollowsItsSourceTests(EncryptedBase):
    """The US issuer keeps one copy of an address per requesting organization (audit API-1: a
    shared copy let one organization read and rewrite another's client, and put its key on it).
    Each copy mirrors the keys of its own original, and each mirror follows the key it mirrors,
    and only that one."""

    def setUp(self):
        super().setUp()
        self.sc = Organization.objects.create(slug="sc", legal_name="Seller Ltd.")
        add_bank(self.sc)

    def _sc_contact(self):
        contact, _ = find_or_create_contact(self.sc, ALICE, legal_name="Alica Nováková", preferred_language="sk",
                                            us_tax_resident="yes")
        return contact

    def _smime_key(self, contact):
        from apps.keys import services as keys
        from apps.keys.tests_validation import make_cert, pem
        return keys.add_key(contact, pem(make_cert()), source="staff", actor=self.staff)

    def _letter_type(self, invoice):
        mail.outbox.clear()
        send_invoice(invoice)
        notify.flush_outbox()
        self.assertEqual(len(mail.outbox), 1)
        return parsed(mail.outbox[0]).get_content_type()

    def _copy_keys(self, copy):
        return {k.mirrors_id: k.state for k in EncryptionKey.objects.filter(contact=copy, source="mirror")}

    def test_two_requesting_orgs_get_a_copy_each(self):
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        b = self._sc_contact()  # the same address in another organization, no key there
        second = create_invoice(self.sc, b, currency="EUR", lines=[LINE])[0]
        self.assertNotEqual(second.contact_id, first.contact_id)  # one copy per address and requester
        self.assertEqual((first.contact.copy_for_org_id, second.contact.copy_for_org_id), (self.org.pk, self.sc.pk))
        self.assertEqual(self._letter_type(first), "multipart/encrypted")
        self.assertEqual(self._letter_type(second), "multipart/mixed")  # never to the key A's organization holds
        mirror = EncryptionKey.objects.get(contact=first.contact, state="active")
        self.assertEqual((mirror.source, mirror.mirrors_id), ("mirror", key_a.pk))
        self.assertFalse(EncryptionKey.objects.filter(contact=second.contact).exists())

    def test_revoking_source_key_revokes_its_mirror_immediately(self):
        from apps.keys import services as keys
        b = self._sc_contact()
        key_b = self._smime_key(b)
        a, key_a = self._us_contact_with_key()
        d1 = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        d2 = create_invoice(self.sc, b, currency="EUR", lines=[LINE])[0]
        self.assertEqual(self._copy_keys(d1.contact), {key_a.pk: "active"})
        self.assertEqual(self._copy_keys(d2.contact), {key_b.pk: "active"})

        keys.revoke_key(key_a, self.staff)  # no invoice in between: the copy follows at once
        self.assertEqual(self._copy_keys(d1.contact), {key_a.pk: "revoked"})
        self.assertEqual(self._copy_keys(d2.contact), {key_b.pk: "active"})  # B's copy is not A's to change
        self.assertEqual(self._letter_type(d1), "multipart/mixed")  # no key left for A's copy, never B's

        keys.revoke_key(key_b, self.staff)
        self.assertEqual(self._copy_keys(d2.contact), {key_b.pk: "revoked"})
        self.assertEqual(self._letter_type(d2), "multipart/mixed")

    def test_a_key_added_later_reaches_only_its_own_copy(self):
        from apps.keys import services as keys
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        b = self._sc_contact()
        b_copy = create_invoice(self.sc, b, currency="EUR", lines=[LINE])[0].contact
        self.assertEqual(self._copy_keys(b_copy), {})

        key_b = self._smime_key(b)  # B's org adds a key: it reaches B's copy without another invoice
        self.assertEqual(self._copy_keys(b_copy), {key_b.pk: "active"})
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "active"})

        create_invoice(self.org, a, currency="EUR", lines=[LINE])  # A invoicing again changes nothing
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "active"})

        keys.revoke_key(key_b, self.staff)
        self.assertEqual(self._copy_keys(b_copy), {key_b.pk: "revoked"})
        self.assertEqual(self._letter_type(first), "multipart/encrypted")

    def test_a_copy_shared_before_the_split_keeps_following_its_mirrors_and_gets_no_new_one(self):
        """A copy made when copies were shared is the issuer's own contact now (it cannot be told
        apart). The mirrors it holds still follow their keys, newest active first, so a withdrawn
        key never stays active there; no new key is mirrored onto it."""
        from apps.keys import services as keys
        b = self._sc_contact()
        key_b = self._smime_key(b)
        a, key_a = self._us_contact_with_key()
        legacy, _ = find_or_create_contact(self.us, ALICE, us_tax_resident="yes")  # copy_for_org NULL
        full_a, full_b = EncryptionKey.objects.get(pk=key_a.pk), EncryptionKey.objects.get(pk=key_b.pk)
        for full, state in ((full_a, "active"), (full_b, "replaced")):
            EncryptionKey.objects.create(contact=legacy, kind=full.kind, material=full.material,
                                         fingerprint=full.fingerprint, expires_at=full.expires_at, source="mirror",
                                         state=state, mirrors=full)

        keys.revoke_key(key_a, self.staff)
        self.assertEqual(self._copy_keys(legacy), {key_a.pk: "revoked", key_b.pk: "active"})
        new = keys.add_key(a, fixture("alice_pub.asc"), source="staff", actor=self.staff)
        self.assertNotIn(new.pk, self._copy_keys(legacy))
        keys.revoke_key(key_b, self.staff)
        self.assertEqual(self._copy_keys(legacy), {key_a.pk: "revoked", key_b.pk: "revoked"})

    def test_failed_mirror_not_reset_by_next_invoice(self):
        from apps.keys import delivery, services as keys
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        mirror = EncryptionKey.objects.get(contact=first.contact, source="mirror")
        self.assertTrue(delivery.mark_failed(mirror, "expired", outbox_pk=0))  # failed on the copy alone
        key_a.refresh_from_db()
        self.assertEqual(key_a.state, "active")

        second = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        self.assertEqual(self._copy_keys(second.contact), {key_a.pk: "failed"})
        send_invoice(second)
        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        self.assertEqual(len(mail.outbox), 0)  # blocked, never plaintext
        self.assertEqual(OutboxMessage.objects.get(invoice=second).last_error, "EncryptionRefused(key_failed)")

        # Only a different source key replaces it.
        new = keys.add_key(a, fixture("alice_pub.asc"), source="staff", actor=self.staff)
        self.assertEqual(self._copy_keys(second.contact), {key_a.pk: "revoked", new.pk: "active"})
        notify.retry_now(OutboxMessage.objects.filter(invoice=second))
        notify.flush_outbox()
        self.assertEqual(parsed(mail.outbox[-1]).get_content_type(), "multipart/encrypted")

    def test_orphaned_mirror_is_revoked(self):
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        orphan = EncryptionKey.objects.get(contact=first.contact, source="mirror")
        EncryptionKey.objects.filter(pk=key_a.pk).delete()  # the key it mirrored is gone
        orphan.refresh_from_db()
        self.assertEqual((orphan.source, orphan.mirrors_id), ("mirror", None))
        newer = EncryptionKey.objects.create(contact=a, kind="pgp", material=fixture("alice_pub.asc"),
                                             fingerprint="B" * 40, state="active", source="staff")

        create_invoice(self.org, a, currency="EUR", lines=[LINE])  # never mistaken for a staff key
        orphan.refresh_from_db()
        self.assertEqual(orphan.state, "revoked")
        active = EncryptionKey.objects.get(contact=first.contact, state="active")
        self.assertEqual((active.source, active.mirrors_id, active.fingerprint), ("mirror", newer.pk, "B" * 40))

    def test_a_source_key_failing_at_send_blocks_the_copy_at_once(self):
        from apps.keys import delivery
        a, key_a = self._us_contact_with_key()
        draft = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        delivery.mark_failed(key_a, "expired", outbox_pk=0)  # A's own letter found it expired
        mirror = EncryptionKey.objects.get(contact=draft.contact, source="mirror")
        self.assertEqual((mirror.state, mirror.failed_while, mirror.last_error_kind), ("failed", "active", "expired"))
        send_invoice(draft)
        with self.assertLogs("apps.notify.services", "ERROR"):
            notify.flush_outbox()
        self.assertEqual(len(mail.outbox), 0)

    def test_a_confirmed_client_key_reaches_the_copy(self):
        from apps.keys import services as keys
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        pending = keys.add_key(a, fixture("alice_pub.asc"), source="client")
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "active"})  # pending: nothing yet
        url = OutboxMessage.objects.get(kind="key_confirm").body_context["confirm_url"]
        keys.confirm_key(url.rstrip("/").rsplit("/", 1)[1])
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "revoked", pending.pk: "active"})

    def test_a_mirror_revoked_on_the_copy_stays_revoked_until_its_source_changes(self):
        from apps.keys import services as keys
        a, key_a = self._us_contact_with_key()
        first = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0]
        keys.revoke_key(EncryptionKey.objects.get(contact=first.contact, source="mirror"), self.staff)
        create_invoice(self.org, a, currency="EUR", lines=[LINE])
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "revoked"})
        new = keys.add_key(a, fixture("alice_pub.asc"), source="staff", actor=self.staff)
        self.assertEqual(self._copy_keys(first.contact), {key_a.pk: "revoked", new.pk: "active"})

    def test_a_key_added_to_the_copy_itself_wins_and_a_failed_mirror_stays_failed(self):
        from apps.keys import delivery, services as keys
        from apps.keys.tests_validation import make_cert, pem
        a, key_a = self._us_contact_with_key()
        copy = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0].contact
        delivery.mark_failed(EncryptionKey.objects.get(contact=copy, source="mirror"), "expired", outbox_pk=0)
        own = keys.add_key(copy, pem(make_cert()), source="staff", actor=self.staff)
        create_invoice(self.org, a, currency="EUR", lines=[LINE])
        self.assertEqual(delivery.key_for_contact(copy.pk).pk, own.pk)
        self.assertEqual(self._copy_keys(copy), {key_a.pk: "failed"})
        keys.revoke_key(own, self.staff)
        with self.assertRaises(delivery.EncryptionRefused):  # blocked again, never plaintext
            delivery.key_for_contact(copy.pk)

    def test_following_a_failed_key_reads_no_key_material(self):
        """mark_failed runs in the outbox flush, outside the per-message guard: following it must
        work without the vault, so a sealed vault can never abort the flush there."""
        from apps.keys import delivery
        from apps.vault.vault import vault
        b = self._sc_contact()
        create_invoice(self.sc, b, currency="EUR", lines=[LINE])
        a, key_a = self._us_contact_with_key()
        copy = create_invoice(self.org, a, currency="EUR", lines=[LINE])[0].contact
        # B's key arrived without the services: only a reconcile that makes mirrors would read it.
        EncryptionKey.objects.create(contact=b, kind="pgp", material=fixture("alice_pub.asc"),
                                     fingerprint="C" * 40, state="active", source="staff")
        key_a = EncryptionKey.objects.only("pk", "state", "contact_id").get(pk=key_a.pk)
        vault.seal()
        try:
            self.assertTrue(delivery.mark_failed(key_a, "expired", outbox_pk=0))
        finally:
            vault.unseal(TEST_VAULT_PASSWORD)
        self.assertEqual(self._copy_keys(copy), {key_a.pk: "failed"})
