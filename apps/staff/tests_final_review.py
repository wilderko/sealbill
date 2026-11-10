"""Fixes from the final whole-branch review (A1 ... F)."""
import tempfile
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin


def org_fields(**over):
    data = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
            "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
            "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
            "accounting_currency": "EUR"}
    data.update(over)
    return data


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class OrgCounterAndDefaultsTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.url = reverse("staff:org_edit", args=[self.org.pk])
        self.c = verified_staff_client()

    def test_a1_saving_never_moves_the_counter_backwards(self):
        stale = Organization.objects.get(pk=self.org.pk)  # read before a number was issued
        Organization.objects.filter(pk=self.org.pk).update(next_number=2)
        with mock.patch("apps.staff.views_settings.get_object_or_404", return_value=stale):
            r = self.c.post(self.url, org_fields(footer_note="Thank you"))
        self.assertEqual(r.status_code, 302)
        row = Organization.objects.get(pk=self.org.pk)
        self.assertEqual(row.footer_note, "Thank you")
        self.assertEqual(row.next_number, 2)

    def test_a2_footer_only_edit_of_an_org_with_empty_settings(self):
        Organization.objects.filter(pk=self.org.pk).update(settings={})
        page = self.c.get(self.url)
        self.assertContains(page, 'value="EUR" selected')
        r = self.c.post(self.url, org_fields(footer_note="Thanks"))
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["accounting_currency"], "EUR")
        self.assertEqual(self.org.settings["underpay_allowance_bps"], 50)
        e = AuditEntry.objects.get(action="organization.update")
        self.assertEqual(e.detail["fields"], ["footer_note"])

    def test_a2_lowercase_currency_stays(self):
        Organization.objects.filter(pk=self.org.pk).update(
            settings={"underpay_allowance_bps": 50, "underpay_allowance_usd": "1.00",
                      "overpay_ceiling_bps": 1000, "accounting_currency": "eur"})
        self.c.post(self.url, org_fields(footer_note="x"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["accounting_currency"], "EUR")
        self.assertEqual(AuditEntry.objects.get(action="organization.update").detail["fields"], ["footer_note"])

    def test_a2_off_list_currency_is_kept_as_an_option(self):
        Organization.objects.filter(pk=self.org.pk).update(
            settings={"underpay_allowance_bps": 50, "underpay_allowance_usd": "1.00",
                      "overpay_ceiling_bps": 1000, "accounting_currency": "zzz"})
        self.assertContains(self.c.get(self.url), "ZZZ (unsupported)")
        self.c.post(self.url, org_fields(footer_note="x", accounting_currency="ZZZ"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["accounting_currency"], "ZZZ")


class UnreadableRowTest(UnsealedVaultMixin, TestCase):
    """B1: one value written with a key the vault no longer holds is a page that says so, not a 500."""

    def setUp(self):
        super().setUp()
        from django.db import connection

        from apps.contacts.models import Contact, ContactEmail
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.contact = Contact.objects.create(org=self.org, legal_name="Ada", display_name="Ada", phone="+421")
        ContactEmail.objects.create(contact=self.contact, address="ada@example.com", primary=True)
        with connection.cursor() as cur:
            cur.execute("UPDATE contacts_contact SET phone = %s WHERE id = %s", ["gAAAAAbogus", self.contact.pk])
        self.c = verified_staff_client()

    def test_pages_answer_409_and_never_print_the_value(self):
        edit = reverse("staff:contact_edit", args=[self.contact.pk])
        for r in (self.c.get(edit), self.c.post(edit, {"legal_name": "Changed"}),
                  self.c.get(reverse("staff:contact_list")), self.c.get(reverse("staff:contact_detail", args=[self.contact.pk]))):
            self.assertEqual(r.status_code, 409)
            self.assertContains(r, "A stored value cannot be read", status_code=409)
            self.assertNotContains(r, "gAAAAAbogus", status_code=409)
            self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(AuditEntry.objects.filter(action="contact.update").count(), 0)
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT legal_name FROM contacts_contact WHERE id = %s", [self.contact.pk])
            self.assertNotEqual(cur.fetchone()[0], "Changed")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class LogoTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.url = reverse("staff:org_edit", args=[self.org.pk])
        self.c = verified_staff_client()

    def upload(self, raw, name="l.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, raw, "image/png")

    def png(self, size=(8, 8), exif_orientation=None):
        import io

        from PIL import Image
        img = Image.new("RGB", size, "red")
        for x in range(size[0] // 2):
            img.putpixel((x, 0), (0, 0, 255))  # the left half of the top row is blue
        out = io.BytesIO()
        if exif_orientation:
            exif = Image.Exif()
            exif[0x0112] = exif_orientation
            img.save(out, "JPEG", exif=exif)
        else:
            img.save(out, "PNG")
        return out.getvalue()

    def test_b3_page_carries_the_limit_for_the_browser(self):
        page = self.c.get(self.url)
        self.assertContains(page, 'data-max-bytes="1992294"')
        self.assertContains(page, "data-max-bytes-message=")

    def test_b3_ui_js_checks_the_size_before_sending(self):
        from pathlib import Path

        from django.conf import settings
        js = (Path(settings.BASE_DIR) / "static" / "ui" / "ui.js").read_text()
        for needle in ("data-max-bytes", "data-max-bytes-message", "setCustomValidity"):
            self.assertIn(needle, js)

    def test_b3_a_logo_just_over_the_apache_limit_is_refused(self):
        from apps.staff.forms_settings import MAX_LOGO_BYTES
        self.assertEqual(MAX_LOGO_BYTES, 1992294)
        big = self.png() + b"\0" * int(1.95 * 1024 * 1024)
        r = self.c.post(self.url, {**org_fields(), "logo": self.upload(big)})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "at most 1.9 MB", status_code=400)

    def test_b3_exif_orientation_is_applied_before_metadata_is_dropped(self):
        import io

        from PIL import Image
        raw = self.png(size=(8, 4), exif_orientation=6)  # rotate 90 degrees clockwise on display
        r = self.c.post(self.url, {**org_fields(), "logo": self.upload(raw, "l.jpg")})
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        with self.org.logo.open("rb") as f:
            stored = Image.open(io.BytesIO(f.read()))
        self.assertEqual(stored.size, (4, 8))

    def test_b3_remove_logo(self):
        self.c.post(self.url, {**org_fields(), "logo": self.upload(self.png())})
        self.org.refresh_from_db()
        name, storage = self.org.logo.name, self.org.logo.storage
        self.assertTrue(storage.exists(name))
        self.assertContains(self.c.get(self.url), 'name="remove_logo"')
        with self.captureOnCommitCallbacks(execute=True):
            r = self.c.post(self.url, {**org_fields(), "remove_logo": "on"})
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.logo)
        self.assertFalse(storage.exists(name))
        self.assertEqual(AuditEntry.objects.filter(action="organization.update").order_by("-pk").first().detail["fields"], ["logo"])

    def test_b3_remove_logo_keeps_a_file_another_row_names(self):
        self.c.post(self.url, {**org_fields(), "logo": self.upload(self.png())})
        self.org.refresh_from_db()
        name, storage = self.org.logo.name, self.org.logo.storage
        Organization.objects.create(slug="us", legal_name="LLC", logo=name)
        with self.captureOnCommitCallbacks(execute=True):
            self.c.post(self.url, {**org_fields(), "remove_logo": "on"})
        self.assertTrue(storage.exists(name))

    def test_b3_no_logo_no_checkbox(self):
        self.assertNotContains(self.c.get(self.url), 'name="remove_logo"')


class ContactEmailsTest(UnsealedVaultMixin, TestCase):
    """C1: a contact's addresses are managed on the staff page."""

    def setUp(self):
        super().setUp()
        from apps.contacts.models import Contact, ContactEmail
        self.Contact, self.ContactEmail = Contact, ContactEmail
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.contact = Contact.objects.create(org=self.org, legal_name="Ada")
        self.main = ContactEmail.objects.create(contact=self.contact, address="ada@example.com", primary=True)
        self.c = verified_staff_client(superuser=True)

    def url(self, name, *extra):
        return reverse(f"staff:{name}", args=[self.contact.pk, *extra])

    def audits(self):
        return list(AuditEntry.objects.filter(action="contact.update"))

    def test_detail_offers_the_actions(self):
        page = self.c.get(reverse("staff:contact_detail", args=[self.contact.pk]))
        self.assertContains(page, self.url("contact_email_add"))

    def test_add_stores_normalised_and_audits_without_the_address(self):
        r = self.c.post(self.url("contact_email_add"), {"address": "  Second@Example.com "})
        self.assertEqual(r.status_code, 302)
        row = self.ContactEmail.objects.get(contact=self.contact, primary=False)
        self.assertEqual(row.address, "Second@Example.com")
        from apps.vault.fields import blind_index
        self.assertEqual(row.address_index, blind_index("second@example.com"))
        (entry,) = self.audits()
        self.assertEqual(entry.detail, {"fields": ["emails"], "via": "staff"})
        self.assertNotIn("example.com", str(entry.detail))

    def test_add_refuses_an_invalid_address_and_a_duplicate(self):
        self.c.post(self.url("contact_email_add"), {"address": "not an address"})
        self.c.post(self.url("contact_email_add"), {"address": "ADA@example.com"})
        other = self.Contact.objects.create(org=self.org, legal_name="Bob")
        self.ContactEmail.objects.create(contact=other, address="bob@example.com", primary=True)
        r = self.c.post(self.url("contact_email_add"), {"address": "bob@example.com"}, follow=True)
        self.assertContains(r, "Another contact of this organization already has that address.")
        self.assertEqual(self.ContactEmail.objects.filter(contact=self.contact).count(), 1)
        self.assertEqual(self.audits(), [])

    def test_the_same_address_in_another_organization_is_fine(self):
        other_org = Organization.objects.create(slug="us", legal_name="LLC")
        other = self.Contact.objects.create(org=other_org, legal_name="Bob")
        self.ContactEmail.objects.create(contact=other, address="bob@example.com", primary=True)
        self.c.post(self.url("contact_email_add"), {"address": "bob@example.com"})
        self.assertEqual(self.ContactEmail.objects.filter(contact=self.contact).count(), 2)

    def test_make_primary_moves_the_flag(self):
        second = self.ContactEmail.objects.create(contact=self.contact, address="two@example.com")
        r = self.c.post(self.url("contact_email_primary", second.pk))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(list(self.ContactEmail.objects.filter(contact=self.contact, primary=True)), [second])
        self.assertEqual(len(self.audits()), 1)
        self.c.post(self.url("contact_email_primary", second.pk))  # already primary: nothing to record
        self.assertEqual(len(self.audits()), 1)

    def test_remove_refuses_primary_and_last_address(self):
        r = self.c.post(self.url("contact_email_remove", self.main.pk), follow=True)
        self.assertContains(r, "primary address cannot be removed")
        self.assertTrue(self.ContactEmail.objects.filter(pk=self.main.pk).exists())
        self.assertEqual(self.audits(), [])

    def test_remove_a_secondary_address(self):
        second = self.ContactEmail.objects.create(contact=self.contact, address="two@example.com")
        self.c.post(self.url("contact_email_remove", second.pk))
        self.assertFalse(self.ContactEmail.objects.filter(pk=second.pk).exists())
        (entry,) = self.audits()
        self.assertEqual(entry.detail, {"fields": ["emails"], "via": "staff"})

    def test_remove_carries_a_confirmation_and_needs_post(self):
        second = self.ContactEmail.objects.create(contact=self.contact, address="two@example.com")
        page = self.c.get(reverse("staff:contact_detail", args=[self.contact.pk]))
        self.assertContains(page, 'data-confirm="Remove this e-mail address')
        self.assertEqual(self.c.get(self.url("contact_email_remove", second.pk)).status_code, 405)

    def test_another_contacts_address_cannot_be_reached_through_this_one(self):
        other = self.Contact.objects.create(org=self.org, legal_name="Bob")
        theirs = self.ContactEmail.objects.create(contact=other, address="bob@example.com", primary=False)
        self.assertEqual(self.c.post(self.url("contact_email_remove", theirs.pk)).status_code, 404)
        self.assertEqual(self.c.post(self.url("contact_email_primary", theirs.pk)).status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        from django.test import Client
        r = Client().post(self.url("contact_email_add"), {"address": "x@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ContactEmail.objects.filter(contact=self.contact).count(), 1)


class ReturnToTheListTest(UnsealedVaultMixin, TestCase):
    """D1: retry and dismiss on the full lists come back to the list, filter and page included."""

    def setUp(self):
        super().setUp()
        from apps.notify.models import OutboxMessage
        from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.msg = OutboxMessage.objects.create(kind="invoice", state="failed", last_error="Boom", to=["a@example.com"])
        endpoint = WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s", events=["*"])
        event = WebhookEvent.objects.create(type="invoice.paid", org=self.org, data={})
        self.delivery = WebhookDelivery.objects.create(event=event, endpoint=endpoint, state="failed")
        self.c = verified_staff_client()

    def test_row_forms_carry_the_current_path(self):
        page = self.c.get(reverse("staff:outbox_list"), {"state": "failed"})
        self.assertContains(page, '<input type="hidden" name="next" value="/outbox/?state=failed">', html=False)
        page = self.c.get(reverse("staff:delivery_list"), {"state": "failed"})
        self.assertContains(page, '<input type="hidden" name="next" value="/deliveries/?state=failed">', html=False)

    def fresh(self, name):
        from apps.notify.models import OutboxMessage
        from apps.webhooks.models import WebhookDelivery
        if name.endswith("outbox"):
            return OutboxMessage.objects.create(kind="invoice", state="failed", last_error="Boom", to=["a@example.com"])
        return WebhookDelivery.objects.create(event=self.delivery.event, endpoint=self.delivery.endpoint, state="failed")

    def test_all_four_return_to_next_and_ignore_a_foreign_one(self):
        cases = (("staff:retry_outbox", {}, "/outbox/?state=failed"),
                 ("staff:dismiss_outbox", {"reason": "bounced"}, "/outbox/?state=failed"),
                 ("staff:retry_webhook", {}, "/deliveries/?state=failed&page=2"),
                 ("staff:dismiss_webhook", {"reason": "gone"}, "/deliveries/?state=failed"))
        for name, extra, back in cases:
            with self.subTest(name):
                r = self.c.post(reverse(name, args=[self.fresh(name).pk]), {**extra, "next": back})
                self.assertRedirects(r, back, fetch_redirect_response=False)
                for evil in ("https://evil.example/x", "//evil.example/x"):
                    r = self.c.post(reverse(name, args=[self.fresh(name).pk]), {**extra, "next": evil})
                    self.assertEqual(r.status_code, 302)
                    self.assertEqual(r["Location"], reverse("staff:needs_person"))

    def test_without_next_it_is_still_needs_a_person(self):
        r = self.c.post(reverse("staff:retry_outbox", args=[self.msg.pk]))
        self.assertRedirects(r, reverse("staff:needs_person"), fetch_redirect_response=False)

    def test_lists_show_the_message_afterwards(self):
        r = self.c.post(reverse("staff:dismiss_outbox", args=[self.msg.pk]), {"reason": "bounced", "next": "/outbox/"}, follow=True)
        self.assertContains(r, "Dismissed the message.")


class ConfirmAndMessagesTest(UnsealedVaultMixin, TestCase):
    def test_d2_each_irreversible_action_asks_first(self):
        from pathlib import Path

        from django.conf import settings
        root = Path(settings.BASE_DIR) / "templates" / "staff"
        expectations = {
            "_key_rows.html": ["Revoke this encryption key?"],
            "invoice_detail.html": ["Delete this draft?", "Void this invoice?"],
            "security.html": ["Remove the passkey", "Remove the authenticator"],
        }
        for name, texts in expectations.items():
            body = (root / name).read_text()
            for text in texts:
                self.assertIn(f'data-confirm="{text}', body, (name, text))

    def test_e3_an_error_message_does_not_render_as_success(self):
        from django.contrib.messages import constants
        from django.contrib.messages.storage.base import Message
        from django.template.loader import render_to_string
        html = render_to_string("ui/_messages.html", {"messages": [
            Message(constants.ERROR, "It broke"), Message(constants.WARNING, "Careful"), Message(constants.SUCCESS, "Done")]})
        err, warn, ok = ([l for l in html.splitlines() if t in l][0] for t in ("It broke", "Careful", "Done"))
        self.assertIn('class="error"', err)
        self.assertNotIn("notice--ok", err)
        self.assertIn("notice--warn", warn)
        self.assertIn("notice--ok", ok)

    def test_e3_every_staff_template_uses_the_shared_partial(self):
        from pathlib import Path

        from django.conf import settings
        for path in (Path(settings.BASE_DIR) / "templates").rglob("*.html"):
            if path.name == "_messages.html":
                continue
            self.assertNotIn("in messages", path.read_text(), str(path))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CrlfTest(UnsealedVaultMixin, TestCase):
    """E1: a browser posts CRLF; that is not an edit."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.", address="Line1\nLine2",
                                               tax_ids="A\nB", footer_note="x\ny")
        self.c = verified_staff_client()

    def test_org_unchanged_multiline_post_writes_no_audit(self):
        r = self.c.post(reverse("staff:org_edit", args=[self.org.pk]), org_fields(
            address="Line1\r\nLine2", tax_ids="A\r\nB", footer_note="x\r\ny"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AuditEntry.objects.filter(action="organization.update").count(), 0)

    def test_a_real_edit_is_stored_with_lf_and_names_only_that_field(self):
        self.c.post(reverse("staff:org_edit", args=[self.org.pk]), org_fields(
            address="Line1\r\nLine3", tax_ids="A\r\nB", footer_note="x\r\ny"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.address, "Line1\nLine3")
        self.assertEqual(AuditEntry.objects.get(action="organization.update").detail["fields"], ["address"])

    def test_bank_instructions_and_contact_notes(self):
        from apps.contacts.models import Contact, ContactEmail
        from apps.orgs.models import BankAccount
        bank = BankAccount.objects.create(org=self.org, currency="EUR", beneficiary_address="a\nb", bank_address="c\nd",
                                          instructions={"en": "one\ntwo"})
        from apps.staff.forms_settings import BankAccountForm
        data = {"label": bank.label or "", "currency": "EUR", "beneficiary_name": bank.beneficiary_name or "",
                "beneficiary_address": "a\r\nb", "account_number": bank.account_number or "", "bank_name": bank.bank_name or "",
                "bank_address": "c\r\nd", "bank_swift": bank.bank_swift or "", "intermediary_bank_name": "",
                "intermediary_swift": "", "intermediary_account": "", "instr_en": "one\r\ntwo"}
        form = BankAccountForm(data, instance=bank)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.changed_data, [])
        contact = Contact.objects.create(org=self.org, legal_name="Ada", notes="n1\nn2", tax_ids="t1\nt2")
        ContactEmail.objects.create(contact=contact, address="ada@example.com", primary=True)
        r = self.c.post(reverse("staff:contact_edit", args=[contact.pk]), {
            "legal_name": "Ada", "display_name": "", "tax_ids": "t1\r\nt2", "phone": "", "notes": "n1\r\nn2",
            "preferred_language": "", "us_tax_resident": "unknown", **{f"address_{p}": "" for p in ("line1", "line2", "city", "zip", "state", "country")}})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AuditEntry.objects.filter(action="contact.update").count(), 0)


class NavigationTest(TestCase):
    def test_e2_every_settings_url_marks_settings_current(self):
        from apps.staff import urls
        from apps.staff.context import CURRENT
        names = [p.name for p in urls.urlpatterns if str(p.pattern).startswith("settings/")]
        self.assertGreater(len(names), 25)
        for name in names:
            self.assertEqual(CURRENT.get(name), "settings", name)


class AuditShapeTest(UnsealedVaultMixin, TestCase):
    def test_e4_credential_audit_says_staff_only_for_a_signed_in_actor(self):
        from apps.rails.credentials import _store_credentials
        from apps.rails.models import RailAccount
        org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        acct = RailAccount.objects.create(org=org, kind="btcpay", label="b", active=False, assets=["BTC"],
                                          config={"url": "https://btcpay.example", "store_id": "S"})
        user = verified_staff_client().user
        _store_credentials(acct, {"api_key": "k"}, ["api_key"], user)
        _store_credentials(acct, {"api_key": "k"}, ["api_key"], None, "command")
        staff_entry, command_entry = AuditEntry.objects.filter(action="rail_account.credentials").order_by("pk")
        self.assertEqual(staff_entry.detail["via"], "staff")
        self.assertNotIn("via", command_entry.detail)
        self.assertEqual(command_entry.actor_username, "command")


class TokenScopeOrderTest(UnsealedVaultMixin, TestCase):
    def test_e5_the_same_scopes_in_another_order_are_no_change(self):
        from apps.api.models import ApiToken
        org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        c = verified_staff_client(superuser=True)
        token, _plain = ApiToken.issue(org, "t", ["read", "payments:confirm"], actor=c.user, channel="staff")
        before = AuditEntry.objects.count()
        r = c.post(reverse("staff:token_scopes", args=[token.pk]), {"scopes": ["payments:confirm", "read"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AuditEntry.objects.count(), before)
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["read", "payments:confirm"])


# A rail URL is resolved as it is saved (audit SEC-4): every host here answers one public address.
PUBLIC_ADDRINFO = [(2, 1, 6, "", ("93.184.216.34", 443))]


class RailUrlBase(UnsealedVaultMixin, TestCase):
    """E6: a credential is only sent over https, and only to the host it was issued for."""

    def setUp(self):
        super().setUp()
        from apps.rails.models import RailAccount
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.btcpay = RailAccount.objects.create(
            org=self.org, kind="btcpay", label="BTCPay", active=True, assets=["BTC"],
            config={"url": "https://btcpay.example/base", "store_id": "S1", "speed_policy": "MediumSpeed"},
            credentials={"api_key": "KEY"})
        self.c = verified_staff_client(superuser=True)
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=PUBLIC_ADDRINFO))

    def post_btcpay(self, url, **over):
        data = {"label": "BTCPay", "active": "on", "url": url, "store_id": "S1", "speed_policy": "MediumSpeed", "assets": ["BTC"]}
        data.update(over)
        return self.c.post(reverse("staff:rail_edit", args=[self.btcpay.pk]), data)



class RailUrlTest(RailUrlBase):
    def test_http_is_refused_for_btcpay_and_lnbits(self):
        r = self.post_btcpay("http://btcpay.example")
        self.assertEqual(r.status_code, 200)
        self.assertIn("url", r.context["form"].errors)
        from apps.rails.models import RailAccount
        ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", active=False, assets=["BTC"],
                                        config={"backend": "lnbits", "url": "https://lnbits.example"})
        r = self.c.post(reverse("staff:rail_edit", args=[ln.pk]), {"label": "LN", "backend": "lnbits", "url": "http://lnbits.example"})
        self.assertIn("url", r.context["form"].errors)

    @override_settings(LOCAL_LIGHTNING_PORT=8080)
    def test_lnd_and_cln_allow_http_on_loopback_only(self):
        from apps.rails.models import RailAccount
        ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", active=False, assets=["BTC"],
                                        config={"backend": "lnd", "url": "https://lnd.example"})
        url = reverse("staff:rail_edit", args=[ln.pk])
        for bad in ("http://lnd.example:8080", "http://127.0.0.1:9735", "https://127.0.0.1:3001"):
            r = self.c.post(url, {"label": "LN", "backend": "lnd", "url": bad})
            self.assertIn("url", r.context["form"].errors, bad)
        for ok in ("http://127.0.0.1:8080", "https://lnd.example"):
            r = self.c.post(url, {"label": "LN", "backend": "lnd", "url": ok})
            self.assertEqual(r.status_code, 302, ok)
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 8080))]):
            r = self.c.post(url, {"label": "LN", "backend": "lnd", "url": "http://localhost:8080/"})
        self.assertEqual(r.status_code, 302)

    @override_settings(LOCAL_LIGHTNING_PORT=None)
    def test_without_local_lightning_port_lnd_reaches_no_loopback(self):
        from apps.rails.models import RailAccount
        ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", active=False, assets=["BTC"],
                                        config={"backend": "lnd", "url": "https://lnd.example"})
        r = self.c.post(reverse("staff:rail_edit", args=[ln.pk]),
                        {"label": "LN", "backend": "lnd", "url": "http://127.0.0.1:8080"})
        self.assertIn("LOCAL_LIGHTNING_PORT", str(r.context["form"].errors["url"]))

    def test_a_host_change_clears_the_credentials_and_deactivates(self):
        r = self.post_btcpay("https://other.example/base")
        self.assertRedirects(r, reverse("staff:rail_credentials", args=[self.btcpay.pk]), fetch_redirect_response=False)
        self.btcpay.refresh_from_db()
        self.assertFalse(self.btcpay.credentials)
        self.assertFalse(self.btcpay.active)
        self.assertEqual(self.btcpay.config["url"], "https://other.example/base")
        entry = AuditEntry.objects.get(action="railaccount.update")
        self.assertEqual(entry.detail["fields"], ["active", "credentials", "url"])
        self.assertNotIn("KEY", str(entry.detail))
        page = self.c.get(reverse("staff:rail_credentials", args=[self.btcpay.pk]))
        self.assertContains(page, "Set the credentials again")

    def test_a_path_change_on_the_same_host_keeps_them(self):
        r = self.post_btcpay("https://BTCPAY.example/other-path")
        self.assertRedirects(r, reverse("staff:rail_list"), fetch_redirect_response=False)
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials, {"api_key": "KEY"})
        self.assertTrue(self.btcpay.active)
        self.assertEqual(AuditEntry.objects.get(action="railaccount.update").detail["fields"], ["url"])


class ParkedTestsAndSmallUiTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)

    def test_f_clearing_one_address_part_removes_that_key_and_keeps_unknown_keys(self):
        from apps.contacts.models import Contact, ContactEmail
        contact = Contact.objects.create(org=self.org, legal_name="Ada", address={
            "line1": "Main 1", "city": "Brno", "zip": "60200", "region_code": "XY"})
        ContactEmail.objects.create(contact=contact, address="ada@example.com", primary=True)
        r = self.c.post(reverse("staff:contact_edit", args=[contact.pk]), {
            "legal_name": "Ada", "display_name": "", "tax_ids": "", "phone": "", "notes": "", "preferred_language": "",
            "us_tax_resident": "unknown", "address_line1": "Main 1", "address_line2": "", "address_city": "",
            "address_zip": "60200", "address_state": "", "address_country": ""})
        self.assertEqual(r.status_code, 302)
        contact.refresh_from_db()
        self.assertEqual(contact.address, {"line1": "Main 1", "zip": "60200", "region_code": "XY"})
        self.assertEqual(AuditEntry.objects.get(action="contact.update").detail["fields"], ["address"])

    def test_f_invoice_detail_links_the_payment_link_and_the_contact(self):
        import datetime

        from apps.contacts.models import Contact
        from apps.invoices.models import Invoice
        from apps.links.models import PaymentLink
        contact = Contact.objects.create(org=self.org, legal_name="Ada")
        link = PaymentLink.objects.create(org=self.org, token_hash="a" * 64, currency="EUR", price_minor=100)
        inv = Invoice.objects.create(org=self.org, contact=contact, requested_by_org=self.org, currency="EUR",
                                     number="2026-0001", issue_date=datetime.date(2026, 11, 1), payment_link=link)
        page = self.c.get(reverse("staff:invoice", args=[inv.pk]))
        self.assertContains(page, f'href="{reverse("staff:link_detail", args=[link.pk])}"')
        self.assertContains(page, f'href="{reverse("staff:contact_detail", args=[contact.pk])}"')

    def test_e9_chain_accounts_show_a_dash_and_btcpay_shows_the_state(self):
        from apps.rails.models import RailAccount
        RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20, networks=["arbitrum"], assets=["USDC"])
        RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPAY", assets=["BTC"], active=False,
                                   config={"url": "https://b.example", "store_id": "S"})
        page = self.c.get(reverse("staff:rail_list"))
        self.assertEqual(page.content.decode().count("credentials set: no"), 1)  # the BTCPay one only
        self.assertContains(page, "&mdash;")

    def test_e9_team_links_back_to_settings(self):
        self.assertContains(self.c.get(reverse("staff:team")), f'href="{reverse("staff:settings")}"')

    def test_e9_a_no_op_save_says_no_changes_on_org_bank_fiat_and_rail(self):
        from apps.orgs.models import BankAccount, FiatLink
        from apps.rails.models import RailAccount
        r = self.c.post(reverse("staff:org_edit", args=[self.org.pk]), org_fields(), follow=True)
        self.assertContains(r, "No changes.")
        bank = BankAccount.objects.create(org=self.org, currency="EUR", label="B")
        r = self.c.post(reverse("staff:bank_edit", args=[bank.pk]), {"label": "B", "currency": "EUR"}, follow=True)
        self.assertContains(r, "No changes.")
        fiat = FiatLink.objects.create(org=self.org, kind="paypal", label="P", url="https://p.example/x", currencies=["EUR"])
        r = self.c.post(reverse("staff:fiat_edit", args=[fiat.pk]), {"kind": "paypal", "label": "P", "url": "https://p.example/x", "currencies": ["EUR"]}, follow=True)
        self.assertContains(r, "No changes.")
        acct = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20, networks=["arbitrum"], assets=["USDC"], active=True)
        r = self.c.post(reverse("staff:rail_edit", args=[acct.pk]), {"label": "EVM", "active": "on", "address": acct.address,
                                                                     "networks": ["arbitrum"], "assets": ["USDC"]}, follow=True)
        self.assertContains(r, "No changes.")

    def test_e8_stale_wording_is_gone(self):
        from pathlib import Path

        from django.conf import settings
        base = Path(settings.BASE_DIR)
        for name in ("declared_staff.txt", "declared_staff.html"):
            body = (base / "templates" / "email" / name).read_text()
            self.assertIn("on the staff page", body)
            self.assertNotIn("in the admin", body)
        readme = (base / "docs/operations.md").read_text()
        self.assertIn("Settings > Rail accounts", readme)
        self.assertIn("hidden Django admin is the fallback", readme)
        self.assertNotIn("a superuser can delete a lost passkey in the admin", readme)


class RailUrlCraftedTest(RailUrlBase):
    """Round 2: urlsplit and requests disagree about backslashes; refuse what they could disagree on."""

    def test_crafted_urls_are_refused_and_keep_the_credentials(self):
        for crafted in ("https://evil.example\\@btcpay.example/base", "https://evil.example\\@btcpay.example",
                        "https://user:pw@btcpay.example/base", "https://btcpay.example/ba se",
                        "https://btcpay.example/base\x07", "https://btcpay.example:notaport/"):
            with self.subTest(crafted):
                r = self.post_btcpay(crafted)
                self.assertEqual(r.status_code, 200)
                self.assertIn("url", r.context["form"].errors)
                self.btcpay.refresh_from_db()
                self.assertEqual(self.btcpay.credentials, {"api_key": "KEY"})
                self.assertTrue(self.btcpay.active)

    def test_crafted_loopback_url_is_refused_for_lnd(self):
        from apps.rails.models import RailAccount
        ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", active=False, assets=["BTC"],
                                        config={"backend": "lnd", "url": "https://lnd.example"})
        r = self.c.post(reverse("staff:rail_edit", args=[ln.pk]),
                        {"label": "LN", "backend": "lnd", "url": "http://evil.example\\@127.0.0.1:8080"})
        self.assertIn("url", r.context["form"].errors)

    def test_loopback_http_is_refused_for_btcpay_and_lnbits(self):
        r = self.post_btcpay("http://127.0.0.1:23000")
        self.assertIn("url", r.context["form"].errors)
        from apps.rails.models import RailAccount
        ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", active=False, assets=["BTC"],
                                        config={"backend": "lnbits", "url": "https://lnbits.example"})
        r = self.c.post(reverse("staff:rail_edit", args=[ln.pk]), {"label": "LN", "backend": "lnbits", "url": "http://127.0.0.1"})
        self.assertIn("url", r.context["form"].errors)

    def test_webhook_audit_says_staff(self):
        from apps.staff.tests_settings_rail_credentials import INVOICES_ONLY
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH1"}), \
                override_settings(SITE_URL="https://pay.example"):
            self.c.post(reverse("staff:rail_btcpay_webhook", args=[self.btcpay.pk]))
        self.assertEqual(AuditEntry.objects.get(action="rail_account.btcpay_webhook").detail["via"], "staff")


class RailQuotedConfigTest(RailUrlBase):
    """Round 2: while a quote may still be paid, the gateway an account talks to cannot change."""

    def quote(self):
        import datetime

        from django.utils import timezone

        from apps.contacts.services import find_or_create_contact
        from apps.invoices.services import create_invoice, send_invoice
        from apps.quotes.models import Quote
        contact, _ = find_or_create_contact(self.org, "q@example.com", legal_name="Q")
        draft, _ = create_invoice(self.org, contact, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])
        invoice, _v, _p = send_invoice(draft, email_client=False)
        now = timezone.now()
        return Quote.objects.create(
            invoice=invoice, rail_account=self.btcpay, network="bitcoin", asset="BTC", address="x",
            amount_base_units="1", min_accept_base_units="1", ceiling_base_units="2", invoice_amount_minor=100,
            issued_at=now, expires_at=now + datetime.timedelta(minutes=30))

    def assert_untouched(self):
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials, {"api_key": "KEY"})
        self.assertTrue(self.btcpay.active)
        self.assertEqual(self.btcpay.config["url"], "https://btcpay.example/base")
        self.assertEqual(self.btcpay.config["store_id"], "S1")

    def test_host_and_store_changes_are_refused_beside_the_field(self):
        self.quote()
        r = self.post_btcpay("https://other.example/base")
        self.assertEqual(r.status_code, 200)
        self.assertIn("url", r.context["form"].errors)
        self.assert_untouched()
        r = self.post_btcpay("https://btcpay.example/base", store_id="S2")
        self.assertIn("store_id", r.context["form"].errors)
        self.assert_untouched()

    def test_backend_change_is_refused(self):
        from apps.rails.models import RailAccount
        from apps.rails.models import RailAccount as R
        ln = R.objects.create(org=self.org, kind="lightning", label="LN", active=True, assets=["BTC"],
                              config={"backend": "lnd", "url": "https://lnd.example"})
        self.btcpay = ln  # quote() attaches to self.btcpay
        self.quote()
        r = self.c.post(reverse("staff:rail_edit", args=[ln.pk]), {"label": "LN", "active": "on", "backend": "cln", "url": "https://lnd.example"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("backend", r.context["form"].errors)
        ln.refresh_from_db()
        self.assertEqual(ln.config["backend"], "lnd")

    def test_trailing_slash_and_speed_policy_stay_editable(self):
        self.quote()
        r = self.post_btcpay("https://btcpay.example/base/", speed_policy="LowSpeed")
        self.assertEqual(r.status_code, 302)
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.config["speed_policy"], "LowSpeed")
        self.assertEqual(self.btcpay.credentials, {"api_key": "KEY"})

    def test_the_hidden_admin_is_bound_too(self):
        from django.core.exceptions import ValidationError
        self.quote()
        self.btcpay.config = {**self.btcpay.config, "store_id": "OTHER"}
        with self.assertRaises(ValidationError) as ctx:
            self.btcpay.full_clean()
        self.assertIn("config", ctx.exception.message_dict)

    def test_a_released_quote_no_longer_blocks_the_clearing(self):
        quote = self.quote()
        type(quote).objects.filter(pk=quote.pk).update(state="released")
        r = self.post_btcpay("https://other.example/base")
        self.assertEqual(r.status_code, 302)
        self.btcpay.refresh_from_db()
        self.assertFalse(self.btcpay.credentials)
        self.assertFalse(self.btcpay.active)


class RoundTwoSmallTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        from apps.contacts.models import Contact, ContactEmail
        self.Contact, self.ContactEmail = Contact, ContactEmail
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client()

    def test_removing_an_address_an_active_key_names_is_refused(self):
        from apps.keys.models import EncryptionKey
        from apps.keys.validation import KeyRejected
        contact = self.Contact.objects.create(org=self.org, legal_name="Ada")
        self.ContactEmail.objects.create(contact=contact, address="ada@example.com", primary=True)
        second = self.ContactEmail.objects.create(contact=contact, address="two@example.com")
        EncryptionKey.objects.create(contact=contact, kind="pgp", material="m", fingerprint="f", state="active", source="staff")
        url = reverse("staff:contact_email_remove", args=[contact.pk, second.pk])
        with mock.patch("apps.staff.views_contacts.validation.inspect", side_effect=KeyRejected("email_mismatch")):
            r = self.c.post(url, follow=True)
        self.assertContains(r, "An active encryption key names this address: revoke the key first.")
        self.assertTrue(self.ContactEmail.objects.filter(pk=second.pk).exists())
        with mock.patch("apps.staff.views_contacts.validation.inspect", return_value=object()):
            self.c.post(url)  # the key still names another address: removal is fine
        self.assertFalse(self.ContactEmail.objects.filter(pk=second.pk).exists())

    def test_issuer_copies_are_named_on_both_sides(self):
        other = Organization.objects.create(slug="us", legal_name="LLC")
        native = self.Contact.objects.create(org=other, legal_name="Ada")
        self.ContactEmail.objects.create(contact=native, address="ada@example.com", primary=True)
        copy = self.Contact.objects.create(org=self.org, legal_name="Ada", copy_for_org=other)
        self.ContactEmail.objects.create(contact=copy, address="ada@example.com", primary=True)
        alone = self.Contact.objects.create(org=self.org, legal_name="Bob")
        self.ContactEmail.objects.create(contact=alone, address="bob@example.com", primary=True)
        for me, them in ((native, copy), (copy, native)):
            page = self.c.get(reverse("staff:contact_detail", args=[me.pk]))
            self.assertContains(page, "do not reach")
            self.assertContains(page, f'href="{reverse("staff:contact_detail", args=[them.pk])}"')
        self.assertNotContains(self.c.get(reverse("staff:contact_detail", args=[alone.pk])), "do not reach")

    def test_null_or_empty_currency_falls_back_to_the_default(self):
        org = Organization.objects.create(slug="x", legal_name="X")
        for bad in (None, ""):
            Organization.objects.filter(pk=org.pk).update(settings={"underpay_allowance_bps": 50, "underpay_allowance_usd": "1.00",
                                                                    "overpay_ceiling_bps": 1000, "accounting_currency": bad})
            page = self.c.get(reverse("staff:org_edit", args=[org.pk]))
            self.assertNotContains(page, "NONE")
            self.assertContains(page, 'value="EUR" selected')

    def test_the_speed_policy_explanation_names_no_admin(self):
        from apps.rails.health import SPEED_POLICY_EXPLANATION
        self.assertNotIn("in the admin", SPEED_POLICY_EXPLANATION)
        self.assertIn("Settings", SPEED_POLICY_EXPLANATION)
