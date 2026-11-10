from types import SimpleNamespace as NS

from django.test import SimpleTestCase

from apps.invoices.reference import is_valid, payment_reference


class ReferenceTests(SimpleTestCase):
    def test_reference_is_valid_iso11649(self):
        ref = payment_reference(NS(kind="invoice", number="2026-0001", pk=1))
        self.assertTrue(ref.startswith("RF"))
        self.assertLessEqual(len(ref), 25)
        self.assertTrue(is_valid(ref))
        bad = ref[:-1] + ("0" if ref[-1] != "0" else "1")
        self.assertFalse(is_valid(bad))

    def test_payment_request_uses_pk(self):
        ref = payment_reference(NS(kind="payment_request", number=None, pk=42))
        self.assertTrue(ref.endswith("P42"))
        self.assertTrue(is_valid(ref))
