"""tools/migrate/zaprite_export.py, without Django and without the network.

Runs in the project's suite (`manage.py test`) and on its own:
    python3 -m unittest tools.migrate.tests_zaprite_export
The fixtures in testdata/ are synthetic (example.test addresses, "Fixture" names).
"""
import io
import json
import os
import stat
import tempfile
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock

from tools.migrate import zaprite_export as ze

DATA = Path(__file__).parent / "testdata"
CONTACTS = json.loads((DATA / "zaprite_contacts.json").read_text())
ORDERS = json.loads((DATA / "zaprite_orders.json").read_text())


class FakeClient:
    api_url = "http://zaprite.test"

    def __init__(self, pages):
        self.pages = pages  # {path: [items of page 1, items of page 2, ...]}
        self.calls = []
        self.session = self

    def get(self, url, params=None, timeout=None):
        path = url.replace(self.api_url, "")
        self.calls.append((path, params["page"], dict(params)))
        items = self.pages[path]
        resp = mock.Mock()
        resp.json.return_value = {"items": items[params["page"] - 1], "meta": {"pagesCount": len(items)}}
        return resp


def run(*args, orders=None, client=None):
    client = client or FakeClient({"/v1/contacts": [CONTACTS[:1], CONTACTS[1:]],
                                   "/v1/orders": [ORDERS if orders is None else orders]})
    out, err = io.StringIO(), io.StringIO()
    code = ze.main(list(args), client=client, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue(), client


def records(out):
    return [json.loads(line) for line in out.splitlines()]


def by_id(out):
    return {(r["type"], r["source_id"]): r for r in records(out)}


class MappingTest(unittest.TestCase):
    def test_invoice_order(self):
        r = ze.order_line(ORDERS[0])
        self.assertEqual((r["type"], r["source_id"], r["number"], r["kind"]), ("invoice", "od_inv1", "449", "invoice"))
        self.assertEqual((r["title"], r["footer"]), ("Residency fixture", "Thanks, Fixture footer"))
        self.assertEqual((r["issue_date"], r["due_days"], r["paid_date"]), ("2025-04-01", 14, "2025-04-10"))
        self.assertEqual(r["source_url"], "https://pay.zaprite.test/invoice/inv1")
        self.assertEqual((r["contact_source_id"], r["source_status"]), ("ct_1", "COMPLETE"))
        self.assertEqual(r["lines"][0], {"name": "Service A", "description": "Desc A", "quantity": 1,
                                         "unit_price_minor": 133700})
        self.assertEqual(r["total_minor"], 143700)

    def test_amounts_are_integers_unchanged(self):
        r = ze.order_line(ORDERS[0])
        self.assertTrue(all(isinstance(line["unit_price_minor"], int) for line in r["lines"]))
        self.assertIsInstance(r["total_minor"], int)

    def test_paid_date_from_confirmed_transactions_only(self):
        r = ze.order_line(ORDERS[1])
        self.assertEqual((r["paid_date"], r["source_status"], r["currency"]), ("2025-05-06", "PROCESSING", "USD"))

    def test_payment_link_is_a_payment_request_without_number(self):
        r = ze.order_line(ORDERS[2])
        self.assertEqual((r["kind"], r["number"], r["title"]), ("payment_request", None, "Link fixture"))
        self.assertEqual(r["lines"], [{"name": "Hotel fixture", "description": "", "quantity": 1,
                                       "unit_price_minor": 99900}])
        self.assertEqual((r["total_minor"], r["issue_date"]), (99900, "2025-06-01"))
        self.assertEqual(r["source_url"], "https://pay.zaprite.test/order/od_pl1")

    def test_total_is_the_orders_total_amount_not_the_line_sum(self):
        r = ze.order_line(ORDERS[3])  # 10000 less a 1500 line discount
        self.assertEqual(r["total_minor"], 8500)
        self.assertEqual(sum(line["unit_price_minor"] * line["quantity"] for line in r["lines"]), 10000)
        self.assertEqual(ze.order_line(ORDERS[5])["total_minor"], 9000)  # promo
        self.assertEqual(ze.order_line(ORDERS[4])["total_minor"], 12000)  # tax

    def test_line_discount_and_tax_rate_are_carried(self):
        self.assertEqual(ze.order_line(ORDERS[3])["lines"][0]["discount_amount_minor"], 1500)
        self.assertNotIn("discount_amount_minor", ze.order_line(ORDERS[0])["lines"][0])
        taxed = ze.order_line(ORDERS[4])["lines"][0]
        self.assertEqual(taxed["tax_rate_bps"], 2000)
        self.assertFalse([k for k in taxed if "tax" in k and k != "tax_rate_bps"])

    def test_rate_and_amount_discount_are_never_both_sent(self):
        line = ze.order_line(ORDERS[6])["lines"][0]
        self.assertEqual(line["discount_rate_bps"], 1000)
        self.assertNotIn("discount_amount_minor", line)

    def test_discount_then_tax_adds_up_the_way_the_importer_computes_it(self):
        r = ze.order_line(ORDERS[8])
        line = r["lines"][0]

        def half_up(x):
            return int(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))

        gross = line["unit_price_minor"] * line["quantity"]
        discount = half_up(Decimal(gross) * line["discount_rate_bps"] / 10000)
        tax = half_up(Decimal(gross - discount) * line["tax_rate_bps"] / 10000)
        self.assertEqual(gross - discount + tax, r["total_minor"])
        self.assertEqual(r["total_minor"], 10805)

    def test_tax_and_promo_orders_are_flagged(self):
        self.assertEqual(ze.carries_tax_or_promo(ORDERS[4]), (True, False))
        self.assertEqual(ze.carries_tax_or_promo(ORDERS[5]), (False, True))
        self.assertEqual(ze.carries_tax_or_promo(ORDERS[0]), (False, False))

    def test_contact_without_email_and_normalised_email(self):
        self.assertIsNone(ze.contact_line(CONTACTS[1])["email"])
        self.assertEqual(ze.contact_line(CONTACTS[1])["display_name"], "Nomail Person")
        c = ze.contact_line(CONTACTS[0])
        self.assertEqual((c["email"], c["tax_id"]), ("jan.fixture@example.test", "SK123"))

    def test_language_only_when_the_contact_states_one(self):
        self.assertNotIn("language", ze.contact_line(CONTACTS[0]))
        self.assertEqual(ze.contact_line(dict(CONTACTS[0], language="sk"))["language"], "sk")


