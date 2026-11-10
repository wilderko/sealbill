import copy
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.orgs.models import Organization
from apps.rails import http
from apps.rails.chains import tron
from apps.rails.chains.base import adapter_for
from apps.rails.models import RailAccount, WatchCursor, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin

FIX = Path(__file__).parent / "fixtures" / "tron"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
RECORDED_TO = "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9"
RECORDED_FROM = "TEPSrSYPDSQ7yXpMFPq91Fb1QEWpMkRGfn"
RECORDED_TX = "8b5e5f9a99d65c0b3aac7f3cbc2ee3029a0d4225054fd99830236d96d853c503"
MINE = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"  # a valid address that is not in the recording
OTHER = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"
T_OLD, T_NEW = 1_764_517_000_000, 1_764_517_689_000


def row(tx, ts, amount, to=MINE, frm=RECORDED_FROM, contract=USDT):
    r = json.loads((FIX / "trc20_list.json").read_text())["data"][0]
    r.update(transaction_id=tx, block_timestamp=ts, value=str(amount), to=to, **{"from": frm})
    r["token_info"]["address"] = contract
    return r


class FakeGrid:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, url, *, params=None, headers=None, service, timeout=10):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.pages.pop(0)


def page(rows, fingerprint=None):
    meta = {"at": 1, "page_size": len(rows)}
    if fingerprint:
        meta["fingerprint"] = fingerprint
    return {"data": rows, "success": True, "meta": meta}


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="lt", legal_name="LT s.r.o.")
        self.acct = RailAccount.objects.create(org=self.org, kind="tron", label="t", address=MINE,
                                               networks=["tron"], assets=["USDT"])
        self.stream = f"tron-trc20:{self.acct.pk}"

    def cursor(self, position, address=None):
        WatchCursor.objects.create(network="tron", address=address or MINE, stream=self.stream, position=str(position))

    def patch(self, grid):
        p = mock.patch("apps.rails.http.get_json", side_effect=grid)
        p.start()
        self.addCleanup(p.stop)
        return grid


