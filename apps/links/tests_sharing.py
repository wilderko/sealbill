"""Sharing a payment link (spec 2.2): URL, QR as PNG and SVG, a printable PDF, the button snippet, rotation."""
import html
import io
import re
import subprocess
import tempfile

import qrcode
from django.test import override_settings
from PIL import Image

from apps.audit.models import AuditEntry
from apps.links import services as links
from apps.links import sharing

from .tests_staff import SITE, StaffLinkBase


def matrix(text):
    """The modules of the QR a decoder would have to read for `text`: True is dark."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


def modules_of(png: bytes, border_modules: int):
    """Read the module grid back out of a QR PNG whose border is `border_modules` modules wide."""
    img = Image.open(io.BytesIO(png)).convert("L")
    side = img.size[0]
    assert img.size[0] == img.size[1]
    # The grid is (n + 2*border) modules across; find n by trying the sizes a QR can have.
    for n in range(21, 178, 4):
        total = n + 2 * border_modules
        if side % total == 0:
            box = side // total
            if box < 2:
                continue
            grid = [[img.getpixel(((border_modules + x) * box + box // 2, (border_modules + y) * box + box // 2)) < 128
                     for x in range(n)] for y in range(n)]
            yield grid


class SharingBase(StaffLinkBase):
    def setUp(self):
        super().setUp()
        self.link, self.plain = links.create_link(self.org, title='Tour <b>"A"</b> & more', currency="EUR",
                                                  price_minor=10000)
        self.url = f"{SITE}/l/{self.plain}/"
        self.sign_in()

    def assertDecodesTo(self, png, text):
        want = matrix(text)
        grids = list(modules_of(png, sharing.QR_BORDER))
        self.assertTrue(any(g == want for g in grids), "the QR does not carry the link URL")


class QrTests(SharingBase):
    def test_png_is_an_attachment_with_a_safe_name_and_decodes_to_the_url(self):
        r = self.client.get(f"/links/{self.link.pk}/qr.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertEqual(r["Content-Disposition"], f'attachment; filename="payment-link-{self.link.pk}.png"')
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertDecodesTo(r.content, self.url)

    def test_svg_is_an_attachment_and_carries_the_url_modules(self):
        r = self.client.get(f"/links/{self.link.pk}/qr.svg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/svg+xml")
        self.assertEqual(r["Content-Disposition"], f'attachment; filename="payment-link-{self.link.pk}.svg"')
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertNotIn(b"<script", r.content)
        self.assertEqual(r.content, sharing.qr_svg(self.url))
        # the same QR as the PNG's: render the SVG's path back to modules
        svg = r.content.decode()
        self.assertIn("<svg", svg)
        self.assertNotIn(self.plain, svg)  # the picture holds modules, not text

    def test_pages_need_staff(self):
        self.client.logout()
        for tail in ("qr.png", "qr.svg", "sheet.pdf"):
            r = self.client.get(f"/links/{self.link.pk}/{tail}")
            self.assertEqual(r.status_code, 302, tail)

    def test_unknown_link_is_404(self):
        for tail in ("qr.png", "qr.svg", "sheet.pdf"):
            self.assertEqual(self.client.get(f"/links/999999/{tail}").status_code, 404)


class SheetTests(SharingBase):
    def get_pdf(self):
        r = self.client.get(f"/links/{self.link.pk}/sheet.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["Content-Disposition"], f'inline; filename="payment-link-{self.link.pk}.pdf"')
        self.assertTrue(r.content.startswith(b"%PDF"))
        return r.content

    def test_pdf_has_the_title_and_the_url_and_a_qr_that_decodes_to_it(self):
        from pypdf import PdfReader

        pdf = self.get_pdf()
        text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        self.assertIn('Tour <b>"A"</b> & more', text)  # escaped on the way in, plain on the page
        self.assertIn(self.plain, text.replace("\n", "").replace(" ", ""))
        with tempfile.TemporaryDirectory() as d:
            open(f"{d}/s.pdf", "wb").write(pdf)
            subprocess.run(["pdfimages", "-png", f"{d}/s.pdf", f"{d}/img"], check=True)
            images = sorted(p for p in __import__("os").listdir(d) if p.endswith(".png"))
            self.assertEqual(len(images), 1)
            self.assertDecodesTo(open(f"{d}/{images[0]}", "rb").read(), self.url)


class SnippetTests(SharingBase):
    def test_snippet_escapes_the_title(self):
        snippet = sharing.button_snippet(self.url, 'Tour <b>"A"</b> & more')
        self.assertEqual(snippet, f'<a href="{self.url}" target="_blank" rel="noopener">'
                                  'Tour &lt;b&gt;&quot;A&quot;&lt;/b&gt; &amp; more</a>')

    def test_detail_page_offers_everything_and_the_textarea_holds_the_escaped_snippet(self):
        r = self.client.get(f"/links/{self.link.pk}/")
        page = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/links/{self.link.pk}/qr.png", page)
        self.assertIn(f"/links/{self.link.pk}/qr.svg", page)
        self.assertIn(f"/links/{self.link.pk}/sheet.pdf", page)
        area = re.search(r'<textarea[^>]*readonly[^>]*>(.*?)</textarea>', page, re.S)
        self.assertIsNotNone(area)
        self.assertEqual(html.unescape(area.group(1)), sharing.button_snippet(self.url, 'Tour <b>"A"</b> & more'))
        self.assertNotIn('<b>"A"', area.group(1))
        self.assertIn(f'data-copy="{html.escape(sharing.button_snippet(self.url, chr(84) + "our <b>" + chr(34) + "A" + chr(34) + "</b> & more"))}"',
                      page.replace("&#x27;", "'"))
        self.assertIn("<img", page)  # the preview of the QR


class RotationTests(SharingBase):
    def test_rotation_kills_the_old_url_at_once_and_is_audited_without_the_token(self):
        old = self.plain
        self.assertEqual(self.client.get(f"/l/{old}/").status_code, 200)
        r = self.client.post(f"/links/{self.link.pk}/rotate/")
        self.assertEqual(r.status_code, 302)
        self.link.refresh_from_db()
        self.assertNotEqual(self.link.token, old)
        self.assertEqual(self.client.get(f"/l/{old}/").status_code, 404)
        self.assertEqual(self.client.get(f"/l/{self.link.token}/").status_code, 200)
        entry = AuditEntry.objects.get(action="link.rotate")
        self.assertEqual(entry.detail["channel"], "staff")
        self.assertNotIn(old, str(entry.detail))
        self.assertNotIn(self.link.token, str(entry.detail))
        # the QR now carries the new URL
        png = self.client.get(f"/links/{self.link.pk}/qr.png").content
        self.assertDecodesTo(png, f"{SITE}/l/{self.link.token}/")

    def test_rotation_is_post_only_and_needs_csrf(self):
        self.assertEqual(self.client.get(f"/links/{self.link.pk}/rotate/").status_code, 405)
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        self.assertEqual(strict.post(f"/links/{self.link.pk}/rotate/").status_code, 403)

    def test_the_page_asks_before_replacing(self):
        page = self.client.get(f"/links/{self.link.pk}/").content.decode()
        form = re.search(r'<form[^>]*rotate[^>]*>.*?</form>', page, re.S).group(0)
        self.assertIn("csrfmiddlewaretoken", form)
        self.assertIn("stops working at once", page)