class AddressTest(unittest.TestCase):
    def test_zaprite_names_map_to_the_importer_shape(self):
        self.assertEqual(ze.contact_line(CONTACTS[0])["address"],
                         {"line1": "Fixture Street 1", "city": "Bratislava", "zip": "81101", "country": "SK"})

    def test_every_documented_key_survives(self):
        full = {"line1": "A", "line2": "B", "zipCode": "1", "city": "C", "state": "D", "countryCode": "us"}
        self.assertEqual(ze.contact_line({"id": "x", "billingAddress": full})["address"],
                         {"line1": "A", "line2": "B", "zip": "1", "city": "C", "state": "D", "country": "US"})

    def test_missing_address_is_empty(self):
        self.assertEqual(ze.contact_line(CONTACTS[1])["address"], {})

    def test_an_undocumented_key_is_refused_not_dropped(self):
        with self.assertRaises(ValueError):
            ze.contact_line({"id": "x", "billingAddress": {"line1": "A", "floor": "3"}})


class LeftOutTest(unittest.TestCase):
    def reason(self, order):
        with self.assertRaises(ze.OrderLeftOut) as cm:
            ze.order_line(order)
        return cm.exception.reason

    def test_reasons(self):
        self.assertEqual(self.reason(ORDERS[9]), "no_contact")   # an invoice with no buyer at all
        self.assertEqual(self.reason(ORDERS[10]), "no_total")
        self.assertEqual(self.reason(ORDERS[14]), "no_date")
        self.assertEqual(self.reason(ORDERS[15]), "pending")

    def test_an_order_with_no_contact_takes_its_buyer_from_the_order(self):
        recs = by_id(run()[1])
        c = recs[("contact", "order:od_cust")]
        self.assertEqual((c["email"], c["legal_name"], c["tax_id"]), ("cust@example.test", "Cust Fixture Ltd", "CZ999"))
        self.assertEqual(c["address"], {"line1": "Order Street 5", "line2": "Floor 2", "zip": "11000",
                                        "city": "Praha", "country": "CZ"})
        self.assertEqual(recs[("contact", "order:od_apiorder")]["display_name"], "Link Buyer")

    def test_api_order_lines_are_named_by_label_else_by_id(self):
        self.assertEqual(ze.order_line(ORDERS[12])["lines"],
                         [{"name": "Plain link", "description": "", "quantity": 1, "unit_price_minor": 7000}])
        r = ze.order_line(ORDERS[13])
        self.assertEqual(r["lines"][0]["name"], "Zaprite order od_nolabel")
        self.assertEqual((r["issue_date"], r["paid_date"]), ("2025-09-02", "2025-09-02"))


