from decimal import Decimal
from types import SimpleNamespace as NS

from django.test import SimpleTestCase

from apps.invoices.currencies import minor_units
from apps.invoices.money import line_total


def item(q, unit, disc_rate=0, tax=0, disc_amt=0):
    return NS(
        quantity=Decimal(q), unit_price_minor=unit, discount_rate_bps=disc_rate,
        tax_rate_bps=tax, discount_amount_minor=disc_amt,
    )


class MoneyTests(SimpleTestCase):
    def test_line_with_quantity_discount_and_tax(self):
        self.assertEqual(line_total(item("1.50", 10000, 1000, 2000)), 16200)

    def test_negative_line_reduces_total(self):
        self.assertEqual(line_total(item("1", -2500)), -2500)

    def test_half_up_rounding(self):
        self.assertEqual(line_total(item("0.50", 101)), 51)

    def test_btc_invoice_in_sats(self):
        self.assertEqual(minor_units("BTC"), 8)
        self.assertEqual(minor_units("KRW"), 0)
        self.assertEqual(minor_units("ETH"), 8)  # carried-over invoices only (owner, 2026-11-09)
        from apps.invoices.currencies import check_issuable
        with self.assertRaises(ValueError):
            check_issuable("ETH")
        with self.assertRaises(ValueError):
            minor_units("DOGE")
