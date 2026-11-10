"""Settings and organizations on the staff pages (Task 3)."""
import io
import json
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault


def png_bytes(text_chunk=False, size=(8, 8), fmt="PNG"):
    out = io.BytesIO()
    info = PngInfo()
    info.add_text("secret", "gps-here")
    Image.new("RGB", size, "red").save(out, fmt, **({"pnginfo": info} if text_chunk and fmt == "PNG" else {}))
    return out.getvalue()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class OrgSettingsTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.url = reverse("staff:org_edit", args=[self.org.pk])
        self.c = verified_staff_client()

    def fields(self, **over):
        data = {"slug": "eu", "legal_name": "Liberation s.r.o.", "address": "", "tax_ids": "", "email_from": "",
                "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
                "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
                "accounting_currency": "EUR"}
        data.update(over)
        return data

    def test_pages_render(self):
        for name, args in (("staff:settings", []), ("staff:org_list", []), ("staff:org_new", []),
                           ("staff:org_edit", [self.org.pk])):
            self.assertEqual(self.c.get(reverse(name, args=args)).status_code, 200, name)
        self.assertContains(self.c.get(reverse("staff:settings")), reverse("staff:org_list"))
        self.assertContains(self.c.get(reverse("staff:org_list")), "eu")

    def test_create(self):
        r = self.c.post(reverse("staff:org_new"), self.fields(slug="us", legal_name="Liberation LLC"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(AuditEntry.objects.filter(action="organization.create").exists())

    def test_org_edit_saves_and_audits(self):
        r = self.c.post(self.url, self.fields(legal_name="Liberation LLC", underpay_allowance_bps="75"))
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.legal_name, "Liberation LLC")
        self.assertEqual(self.org.settings["underpay_allowance_bps"], 75)
        e = AuditEntry.objects.get(action="organization.update")
        self.assertIn("legal_name", e.detail["fields"])
        self.assertEqual(e.detail["via"], "staff")
        self.assertNotIn("Liberation LLC", json.dumps(e.detail))
        self.assertEqual(e.actor, self.c.user)

    def test_bad_number_format_shows_the_model_error(self):
        r = self.c.post(self.url, self.fields(number_format="{year}"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "must contain {seq}")
        self.assertFalse(AuditEntry.objects.filter(action="organization.update").exists())

    def test_second_us_issuer_is_refused_beside_the_field(self):
        Organization.objects.create(slug="us", is_us_resident_issuer=True)
        r = self.c.post(self.url, self.fields(is_us_resident_issuer="on"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("is_us_resident_issuer", r.context["form"].errors)

    def test_settings_keys_kept(self):
        self.org.settings = {**self.org.settings, "future_key": [1, 2]}
        self.org.save()
        self.c.post(self.url, self.fields(overpay_ceiling_bps="2000"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.settings["future_key"], [1, 2])
        self.assertEqual(self.org.settings["overpay_ceiling_bps"], 2000)

    def test_typed_settings_are_bounded(self):
        for k, v in (("underpay_allowance_bps", "10001"), ("overpay_ceiling_bps", "100001"),
                     ("underpay_allowance_usd", "-1"), ("accounting_currency", "XXX")):
            r = self.c.post(self.url, self.fields(**{k: v}))
            self.assertEqual(r.status_code, 200, k)
            self.assertIn(k, r.context["form"].errors)

    def test_next_number_is_not_editable(self):
        self.c.post(self.url, self.fields(next_number="99"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.next_number, 1)

    def test_audit_names_only_what_changed(self):
        self.c.post(self.url, self.fields(legal_name="Other Name", underpay_allowance_usd="1"))
        e = AuditEntry.objects.get(action="organization.update")
        self.assertEqual(e.detail["fields"], ["legal_name"])

    def test_audit_names_a_changed_setting(self):
        self.c.post(self.url, self.fields(underpay_allowance_bps="60"))
        e = AuditEntry.objects.get(action="organization.update")
        self.assertEqual(e.detail["fields"], ["underpay_allowance_bps"])

    def test_replaced_logo_file_is_deleted_after_commit(self):
        up = lambda: SimpleUploadedFile("l.png", png_bytes(), "image/png")
        self.c.post(self.url, {**self.fields(), "logo": up()})
        self.org.refresh_from_db()
        old, storage = self.org.logo.name, self.org.logo.storage
        self.assertTrue(storage.exists(old))
        with self.captureOnCommitCallbacks(execute=True):
            self.c.post(self.url, {**self.fields(), "logo": up()})
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.logo.name, old)
        self.assertFalse(storage.exists(old))
        self.assertTrue(storage.exists(self.org.logo.name))

    def test_create_needs_a_legal_name(self):
        r = self.c.post(reverse("staff:org_new"), self.fields(slug="nameless", legal_name=""))
        self.assertEqual(r.status_code, 200)
        self.assertIn("legal_name", r.context["form"].errors)
        self.assertFalse(Organization.objects.filter(slug="nameless").exists())

    def test_unsupported_language_still_edits(self):
        Organization.objects.filter(pk=self.org.pk).update(default_language="xx")
        r = self.c.get(self.url)
        self.assertContains(r, "xx (unsupported)")
        r = self.c.post(self.url, self.fields(default_language="xx"))
        self.assertEqual(r.status_code, 302)

    def test_logo_png_is_reencoded_without_metadata(self):
        raw = png_bytes(text_chunk=True)
        self.assertIn(b"tEXt", raw)
        r = self.c.post(self.url, {**self.fields(), "logo": SimpleUploadedFile("l.png", raw, "image/png")})
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.logo.name.endswith(".png"))
        with self.org.logo.open("rb") as f:
            stored = f.read()
        self.assertNotIn(b"tEXt", stored)
        self.assertEqual(Image.open(io.BytesIO(stored)).format, "PNG")

    def test_logo_jpeg_becomes_png(self):
        raw = png_bytes(fmt="JPEG")
        r = self.c.post(self.url, {**self.fields(), "logo": SimpleUploadedFile("l.jpg", raw, "image/jpeg")})
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.logo.name.endswith(".png"))

    def test_logo_rejects_svg_text_and_oversize(self):
        original = self.org.logo.name
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        big = png_bytes() + b"\0" * (2 * 1024 * 1024 + 1)
        for upload in (SimpleUploadedFile("l.svg", svg, "image/svg+xml"),
                       SimpleUploadedFile("l.png", b"just text, not a picture", "image/png"),
                       SimpleUploadedFile("l.png", big, "image/png")):
            r = self.c.post(self.url, {**self.fields(), "logo": upload})
            self.assertEqual(r.status_code, 400)
        self.org.refresh_from_db()
        self.assertEqual(self.org.logo.name, original)

    def test_sealed_vault_redirects_to_unlock(self):
        vault.seal()
        r = self.c.post(self.url, self.fields())
        self.assertRedirects(r, "/unlock/", fetch_redirect_response=False)

    def test_anonymous_and_unverified_are_refused(self):
        from django.test import Client
        anon = Client()
        for name, args in (("staff:settings", []), ("staff:org_list", []), ("staff:org_new", []),
                           ("staff:org_edit", [self.org.pk])):
            r = anon.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn(reverse("staff:login"), r["Location"])
        r = anon.post(self.url, self.fields())
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.legal_name, "Liberation s.r.o.")

    def test_rail_shows_settings(self):
        page = self.c.get(reverse("staff:needs_person"))
        self.assertContains(page, f'href="{reverse("staff:settings")}"')
        page = self.c.get(reverse("staff:org_list"))
        self.assertContains(page, 'aria-current="page"')
