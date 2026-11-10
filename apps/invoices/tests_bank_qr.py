"""Bank-transfer QR codes: PAY by square (Slovakia), QR Platba (Czech Republic), EPC QR (SEPA)."""
from decimal import Decimal

from django.test import SimpleTestCase

from apps.invoices import bank_qr

SK_IBAN = "SK9611000000002918599669"  # the bysquare reference library's test IBAN
CZ_IBAN = "CZ6508000000192000145399"  # the Czech Banking Association's example account
DE_IBAN = "DE89370400440532013000"

# xseman/bysquare, typescript/src/pay/testdata/wire-format-test-data.ts: VALID_PAYMENT_ORDER encoded
# as version 1.2.0 (invoiceId "test-001", 100 EUR, VS 123, beneficiary "John Doe").
BYSQUARE_GOLDEN_120 = ("0804O0002G0L2UES834BQT9SQJQA9M5QGN1AHN4VO0KB6MVM9RPGR6APCGPRILE5PJ8VTQ52RPSLT6TC"
                       "JVVC4BMR03J4998N393G7940TB936AFDVVVQCKA000")
BYSQUARE_GOLDEN_PAYLOAD = ("test-001\t1\t1\t100\tEUR\t\t123\t\t\t\t\t1\tSK9611000000002918599669\t\t0\t0\t"
                           "John Doe\t\t")


class PayBySquareTests(SimpleTestCase):
    def test_the_reference_librarys_golden_string_decodes_to_its_payload(self):
        version, payload = bank_qr.decode_pay_by_square(BYSQUARE_GOLDEN_120)
        self.assertEqual((version, payload), (2, BYSQUARE_GOLDEN_PAYLOAD))

    def test_our_encoding_of_that_payload_decodes_to_the_same(self):
        text = bank_qr.encode_pay_by_square(BYSQUARE_GOLDEN_PAYLOAD, version=2)
        self.assertEqual(bank_qr.decode_pay_by_square(text), (2, BYSQUARE_GOLDEN_PAYLOAD))
        self.assertTrue(text.startswith("08"))

    def test_a_transfer_is_encoded_as_version_1_1_with_the_fields_in_order(self):
        text = bank_qr.pay_by_square(iban=SK_IBAN, bic="TATRSKBX", amount=Decimal("1337.00"), currency="EUR",
                                     note="RF6320260007", variable_symbol="20260007",
                                     beneficiary_name="Example Studio s.r.o.")
        version, payload = bank_qr.decode_pay_by_square(text)
        self.assertEqual(version, 1)
        self.assertEqual(payload.split("\t"), [
            "", "1", "1", "1337", "EUR", "", "20260007", "", "", "", "RF6320260007", "1", SK_IBAN, "TATRSKBX",
            "0", "0", "Example Studio s.r.o.", "", ""])

    def test_diacritics_and_tabs_are_flattened(self):
        text = bank_qr.pay_by_square(iban=SK_IBAN, bic="", amount=Decimal("12.50"), currency="EUR",
                                     note="Faktúra\t42", variable_symbol="", beneficiary_name="Žltý kôň s.r.o.")
        fields = bank_qr.decode_pay_by_square(text)[1].split("\t")
        self.assertEqual((fields[3], fields[10], fields[16]), ("12.5", "Faktura 42", "Zlty kon s.r.o."))


class EpcTests(SimpleTestCase):
    def test_a_structured_reference_goes_on_its_own_line(self):
        self.assertEqual(
            bank_qr.epc_qr(iban=DE_IBAN, bic="COBADEFFXXX", amount=Decimal("99.9"), beneficiary_name="Studio GmbH",
                           reference="RF6320260007"),
            "BCD\n002\n1\nSCT\nCOBADEFFXXX\nStudio GmbH\nDE89370400440532013000\nEUR99.90\n\nRF6320260007")

    def test_anything_else_is_unstructured_remittance(self):
        self.assertEqual(
            bank_qr.epc_qr(iban=DE_IBAN, bic="", amount=Decimal("5"), beneficiary_name="Studio GmbH",
                           reference="Invoice 42"),
            "BCD\n002\n1\nSCT\n\nStudio GmbH\nDE89370400440532013000\nEUR5.00\n\n\nInvoice 42")

    def test_no_code_without_a_beneficiary_or_outside_the_amount_range(self):
        for kwargs in ({"beneficiary_name": ""}, {"amount": Decimal("0")}, {"amount": Decimal("1000000000")}):
            args = {"iban": DE_IBAN, "bic": "", "amount": Decimal("1"), "beneficiary_name": "X", "reference": "",
                    **kwargs}
            self.assertIsNone(bank_qr.epc_qr(**args))