class TronScanTests(Base):
    def test_tron_confirmed_transfer_parsed_and_paged(self):
        self.cursor(T_OLD)
        a, b, c = "ab" * 32, "cd" * 32, "ef" * 32
        grid = self.patch(FakeGrid([
            page([row(a, T_OLD + 1000, 5_000_000), row("11" * 32, T_OLD + 2000, 1, to=OTHER),
                  row("22" * 32, T_OLD + 2000, 1, contract="TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj")],
                 fingerprint="FP1"),
            page([row(b.upper(), T_NEW, 7_000_000), row(b, T_NEW, 3_000_000, frm=OTHER), row(c, T_NEW, 9)]),
        ]))
        got = adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(len(grid.calls), 2)
        url, params, headers = grid.calls[0]
        self.assertTrue(url.endswith(f"/v1/accounts/{MINE}/transactions/trc20"))
        self.assertEqual((params["only_confirmed"], params["only_to"], params["contract_address"]),
                         ("true", "true", USDT))
        self.assertEqual(params["min_timestamp"], T_OLD - 600_000)  # the overlap re-read
        self.assertEqual(params["order_by"], "block_timestamp,asc")  # oldest first: a capped run can advance safely
        self.assertNotIn("fingerprint", params)
        self.assertEqual(grid.calls[1][1]["fingerprint"], "FP1")
        self.assertNotIn("TRON-PRO-API-KEY", headers)
        by_id = sorted(((t.tx_hash, t.log_index, t.amount_base_units, t.from_address) for t in got))
        self.assertEqual(by_id, sorted([
            (a, 0, 5_000_000, RECORDED_FROM),
            (b, 0, 7_000_000, RECORDED_FROM), (b, 1, 3_000_000, OTHER),  # sorted by (contract, from, amount)
            (c, 0, 9, RECORDED_FROM)]))
        self.assertTrue(all(t.final and t.to_address == MINE and t.contract == USDT and t.network == "tron"
                            for t in got))
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_NEW))
        self.assertIsNone(WatcherHealth.objects.get(name=f"scan:tron:tron:{self.acct.pk}").failing_since)

    def test_tron_first_scan_only_marks_the_start(self):
        grid = self.patch(FakeGrid([]))
        with mock.patch("apps.rails.chains.tron.time.time", return_value=1_800_000_000):
            self.assertEqual(adapter_for("tron").scan(self.acct, "tron"), [])
        self.assertEqual(grid.calls, [])
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, "1800000000000")

    def test_tron_cursor_unchanged_on_429(self):
        self.cursor(T_OLD)
        self.patch(FakeGrid([]))
        with mock.patch("apps.rails.http.get_json", side_effect=http.Unavailable("tron", "429")):
            with self.assertRaises(http.Unavailable):
                adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_OLD))
        self.assertEqual(WatcherHealth.objects.get().last_error_kind, "429")

    def test_tron_second_page_failing_moves_nothing(self):
        self.cursor(T_OLD)

        def grid(url, **kw):
            if "fingerprint" in kw["params"]:
                raise http.Unavailable("tron", "timeout")
            return page([row("ab" * 32, T_NEW, 5)], fingerprint="FP")

        with mock.patch("apps.rails.http.get_json", side_effect=grid):
            with self.assertRaises(http.Unavailable):
                adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_OLD))

    def test_tron_refused_and_garbage_become_unavailable(self):
        self.cursor(T_OLD)
        with mock.patch("apps.rails.http.get_json", side_effect=http.Refused("tron", 403)):
            with self.assertRaises(http.Unavailable) as cm:
                adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(cm.exception.kind, "403")
        bad = page([{"transaction_id": "ab" * 32, "block_timestamp": "x", "to": MINE}])
        with mock.patch("apps.rails.http.get_json", return_value=bad):
            with self.assertRaises(http.Unavailable) as cm:
                adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(cm.exception.kind, "bad_answer")
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_OLD))

    def test_tron_truncation_advances_to_newest_seen(self):
        self.cursor(T_OLD)
        pages = [page([row(f"{i:064x}", T_OLD + 1000 * (i + 1), 1)], fingerprint=f"FP{i}") for i in range(tron.MAX_PAGES)]
        grid = self.patch(FakeGrid(pages))
        got = adapter_for("tron").scan(self.acct, "tron")
        # The last row's transaction may go on past the cap: it waits in the cursor with the
        # continuation, to be numbered with the rest of it (audit MONEY-5).
        self.assertEqual((len(grid.calls), len(got)), (tron.MAX_PAGES, tron.MAX_PAGES - 1))
        c = WatchCursor.objects.get(stream=self.stream)
        self.assertEqual(c.position, str(T_OLD + 1000 * tron.MAX_PAGES))
        (held,) = c.extra["resume"][USDT]["held"]
        self.assertEqual(held["tx_hash"], f"{tron.MAX_PAGES - 1:064x}")

    def test_tron_approval_row_ignored(self):
        self.cursor(T_OLD)
        approval = row("ab" * 32, T_NEW, 5_000_000)
        approval["type"] = "Approval"
        self.patch(FakeGrid([page([approval, row("cd" * 32, T_NEW, 7)])]))
        (t,) = adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(t.tx_hash, "cd" * 32)

    def test_tron_float_amount_is_bad_answer(self):
        self.cursor(T_OLD)
        bad = row("ab" * 32, T_NEW, 1)
        bad["value"] = 5_000_000.0  # a JSON number with a fraction part, not the usual string
        self.patch(FakeGrid([page([bad])]))
        with self.assertRaises(http.Unavailable) as cm:
            adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(cm.exception.kind, "bad_answer")
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_OLD))

    def test_tron_garbage_timestamp_is_bad_answer(self):
        self.cursor(T_OLD)
        self.patch(FakeGrid([page([row("ab" * 32, 10**25, 5)])]))
        with self.assertRaises(http.Unavailable) as cm:
            adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(cm.exception.kind, "bad_answer")

    @override_settings(TRONGRID_API_KEY="k-123")
    def test_tron_api_key_header_sent(self):
        self.cursor(T_OLD)
        grid = self.patch(FakeGrid([page([])]))
        adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(grid.calls[0][2]["TRON-PRO-API-KEY"], "k-123")

    def test_tron_recorded_transfer_has_the_identity_of_its_events(self):
        recorded = json.loads((FIX / "trc20_list.json").read_text())
        acct = RailAccount.objects.create(org=self.org, kind="tron", label="r", address=RECORDED_TO,
                                          networks=["tron"], assets=["USDT"])
        WatchCursor.objects.create(network="tron", address=RECORDED_TO, stream=f"tron-trc20:{acct.pk}", position="1")
        events = json.loads((FIX / "events.json").read_text())
        with mock.patch("apps.rails.http.get_json", return_value=recorded):
            (scanned,) = adapter_for("tron").scan(acct, "tron")
        with mock.patch("apps.rails.http.get_json", return_value=events):
            (looked,) = adapter_for("tron").transfers_in_tx("tron", RECORDED_TX.upper())
        self.assertEqual((scanned.tx_hash, scanned.log_index), (RECORDED_TX, 0))
        self.assertEqual((looked.tx_hash, looked.log_index), (scanned.tx_hash, scanned.log_index))
        self.assertEqual(scanned.amount_base_units, looked.amount_base_units)


