import datetime
import io

from django.test import TestCase
from django.utils import timezone

from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice
from apps.orgs.models import BankAccount, Organization
from apps.vault.testing import UnsealedVaultMixin

try:
    from weasyprint import HTML  # noqa: F401
    WEASY_ERROR = None
except Exception as exc:  # missing Pango/cairo system libraries
    WEASY_ERROR = exc


def text_of(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


class PdfTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        if WEASY_ERROR:
            self.skipTest(f"WeasyPrint system libraries are missing: {WEASY_ERROR}")
        self.org = Organization.objects.create(
            slug="eu", legal_name="Seller s.r.o.", address="Main 1\nBratislava", tax_ids="VAT SK123"
        )
        self.contact, _ = find_or_create_contact(
            self.org, "a@example.com", legal_name="Ján Žilinský",
            address={"line1": "Hlavná 5", "city": "Košice", "zip": "04001", "country": "SK"},
        )
        self.bank = BankAccount.objects.create(
            org=self.org, currency="EUR", label="Main EUR", beneficiary_name="Seller s.r.o.",
            account_number="SK0000000000001", bank_name="Bank A", bank_swift="BANKSKBX",
            intermediary_bank_name="Inter Bank", intermediary_swift="INTERXXX9",
            intermediary_account="CORR-777",
        )

    def make(self, **kw):
        lines = [
            {"name": "숙박", "quantity": 1, "unit_price_minor": 133700},
            {"name": "Проживание", "quantity": 2, "unit_price_minor": 5000},
        ]
        return create_invoice(
            self.org, self.contact, currency="EUR", lines=lines,
            offered_methods=[f"bank:{self.bank.pk}"], **kw,
        )[0]

    def test_pdf_text_layer_keeps_slovak_and_korean_and_cyrillic(self):
        from apps.invoices.pdf import render_invoice_pdf

        text = text_of(render_invoice_pdf(self.make(), "en"))
        for s in ("Ján Žilinský", "숙박", "Проживание"):
            self.assertIn(s, text)

    def test_pdf_due_on_receipt_label_in_sk(self):
        from apps.invoices.pdf import render_invoice_pdf

        text = text_of(render_invoice_pdf(self.make(), "sk"))
        self.assertIn("Splatné ihneď", text)

    def test_pdf_lists_intermediary_bank(self):
        from apps.invoices.pdf import render_invoice_pdf

        inv = self.make()
        text = text_of(render_invoice_pdf(inv, "en"))
        for s in ("INTERXXX9", "CORR-777", "SK0000000000001", "BANKSKBX"):
            self.assertIn(s, text)
        from apps.invoices.reference import payment_reference
        self.assertIn(payment_reference(inv), text.replace(" ", ""))

    def test_pdf_prints_beneficiary_and_bank_addresses(self):
        from apps.invoices.pdf import render_invoice_pdf

        self.bank.beneficiary_address = "Hlavná 1, 811 01 Bratislava"
        self.bank.bank_address = "Bankova 9, Tbilisi"
        self.bank.save()
        text = text_of(render_invoice_pdf(self.make(), "en"))
        for s in ("Beneficiary address", "Hlavná 1, 811 01 Bratislava", "Bank address", "Bankova 9, Tbilisi"):
            self.assertIn(s, text)
        sk = text_of(render_invoice_pdf(self.make(), "sk"))
        self.assertIn("Adresa príjemcu", sk)
        self.assertIn("Adresa banky", sk)

    def test_money_grouping_follows_language(self):
        from apps.invoices.pdf import format_money

        from django.utils import translation
        with translation.override("en"):
            self.assertEqual(format_money(133700, "EUR"), "1,337.00 EUR")
        with translation.override("sk"):
            self.assertEqual(format_money(133700, "EUR").replace("\xa0", " "), "1 337,00 EUR")

    def test_receipt_says_paid(self):
        from apps.invoices.pdf import render_receipt_pdf

        inv = self.make()
        inv.status = "paid"
        inv.paid_at = timezone.now()
        inv.save()
        text = text_of(render_receipt_pdf(inv, "en"))
        self.assertIn("PAID", text)
        self.assertIn("Receipt", text)


class PdfFetchSafetyTests(PdfTests):
    def test_hostile_markup_fetches_nothing_outside_static(self):
        from unittest import mock

        from apps.invoices import pdf

        self.contact.legal_name = '<img src="file:///etc/hostname">'
        self.contact.notes = '<img src="http://example.com/x.png">'
        self.contact.save()
        inv = create_invoice(
            self.org, self.contact, currency="EUR", offered_methods=[],
            lines=[{"name": "x", "description": "<style>body{background:url(file:///etc/passwd)}</style>",
                    "quantity": 1, "unit_price_minor": 100}],
        )[0]
        seen = []
        cls = type(pdf.SafeFetcher())
        real = cls.fetch

        def spy(self, url, headers=None):
            seen.append(url)
            return real(self, url, headers)

        with mock.patch.object(cls, "fetch", spy):
            out = pdf.render_invoice_pdf(inv, "en")
        self.assertTrue(out.startswith(b"%PDF"))
        for u in seen:
            self.assertFalse(u.startswith("http") or "/etc/" in u, u)

    def test_fetcher_refuses_outside_paths_and_network(self):
        from apps.invoices.pdf import STATIC_PDF_DIR, SafeFetcher

        safe_url_fetcher = SafeFetcher()

        for u in ("file:///etc/passwd", "http://example.com"):
            with self.assertRaises(ValueError):
                safe_url_fetcher(u)
        r = safe_url_fetcher((STATIC_PDF_DIR / "pdf.css").as_uri())
        self.assertIn(b"font-family", r.read())

    def test_svg_logo_is_not_embedded(self):
        from django.core.files.base import ContentFile

        from apps.invoices.pdf import _logo_uri

        self.org.logo.save("l.svg", ContentFile(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), save=True)
        self.assertIsNone(_logo_uri(self.org))
        self.org.logo.delete()