class SpaydTests(SimpleTestCase):
    def test_qr_platba_string(self):
        self.assertEqual(
            bank_qr.spayd(iban=CZ_IBAN, bic="GIBACZPX", amount=Decimal("480.5"), currency="CZK",
                          message="RF6320260007", variable_symbol="20260007", beneficiary_name="Firma s.r.o."),
            "SPD*1.0*ACC:CZ6508000000192000145399+GIBACZPX*AM:480.50*CC:CZK*MSG:RF6320260007*X-VS:20260007"
            "*RN:Firma s.r.o.")

    def test_asterisks_are_escaped_and_diacritics_flattened(self):
        text = bank_qr.spayd(iban=CZ_IBAN, bic="", amount=Decimal("1"), currency="CZK", message="a*b",
                             variable_symbol="", beneficiary_name="Žluťoučký kůň")
        self.assertIn("MSG:a%2Ab", text)
        self.assertIn("RN:Zlutoucky kun", text)
        self.assertNotIn("X-VS", text)

    def test_an_amount_too_long_for_the_field_gives_no_code(self):
        self.assertIsNone(bank_qr.spayd(iban=CZ_IBAN, bic="", amount=Decimal("10000000"), currency="CZK",
                                        message="", variable_symbol="", beneficiary_name=""))


class IbanTests(SimpleTestCase):
    def test_valid_ibans_are_normalized(self):
        self.assertEqual(bank_qr.normalize_iban("sk96 1100 0000 0029 1859 9669"), SK_IBAN)
        self.assertEqual(bank_qr.normalize_iban(CZ_IBAN), CZ_IBAN)

    def test_anything_else_is_none(self):
        for value in ("", None, "SK9611000000002918599668", "123456789", "US12345678901234567890"):
            self.assertIsNone(bank_qr.normalize_iban(value))

    def test_variable_symbol_is_the_invoice_numbers_digits_when_they_fit(self):
        self.assertEqual(bank_qr.variable_symbol("2026-0007"), "20260007")
        self.assertEqual(bank_qr.variable_symbol("INV-12345678901"), "")  # 11 digits do not fit
        self.assertEqual(bank_qr.variable_symbol(""), "")


class CodesForTests(SimpleTestCase):
    def method(self, **kw):
        return {"kind": "bank", "account_number": SK_IBAN, "bank_swift": "TATRSKBX",
                "beneficiary_name": "Example Studio s.r.o.", **kw}

    def standards(self, method, currency="EUR", amount_minor=133700):
        return [c["standard"] for c in bank_qr.codes_for(method, amount_minor=amount_minor, currency=currency,
                                                         reference="RF6320260007", invoice_number="2026-0007")]

    def test_a_eur_account_gets_all_three(self):
        self.assertEqual(self.standards(self.method()), ["pay_by_square", "qr_platba", "epc"])

    def test_a_czk_account_gets_qr_platba_only(self):
        self.assertEqual(self.standards(self.method(account_number=CZ_IBAN), currency="CZK"), ["qr_platba"])

    def test_other_currencies_and_non_iban_accounts_get_none(self):
        self.assertEqual(self.standards(self.method(), currency="USD"), [])
        self.assertEqual(self.standards(self.method(account_number="0123456789/0100")), [])

    def test_nothing_is_owed_gives_no_code(self):
        self.assertEqual(self.standards(self.method(), amount_minor=0), [])

    def test_a_eur_account_outside_sepa_gets_no_code(self):
        # Georgia is not in SEPA: a Slovak or Czech app would refuse the code or send a SWIFT payment.
        self.assertEqual(self.standards(self.method(account_number="GE29NB0000000101904917")), [])

    def test_czk_to_an_account_outside_the_czech_republic_gets_no_code(self):
        self.assertEqual(self.standards(self.method(), currency="CZK"), [])

    def test_each_code_is_an_svg_image_with_a_label(self):
        (first, *_rest) = bank_qr.codes_for(self.method(), amount_minor=133700, currency="EUR",
                                            reference="RF6320260007", invoice_number="2026-0007")
        self.assertTrue(first["image"].startswith("data:image/svg+xml;base64,"))
        self.assertTrue(str(first["label"]))
        self.assertTrue(first["text"].startswith("04"))  # PAY by square 1.1.0 header 0x01 0x00