class TronEventsTests(Base):
    def test_tron_events_hex_addresses_converted(self):
        events = json.loads((FIX / "events.json").read_text())
        grid = self.patch(FakeGrid([events]))
        (t,) = adapter_for("tron").transfers_in_tx("tron", "0x" + RECORDED_TX)
        self.assertTrue(grid.calls[0][0].endswith(f"/v1/transactions/{RECORDED_TX}/events"))
        self.assertEqual(grid.calls[0][1], {"only_confirmed": "true"})
        self.assertEqual((t.from_address, t.to_address, t.contract), (RECORDED_FROM, RECORDED_TO, USDT))
        self.assertEqual((t.amount_base_units, t.final, t.tx_hash, t.log_index), (2_000_000, True, RECORDED_TX, 0))
        self.assertEqual(t.block_number, 77951958)

    def test_tron_events_ignore_other_contracts_and_numbers_per_recipient(self):
        events = json.loads((FIX / "events.json").read_text())
        e = events["data"][0]
        other = copy.deepcopy(e)
        other["contract_address"] = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"
        second = copy.deepcopy(e)
        second["result"]["value"] = "1"
        events["data"] = [other, e, second]
        self.patch(FakeGrid([events]))
        got = adapter_for("tron").transfers_in_tx("tron", RECORDED_TX)
        self.assertEqual(sorted((t.log_index, t.amount_base_units) for t in got), [(0, 1), (1, 2_000_000)])

    def test_tron_unknown_or_malformed_hash_is_empty(self):
        grid = self.patch(FakeGrid([{"data": [], "success": True, "meta": {}}]))
        self.assertEqual(adapter_for("tron").transfers_in_tx("tron", RECORDED_TX), [])
        self.assertEqual(adapter_for("tron").transfers_in_tx("tron", "../x?y"), [])
        self.assertEqual(len(grid.calls), 1)

    def test_tron_status_final_once_events_show_it(self):
        events = json.loads((FIX / "events.json").read_text())
        pay = mock.Mock(network="tron", tx_hash=RECORDED_TX, log_index=0)
        self.patch(FakeGrid([events, {"data": [], "success": True, "meta": {}}]))
        self.assertEqual(adapter_for("tron").status(pay), "final")
        self.assertEqual(adapter_for("tron").status(pay), "pending")
