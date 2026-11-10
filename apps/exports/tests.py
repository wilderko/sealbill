import csv as stdcsv
import datetime
import io
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.contacts.services import find_or_create_contact
from apps.exports import csv as exports_csv, rates
from apps.exports.models import DailyRate
from apps.invoices.models import Invoice
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Payment
from apps.quotes.models import Quote
from apps.rails import http
from apps.rails.models import RailAccount
from apps.staff.tests import StaffBase
from apps.vault.testing import UnsealedVaultMixin

DAY = datetime.date(2026, 10, 5)
LINE = {"name": "Consulting", "quantity": 1, "unit_price_minor": 12345}


def when(day=DAY, hour=12):
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=datetime.timezone.utc)


def parse(text):
    return list(stdcsv.reader(io.StringIO(text)))


class ExportBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu")
        self.other = Organization.objects.create(slug="other")
        for org in (self.org, self.other):
            add_bank(org)

    def invoice(self, org=None, name="Ján", currency="EUR", day=DAY, send=True, line=LINE):
        org = org or self.org
        contact, _ = find_or_create_contact(org, f"{org.slug}@example.com", legal_name=name)
        inv = create_invoice(org, contact, currency=currency, lines=[line], issue_date=day)[0]
        if send and currency == "EUR":
            inv, _v, _p = send_invoice(inv, email_client=False)
        elif send:  # no bank account in this currency: the export only reads the status
            Invoice.objects.filter(pk=inv.pk).update(status="sent")
            inv.refresh_from_db()
        return inv

    def rows(self, inv_org=None, a=DAY, b=DAY, kind="invoices"):
        ids = [(inv_org or self.org).pk]
        make = exports_csv.invoices_rows if kind == "invoices" else exports_csv.payments_rows
        return list(make(ids, a, b))

    def pay(self, inv, **kw):
        fields = dict(invoice=inv, rail="bank", amount_minor=12345, received_amount=123.45, state="confirmed",
                      confirmed_at=when())
        fields.update(kw)
        p = Payment.objects.create(**fields)
        Payment.objects.filter(pk=p.pk).update(detected_at=when())
        return Payment.objects.get(pk=p.pk)