from django.test import TestCase  # noqa: E402

from apps.contacts.services import find_or_create_contact  # noqa: E402
from apps.invoices.services import create_invoice, send_invoice  # noqa: E402
from apps.orgs.models import BankAccount, Organization  # noqa: E402
from apps.vault.testing import UnsealedVaultMixin  # noqa: E402


class PayPageAndPdfTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.", address="Main 1")
        self.contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján", preferred_language="en")

    def sent(self, account_number, currency="EUR"):
        bank = BankAccount.objects.create(org=self.org, currency=currency, label=f"Main {currency}",
                                          beneficiary_name="Seller s.r.o.", account_number=account_number,
                                          bank_name="Bank A", bank_swift="TATRSKBX")
        inv, _ = create_invoice(self.org, self.contact, currency=currency, offered_methods=[f"bank:{bank.pk}"],
                                lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 133700}])
        return send_invoice(inv)

    def test_the_pay_page_shows_the_three_codes_for_a_eur_iban(self):
        invoice, _view, pay = self.sent(SK_IBAN)
        r = self.client.get(f"/p/{pay}/?lang=en")
        self.assertContains(r, 'class="bank-qr"')
        self.assertContains(r, "data:image/svg+xml;base64,", count=3)
        for label in ("PAY by square (Slovakia)", "QR Platba (Czech Republic)", "EPC QR (SEPA)"):
            self.assertContains(r, label)

    def test_the_codes_carry_the_amount_due_and_the_reference(self):
        from apps.invoices.pdf import _context
        from apps.invoices.reference import payment_reference

        invoice, _view, _pay = self.sent(SK_IBAN)
        (method,) = [m for m in _context(invoice, "en")["methods"] if m["kind"] == "bank"]
        by = {c["standard"]: c["text"] for c in method["qr_codes"]}
        fields = bank_qr.decode_pay_by_square(by["pay_by_square"])[1].split("\t")
        self.assertEqual((fields[3], fields[4], fields[10]), ("1337", "EUR", payment_reference(invoice)))
        self.assertIn("EUR1337.00", by["epc"])
        self.assertIn("AM:1337.00*CC:EUR", by["qr_platba"])

    def test_an_account_that_is_not_an_iban_shows_no_code(self):
        _invoice, _view, pay = self.sent("0123456789/0100")
        r = self.client.get(f"/p/{pay}/?lang=en")
        self.assertNotContains(r, 'class="bank-qr"')

    def test_the_pdf_prints_the_codes(self):
        try:
            from weasyprint import HTML  # noqa: F401
        except Exception as exc:  # missing Pango/cairo system libraries
            self.skipTest(f"WeasyPrint system libraries are missing: {exc}")
        import io

        from pypdf import PdfReader

        from apps.invoices.pdf import render_invoice_pdf

        invoice, _view, _pay = self.sent(SK_IBAN)
        reader = PdfReader(io.BytesIO(render_invoice_pdf(invoice, "en")))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("PAY by square (Slovakia)", text)
        self.assertIn("EPC QR (SEPA)", text)