def _link(oid, paid="2025-08-01T10:00:00.000Z"):
    return {"id": oid, "orderType": "paymentLink", "totalAmount": 2500, "currency": "EUR", "status": "COMPLETE",
            "paidAt": paid, "checkoutUrl": f"https://pay.zaprite.test/order/{oid}", "contact": None,
            "invoice": None, "label": "", "paymentLink": {"id": "pl1", "title": "Pay here"}, "transactions": [],
            "orderItems": [{"label": "Item", "quantity": 1, "paymentLinkItem": {"id": "i1", "title": "Item"}}],
            "customerData": {"address": None, "company": "", "email": "", "name": "", "note": "", "phone": ""}}


class AnonymousPaymentLinkTest(unittest.TestCase):
    def test_anonymous_links_share_one_placeholder_contact_written_before_them(self):
        _, out, err, _ = run(orders=[_link("od_a1"), _link("od_a2")])
        recs = records(out)
        anon = [r for r in recs if r["type"] == "contact" and r["source_id"] == ze.ANONYMOUS_CONTACT_ID]
        self.assertEqual(len(anon), 1)
        self.assertEqual((anon[0]["email"], anon[0]["address"]), (None, {}))
        pos = {(r["type"], r["source_id"]): i for i, r in enumerate(recs)}
        for oid in ("od_a1", "od_a2"):
            self.assertEqual(recs[pos[("invoice", oid)]]["contact_source_id"], ze.ANONYMOUS_CONTACT_ID)
            self.assertLess(pos[("contact", ze.ANONYMOUS_CONTACT_ID)], pos[("invoice", oid)])
        self.assertIn("anonymous_payment_links=2", err)

    def test_no_anonymous_order_means_no_placeholder(self):
        _, out, err, _ = run(orders=[ORDERS[0]])
        self.assertNotIn(ze.ANONYMOUS_CONTACT_ID, out)
        self.assertIn("anonymous_payment_links=0", err)


class CommandTest(unittest.TestCase):
    def test_stdout_is_json_lines_contacts_first(self):
        code, out, _, _ = run()
        self.assertEqual(code, 0)
        recs = records(out)
        self.assertEqual([r["type"] for r in recs], ["contact"] * 6 + ["invoice"] * 12)
        ids = [r["source_id"] for r in recs if r["type"] == "contact"]
        self.assertEqual(ids, ["ct_1", "ct_2", "ct_3", ze.ANONYMOUS_CONTACT_ID, "order:od_cust", "order:od_apiorder"])

    def test_stderr_names_every_order_left_out_and_holds_no_personal_data(self):
        _, out, err, _ = run()
        for line in ("contacts=6", "orders=12", "no_contact=1: od_nocontact", "no_total=1: od_nototal",
                     "no_date=1: od_nodate", "pending=1: od_pending", "tax_orders=2: od_tax, od_dt",
                     "promo_orders=1: od_promo", "order_status PENDING=1", "invoice_status none="):
            self.assertIn(line, err)
        for personal in ("Fixture", "Nomail", "example.test", "Bratislava", "143700"):
            self.assertNotIn(personal, err)
        ids = [r["source_id"] for r in records(out)]
        for left in ("od_nocontact", "od_nototal", "od_nodate", "od_pending"):
            self.assertNotIn(left, ids)

    def test_paging_stops_at_pages_count_and_sends_no_limit(self):
        client = FakeClient({"/v1/x": [[{"id": 1}], [{"id": 2}], [{"id": 3}]]})
        self.assertEqual([i["id"] for i in ze.iter_pages(client, "/v1/x")], [1, 2, 3])
        _, _, _, client = run()
        self.assertTrue(all(set(c[2]) == {"page"} for c in client.calls))

    def test_output_file_is_0600_and_stdout_stays_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "z.ndjson")
            code, out, _, _ = run(path)
            self.assertEqual((code, out), (0, ""))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(len(Path(path).read_text().splitlines()), 18)

    def test_a_failed_export_leaves_no_file(self):
        class Broken(FakeClient):
            def get(self, url, params=None, timeout=None):
                if "/v1/orders" in url:
                    raise OSError("connection reset")
                return super().get(url, params, timeout)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "z.ndjson")
            code, _, err, _ = run(path, client=Broken({"/v1/contacts": [CONTACTS]}))
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(path))
            self.assertIn("export failed", err)

    def test_without_a_key_nothing_is_read(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"ZAPRITE_API_KEY": ""}):
            self.assertEqual(ze.main([], stdout=out, stderr=err), 2)
        self.assertIn("ZAPRITE_API_KEY", err.getvalue())
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