class RowTests(ExportBase):
    def test_invoice_export_columns_and_decimal_amounts(self):
        inv = self.invoice()
        self.pay(inv, amount_minor=5000)
        header, row = self.rows()
        self.assertEqual(header, exports_csv.INVOICE_HEADER)
        d = dict(zip(header, row))
        self.assertEqual((d["organization"], d["kind"], d["status"], d["issue_date"], d["currency"]),
                         ("eu", "invoice", "sent", "2026-10-05", "EUR"))
        self.assertEqual((d["subtotal"], d["discount"], d["tax"], d["total"], d["paid"]),
                         ("123.45", "0.00", "0.00", "123.45", "50.00"))
        self.assertEqual((d["contact"], d["contact_email"]), ("Ján", "eu@example.com"))
        self.assertEqual((d["accounting_currency"], d["accounting_rate"], d["accounting_total"], d["rate_source"]),
                         ("EUR", "1", "123.45", "identity"))

    def test_payment_link_column_holds_the_title_and_is_neutralised(self):
        from apps.links import services as links

        plain = self.invoice()
        link, _ = links.create_link(self.org, title="=HYPERLINK(\"http://x\")", currency="EUR", price_minor=12345)
        order, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="B"))
        Invoice.objects.filter(pk=order.pk).update(issue_date=DAY)
        header, *rows = parse("".join(exports_csv.stream(exports_csv.invoices_rows([self.org.pk], DAY, DAY))))
        self.assertIn("payment_link", header)
        column = header.index("payment_link")
        values = sorted(row[column] for row in rows)
        self.assertEqual(values, ["", "'=HYPERLINK(\"http://x\")"])
        self.assertEqual(plain.payment_link_id, None)

    def test_the_buyer_is_named_as_on_the_document_sent(self):
        """Final review F1: a sent invoice's frozen snapshot names its buyer; a link order's is what the buyer
        typed, never the stored contact, and a contact renamed after sending does not rename the invoice."""
        from apps.contacts.models import Contact
        from apps.links import services as links

        plain = self.invoice(name="Ján As Sent")
        Contact.objects.filter(pk=plain.contact_id).update(legal_name="Renamed Later", display_name="Renamed Later")
        find_or_create_contact(self.org, "b@example.com", legal_name="Stored Secret Name")
        link, _ = links.create_link(self.org, title="Visa", currency="EUR", price_minor=12345)
        order, _pay = links.checkout(link, links.Order(email="b@example.com", us_tax_resident="no", name="Typed Buyer"))
        Invoice.objects.filter(pk=order.pk).update(issue_date=DAY)
        header, *rows = self.rows()
        names = {row[header.index("contact")] for row in rows}
        self.assertEqual(names, {"Ján As Sent", "Typed Buyer"})

    def test_draft_invoices_are_not_exported(self):
        self.invoice(send=False)
        self.assertEqual(len(self.rows()), 1)

    def test_payment_export_has_network_tx_hash_and_quote_rate(self):
        inv = self.invoice()
        account = RailAccount.objects.create(org=self.org, kind="evm", label="w", networks=["base"],
                                             assets=["USDC"], address="0x" + "a" * 40)
        quote = Quote.objects.create(
            invoice=inv, rail_account=account, network="base", asset="USDC", amount_base_units="123450000",
            min_accept_base_units="123450000", ceiling_base_units="123450000", invoice_amount_minor=12345,
            rate="1.0834", issued_at=when(), expires_at=when(hour=13))
        self.pay(inv, rail="evm", network="base", asset="USDC", amount_base_units="123450000", quote=quote,
                 tx_hash="0x" + "b" * 64, log_index=3, received_amount=123.45)
        header, row = self.rows(kind="payments")
        self.assertEqual(header, exports_csv.PAYMENT_HEADER)
        d = dict(zip(header, row))
        self.assertEqual((d["rail"], d["network"], d["asset"], d["amount"]), ("evm", "base", "USDC", "123.45"))
        self.assertEqual((d["tx_hash"], d["log_index"], d["quote_rate"]), ("0x" + "b" * 64, "3", "1.0834"))
        self.assertEqual((d["invoice_currency"], d["amount_invoice_currency"]), ("EUR", "123.45"))

    def test_accounting_conversion_uses_cached_daily_rate(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        inv = self.invoice()
        self.pay(inv)
        DailyRate.objects.create(day=DAY, base="EUR", quote="CZK", rate="24.335", source="frankfurter")
        d = dict(zip(*self.rows()))
        self.assertEqual((d["accounting_currency"], d["accounting_rate"], d["accounting_total"], d["rate_source"]),
                         ("CZK", "24.335", "3004.16", "frankfurter"))  # 123.45 x 24.335 = 3004.16
        p = dict(zip(*self.rows(kind="payments")))
        self.assertEqual((p["accounting_rate"], p["accounting_amount"]), ("24.335", "3004.16"))

    def test_missing_rate_leaves_cells_empty_and_says_missing(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        self.pay(self.invoice())
        d = dict(zip(*self.rows()))
        self.assertEqual((d["accounting_rate"], d["accounting_total"], d["rate_source"]), ("", "", "missing"))
        self.assertEqual(d["accounting_currency"], "CZK")
        p = dict(zip(*self.rows(kind="payments")))
        self.assertEqual((p["accounting_rate"], p["accounting_amount"]), ("", ""))

    def test_existing_org_without_setting_falls_back_to_eur(self):
        self.org.settings = {}
        self.org.save()
        self.assertEqual(rates.accounting_currency(self.org), "EUR")

    def test_formula_cells_are_neutralised(self):
        self.invoice(name='=HYPERLINK("http://x","y")')
        text = "".join(exports_csv.stream(self.rows()))
        row = parse(text)[1]
        self.assertIn("'=HYPERLINK", "".join(row))
        for cell in row:
            self.assertFalse(cell.startswith(("=", "+", "-", "@")), cell)
        for raw in ("=1+1", "+1", "-5.00", "@x", "\tx", "\rx"):
            self.assertEqual(exports_csv.safe_cell(raw), "'" + raw)
        self.assertEqual(exports_csv.safe_cell("plain"), "plain")
        self.assertEqual(exports_csv.safe_cell(None), "")

    def test_undecryptable_contact_exports_a_dash(self):
        self.invoice()
        from apps.vault.fields import UndecryptableValue

        with mock.patch.object(exports_csv, "_contact_cells", side_effect=None) as m:
            m.return_value = (exports_csv.NO_VALUE, exports_csv.NO_VALUE)
            self.assertEqual(self.rows()[1][6], "—")
        self.assertEqual(exports_csv._guarded(mock.Mock(side_effect=UndecryptableValue("x"))), "—")

    def test_export_never_calls_out(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "USD"}
        self.org.save()
        inv = self.invoice()
        self.pay(inv)
        boom = AssertionError("the export made an HTTP call")
        with mock.patch.object(http, "get_json", side_effect=boom), mock.patch.object(http, "post_json", side_effect=boom), \
                mock.patch("requests.get", side_effect=boom), mock.patch("requests.post", side_effect=boom):
            self.assertEqual(len(self.rows()), 2)
            self.assertEqual(len(self.rows(kind="payments")), 2)
            self.assertIsNone(rates.daily_rate("EUR", "USD", DAY))


BITSTAMP_OHLC = {"data": {"pair": "BTC/EUR", "ohlc": [{"timestamp": "1791158400", "open": "1", "high": "2", "low": "0.5",
                                                     "close": "61000.5", "volume": "1"}]}}


class BackfillTests(ExportBase):
    def test_backfill_fills_needed_pairs_from_frankfurter_and_bitstamp(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        self.pay(self.invoice())  # EUR, 5 Oct
        self.invoice(currency="BTC", day=DAY, line={"name": "x", "quantity": 1, "unit_price_minor": 100000})
        self.invoice(currency="CZK", day=DAY, line={"name": "y", "quantity": 1, "unit_price_minor": 1000})
        seen = []

        def fake(url, **kw):
            seen.append(url)
            if "frankfurter" in url:
                assert "/2026-10-05?base=EUR&symbols=CZK" in url, url
                return {"rates": {"CZK": 24.3}}
            assert url == "https://www.bitstamp.net/api/v2/ohlc/btceur/?step=86400&limit=1&start=1791158400", url
            return BITSTAMP_OHLC

        with mock.patch.object(http, "get_json", side_effect=fake):
            stored = rates.backfill(limit=50)
        self.assertEqual(stored, 2)
        self.assertEqual(rates.daily_rate("EUR", "CZK", DAY), Decimal("24.3"))
        self.assertEqual(DailyRate.objects.get(base="BTC").source, "bitstamp×ecb")
        self.assertEqual(rates.daily_rate("BTC", "CZK", DAY), Decimal("61000.5") * Decimal("24.3"))
        self.assertEqual(DailyRate.objects.filter(base="CZK").count(), 0)  # same currency: nothing to fetch
        with mock.patch.object(http, "get_json", side_effect=AssertionError("refetched")):
            self.assertEqual(rates.backfill(), 0)

    def test_bitstamp_direct_and_limit_and_unavailable(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "EUR"}
        self.org.save()
        self.invoice(currency="BTC", line={"name": "x", "quantity": 1, "unit_price_minor": 100000})
        with mock.patch.object(http, "get_json", return_value=BITSTAMP_OHLC):
            self.assertEqual(rates.backfill(), 1)
        self.assertEqual(DailyRate.objects.get().source, "bitstamp")
        DailyRate.objects.all().delete()
        with mock.patch.object(http, "get_json", side_effect=http.Unavailable("bitstamp", "timeout")):
            self.assertEqual(rates.backfill(), 0)
        self.assertEqual(DailyRate.objects.count(), 0)  # left for the next run
        with mock.patch.object(http, "get_json", side_effect=http.Refused("frankfurter", 404)):
            self.org.settings = {**self.org.settings, "accounting_currency": "KGS"}
            self.org.save()
            self.assertEqual(rates.backfill(), 0)
        self.assertEqual(DailyRate.objects.filter(rate="").count(), 1)  # remembered as unobtainable

    def test_migrated_invoices_are_never_backfilled(self):
        """Old BTC days of carried-over history would be asked of Kraken (720 candles) every hour, for ever."""
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        inv = self.invoice()
        pay = self.pay(inv)
        needed = rates.needed_pairs()
        self.assertTrue(needed)
        Invoice.objects.filter(pk=inv.pk).update(external_reference="zaprite:od_old")
        self.assertEqual(rates.needed_pairs(), set())  # the invoice and its payment both
        Invoice.objects.filter(pk=inv.pk).update(external_reference="rf:591")
        self.assertEqual(rates.needed_pairs(), set())
        Invoice.objects.filter(pk=inv.pk).update(external_reference="order-12")
        self.assertEqual(rates.needed_pairs(), needed)
        self.assertTrue(pay.pk)

    def test_limit_caps_the_work(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        for i in range(3):
            self.invoice(day=DAY - datetime.timedelta(days=i))
        with mock.patch.object(http, "get_json", return_value={"rates": {"CZK": 25}}):
            self.assertEqual(rates.backfill(limit=2), 2)

    def test_today_is_not_fetched(self):
        self.org.settings = {**self.org.settings, "accounting_currency": "CZK"}
        self.org.save()
        self.invoice(day=timezone.now().date())
        with mock.patch.object(http, "get_json", side_effect=AssertionError("asked")):
            self.assertEqual(rates.backfill(), 0)

    def test_job_is_registered_hourly(self):
        from apps.core import jobs, worker

        worker.reset()
        self.addCleanup(worker.reset)
        jobs.register_all()
        self.assertEqual(worker._jobs["accounting_rates"]["every_s"], 3600)


class ApiExportTests(ExportBase):
    def client_for(self, org, scopes=("read",)):
        _t, plain = ApiToken.issue(org, "t", list(scopes))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return c

    def test_api_export_scoped_to_token_org_and_range_capped(self):
        mine = self.invoice()
        self.invoice(org=self.other)
        self.pay(mine)
        c = self.client_for(self.org)
        r = c.get("/api/v1/exports/invoices.csv?from=2026-10-01&to=2026-10-31")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertIn('filename="invoices-2026-10-01-2026-10-31.csv"', r["Content-Disposition"])
        rows = parse(b"".join(r.streaming_content).decode())
        self.assertEqual([x[0] for x in rows[1:]], ["eu"])
        r = c.get("/api/v1/exports/payments.csv?from=2026-10-01&to=2026-10-31")
        self.assertEqual(len(parse(b"".join(r.streaming_content).decode())), 2)
        for query in ("from=2025-01-01&to=2026-10-31", "from=2026-10-31&to=2026-10-01", "from=nope&to=2026-10-01", ""):
            r = c.get(f"/api/v1/exports/invoices.csv?{query}")
            self.assertEqual((r.status_code, r.json()["error"]), (400, "bad_range"), query)
        self.assertEqual(c.get("/api/v1/exports/payments.csv?from=2026-01-01&to=2027-02-04").status_code, 200)  # 399 days
        self.assertEqual(APIClient().get("/api/v1/exports/invoices.csv?from=2026-10-01&to=2026-10-02").status_code, 401)
        nope = self.client_for(self.org, scopes=("invoices:write",))
        self.assertEqual(nope.get("/api/v1/exports/invoices.csv?from=2026-10-01&to=2026-10-02").status_code, 403)


class StaffExportTests(StaffBase):
    def test_staff_export_streams(self):
        self.sign_in()
        page = self.client.get("/exports/")
        self.assertContains(page, "invoices.csv")
        issued = self.invoice.issue_date.isoformat()
        r = self.client.get(f"/exports/invoices.csv?from={issued}&to={issued}")
        self.assertTrue(r.streaming)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(r["Content-Disposition"], f'attachment; filename="invoices-{issued}-{issued}.csv"')
        rows = parse(b"".join(r.streaming_content).decode())
        self.assertEqual(rows[0], exports_csv.INVOICE_HEADER)
        self.assertEqual(len(rows), 2)
        bad = self.client.get("/exports/payments.csv?from=x&to=y")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get(f"/exports/payments.csv?from={issued}&to={issued}").status_code, 200)

    def test_staff_export_requires_sign_in(self):
        self.assertEqual(self.client.get("/exports/invoices.csv?from=2026-01-01&to=2026-01-02").status_code, 302)
