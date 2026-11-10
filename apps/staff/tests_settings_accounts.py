"""Bank accounts and fiat links on the staff pages (Task 4)."""
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from apps.audit.models import AuditEntry
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.staff.testing import verified_staff_client
from apps.staff.tests_settings_orgs import png_bytes
from apps.vault.testing import UnsealedVaultMixin


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AccountsTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)

    def bank(self, **over):
        data = {"beneficiary_name": "Liberation s.r.o.", "beneficiary_address": "", "account_number": "SK12 3456",
                "bank_name": "Bank", "bank_address": "", "bank_swift": "ABCDSKBX", "intermediary_bank_name": "",
                "intermediary_swift": "", "intermediary_account": "", "currency": "EUR", "label": "Main"}
        data.update(over)
        return data

    def fiat(self, **over):
        data = {"kind": "revolut", "label": "Rev", "url": "https://revolut.me/x", "currencies": ["EUR", "USD"]}
        data.update(over)
        return data

    def test_bank_create_edit_audit(self):
        r = self.c.post(reverse("staff:bank_new", args=[self.org.pk]), self.bank())
        self.assertRedirects(r, reverse("staff:org_edit", args=[self.org.pk]), fetch_redirect_response=False)
        b = BankAccount.objects.get()
        self.assertEqual((b.org, b.account_number, b.bank_swift, b.currency), (self.org, "SK12 3456", "ABCDSKBX", "EUR"))
        self.assertTrue(AuditEntry.objects.filter(action="bankaccount.create").exists())
        self.c.post(reverse("staff:bank_edit", args=[b.pk]), self.bank(label="Second"))
        b.refresh_from_db()
        self.assertEqual(b.label, "Second")
        e = AuditEntry.objects.get(action="bankaccount.update")
        self.assertEqual(e.detail["fields"], ["label"])
        self.assertNotIn("SK12", str(e.detail))

    def test_bank_currency_must_be_known(self):
        r = self.c.post(reverse("staff:bank_new", args=[self.org.pk]), self.bank(currency="XXX"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(BankAccount.objects.count(), 0)

    def test_toggle_and_no_delete(self):
        b = BankAccount.objects.create(org=self.org, currency="EUR")
        url = reverse("staff:bank_toggle", args=[b.pk])
        self.assertEqual(self.c.get(url).status_code, 405)
        self.c.post(url)
        b.refresh_from_db()
        self.assertFalse(b.active)
        self.c.post(url)
        b.refresh_from_db()
        self.assertTrue(b.active)
        e = AuditEntry.objects.filter(action="bankaccount.update").order_by("pk")
        self.assertEqual(e.count(), 2)
        self.assertEqual(e[0].detail["fields"], ["active"])
        for name in ("staff:bank_delete", "staff:fiat_delete"):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[b.pk])

    def test_fiat_create_edit_toggle(self):
        r = self.c.post(reverse("staff:fiat_new", args=[self.org.pk]), self.fiat())
        self.assertEqual(r.status_code, 302)
        f = FiatLink.objects.get()
        self.assertEqual((f.kind, sorted(f.currencies), f.url), ("revolut", ["EUR", "USD"], "https://revolut.me/x"))
        self.assertTrue(AuditEntry.objects.filter(action="fiatlink.create").exists())
        self.c.post(reverse("staff:fiat_edit", args=[f.pk]), self.fiat(currencies=["EUR", "USD"], label="New"))
        e = AuditEntry.objects.get(action="fiatlink.update")
        self.assertEqual(e.detail["fields"], ["label"])
        self.c.post(reverse("staff:fiat_toggle", args=[f.pk]))
        f.refresh_from_db()
        self.assertFalse(f.active)
        self.assertEqual(AuditEntry.objects.filter(action="fiatlink.update").order_by("-pk").first().detail["fields"], ["active"])

    def test_fiat_url_must_be_https(self):
        for bad in ("http://revolut.me/x", "javascript:alert(1)", "revolut.me/x"):
            r = self.c.post(reverse("staff:fiat_new", args=[self.org.pk]), self.fiat(url=bad))
            self.assertEqual(r.status_code, 200, bad)
            self.assertIn("url", r.context["form"].errors)
        self.assertEqual(FiatLink.objects.count(), 0)

    def test_org_page_lists_both_with_state(self):
        b = BankAccount.objects.create(org=self.org, currency="EUR", label="Main", active=False)
        f = FiatLink.objects.create(org=self.org, kind="wise", label="W", currencies=["GBP"])
        page = self.c.get(reverse("staff:org_edit", args=[self.org.pk]))
        for u in (reverse("staff:bank_edit", args=[b.pk]), reverse("staff:fiat_edit", args=[f.pk]),
                  reverse("staff:bank_new", args=[self.org.pk]), reverse("staff:fiat_new", args=[self.org.pk]),
                  reverse("staff:bank_toggle", args=[b.pk])):
            self.assertContains(page, u)

    def test_anonymous_refused(self):
        from django.test import Client
        b = BankAccount.objects.create(org=self.org, currency="EUR")
        r = Client().post(reverse("staff:bank_toggle", args=[b.pk]))
        self.assertEqual(r.status_code, 302)
        b.refresh_from_db()
        self.assertTrue(b.active)

    def test_refused_logo_keeps_old_file_and_row(self):
        url = reverse("staff:org_edit", args=[self.org.pk])
        base = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
                "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
                "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
                "accounting_currency": "EUR"}
        self.c.post(url, {**base, "logo": SimpleUploadedFile("l.png", png_bytes(), "image/png")})
        self.org.refresh_from_db()
        old, storage = self.org.logo.name, self.org.logo.storage
        with self.captureOnCommitCallbacks(execute=True):
            r = self.c.post(url, {**base, "logo": SimpleUploadedFile("l.png", b"not a picture", "image/png")})
        self.assertEqual(r.status_code, 400)
        self.org.refresh_from_db()
        self.assertEqual(self.org.logo.name, old)
        self.assertTrue(storage.exists(old))

    def test_shared_logo_name_is_not_deleted(self):
        url = reverse("staff:org_edit", args=[self.org.pk])
        base = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
                "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
                "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
                "accounting_currency": "EUR"}
        self.c.post(url, {**base, "logo": SimpleUploadedFile("l.png", png_bytes(), "image/png")})
        self.org.refresh_from_db()
        old, storage = self.org.logo.name, self.org.logo.storage
        Organization.objects.create(slug="other", logo=old)
        with self.captureOnCommitCallbacks(execute=True):
            self.c.post(url, {**base, "logo": SimpleUploadedFile("l.png", png_bytes(), "image/png")})
        self.assertTrue(storage.exists(old))


class InstructionsTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)
        self.bank = BankAccount.objects.create(org=self.org, currency="EUR", label="Main")
        self.fiat = FiatLink.objects.create(org=self.org, kind="wise", label="W", currencies=["EUR"],
                                            url="https://wise.com/x")

    def post_bank(self, **over):
        data = {"currency": "EUR", "label": "Main"}
        data.update(over)
        return self.c.post(reverse("staff:bank_edit", args=[self.bank.pk]), data)

    def post_fiat(self, **over):
        data = {"kind": "wise", "label": "W", "url": "https://wise.com/x", "currencies": ["EUR"]}
        data.update(over)
        return self.c.post(reverse("staff:fiat_edit", args=[self.fiat.pk]), data)

    def test_round_trip_renders_in_language_with_english_fallback(self):
        from apps.invoices.snapshot import methods_in
        self.post_bank(instr_sk="Platba na účet", instr_en="Pay to the account")
        self.post_fiat(instr_sk="Cez Wise", instr_en="Through Wise")
        self.bank.refresh_from_db()
        self.fiat.refresh_from_db()
        for obj, sk, en in ((self.bank, "Platba na účet", "Pay to the account"), (self.fiat, "Cez Wise", "Through Wise")):
            snap = {"methods": [{"instructions": obj.instructions}]}
            self.assertEqual(methods_in(snap, "sk")[0]["instructions"], sk)
            self.assertEqual(methods_in(snap, "de")[0]["instructions"], en)
        page = self.c.get(reverse("staff:bank_edit", args=[self.bank.pk]))
        self.assertContains(page, "Platba na účet")

    def test_empty_languages_left_out_and_nothing_stores_none(self):
        self.post_bank(instr_sk="  text  ", instr_en="   ")
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.instructions, {"sk": "text"})
        self.post_bank(instr_sk="")
        self.bank.refresh_from_db()
        self.assertIsNone(self.bank.instructions)
        self.post_fiat(instr_en="x")
        self.post_fiat()
        self.fiat.refresh_from_db()
        self.assertIsNone(self.fiat.instructions)

    def test_unknown_stored_keys_are_kept(self):
        BankAccount.objects.filter(pk=self.bank.pk).update(instructions={"xx": "old", "sk": "a"})
        FiatLink.objects.filter(pk=self.fiat.pk).update(instructions={"xx": "old"})
        self.post_bank(instr_sk="b")
        self.post_fiat(instr_en="n")
        self.bank.refresh_from_db()
        self.fiat.refresh_from_db()
        self.assertEqual(self.bank.instructions, {"xx": "old", "sk": "b"})
        self.assertEqual(self.fiat.instructions, {"xx": "old", "en": "n"})

    def test_audit_names_instructions_only_when_changed_and_never_the_text(self):
        BankAccount.objects.filter(pk=self.bank.pk).update(instructions={"sk": "same"})
        self.post_bank(instr_sk="same")
        self.assertFalse(AuditEntry.objects.filter(action="bankaccount.update").exists())
        self.post_bank(instr_sk="secret words")
        e = AuditEntry.objects.get(action="bankaccount.update")
        self.assertEqual(e.detail["fields"], ["instructions"])
        self.assertNotIn("secret", str(e.detail))
        self.post_fiat(instr_en="more secret")
        e = AuditEntry.objects.get(action="fiatlink.update")
        self.assertEqual(e.detail["fields"], ["instructions"])
        self.assertNotIn("secret", str(e.detail))

    def test_instructions_are_capped(self):
        r = self.post_bank(instr_en="x" * 2001)
        self.assertEqual(r.status_code, 200)
        self.assertIn("instr_en", r.context["form"].errors)

    def test_toggle_without_csrf_token_is_refused(self):
        from django.test import Client
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.c.user)
        r = browser.post(reverse("staff:bank_toggle", args=[self.bank.pk]))
        self.assertEqual(r.status_code, 403)
        r = browser.post(reverse("staff:fiat_toggle", args=[self.fiat.pk]))
        self.assertEqual(r.status_code, 403)
        self.bank.refresh_from_db()
        self.fiat.refresh_from_db()
        self.assertTrue(self.bank.active and self.fiat.active)


class UnchangedEditTest(UnsealedVaultMixin, TestCase):
    def test_unchanged_posts_write_no_audit_entry(self):
        org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        bank = BankAccount.objects.create(org=org, currency="EUR", label="Main")
        fiat = FiatLink.objects.create(org=org, kind="wise", label="W", currencies=["EUR"], url="https://wise.com/x")
        c = verified_staff_client(superuser=True)
        org_data = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
                    "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
                    "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
                    "accounting_currency": "EUR"}
        before = AuditEntry.objects.count()
        for url, data in ((reverse("staff:org_edit", args=[org.pk]), org_data),
                          (reverse("staff:bank_edit", args=[bank.pk]), {"currency": "EUR", "label": "Main"}),
                          (reverse("staff:fiat_edit", args=[fiat.pk]),
                           {"kind": "wise", "label": "W", "url": "https://wise.com/x", "currencies": ["EUR"]})):
            self.assertEqual(c.post(url, data).status_code, 302, url)
        self.assertEqual(AuditEntry.objects.count(), before)
