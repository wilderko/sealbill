import base64
import copy
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.orgs.models import Organization
from apps.rails import http
from apps.rails.addresses import ton_user_friendly
from apps.rails.chains import ton
from apps.rails.chains.base import adapter_for
from apps.rails.models import RailAccount, WatchCursor, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin

FIX = Path(__file__).parent / "fixtures" / "ton"
MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
RECORDED_TO = "0:852443F8599FE6A5DA34FE43049AC4E0BEB3071BB2BFB56635EA9421287C283A"
MINE = "0:" + "ab" * 32
FIRST_HASH_B64 = "p8UxcqTejuVXjV+mEwfxpyVRLoD/k33zng0i4ELdjN0="
FIRST_HASH_HEX = base64.b64decode(FIRST_HASH_B64).hex()
NEWEST = 1793635051


def recorded():
    """The recorded answer, its recipient rewritten to the test rail's address."""
    return json.loads((FIX / "jetton_transfers.json").read_text().replace(RECORDED_TO, MINE.upper()))


class FakeCenter:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, *, params=None, headers=None, service, timeout=10):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@override_settings(TON_ENABLED=True)
class Base(UnsealedVaultMixin, TestCase):
    now = NEWEST + 100

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="lt", legal_name="LT s.r.o.")
        self.acct = RailAccount.objects.create(org=self.org, kind="ton", label="t", address=MINE,
                                               networks=["ton"], assets=["USDT"])
        self.stream = f"ton-jetton:{self.acct.pk}"
        clock = mock.patch("apps.rails.chains.ton.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def cursor(self, position):
        WatchCursor.objects.create(network="ton", address=MINE, stream=self.stream, position=str(position))

    def patch(self, *answers):
        center = FakeCenter(*answers)
        p = mock.patch("apps.rails.http.get_json", side_effect=center)
        p.start()
        self.addCleanup(p.stop)
        return center

    def scan(self):
        return adapter_for("ton").scan(self.acct, "ton")


class TonScanTests(Base):
    def test_ton_aborted_skipped_and_comment_kept(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        rows = data["jetton_transfers"]
        aborted = copy.deepcopy(rows[0])
        aborted["transaction_aborted"] = True
        aborted["transaction_hash"] = base64.b64encode(b"\x01" * 32).decode()
        rows.insert(0, aborted)
        rows[2]["decoded_forward_payload"] = None  # no comment on this one
        center = self.patch(data)
        got = {t.tx_hash: t for t in self.scan()}
        self.assertEqual(len(got), 3)
        self.assertNotIn("01" * 32, got)
        first = got[FIRST_HASH_HEX]
        self.assertEqual(first.comment, "100 Telegram Stars \n\nRef#nk66rqOPB")
        self.assertEqual((first.amount_base_units, first.to_address, first.contract, first.network),
                         (1_500_000, MINE, MASTER, "ton"))
        self.assertEqual(first.from_address, first.from_address.lower())
        self.assertEqual(first.log_index, 0)
        self.assertEqual(first.block_time.timestamp(), NEWEST)
        self.assertEqual(sorted(t.comment for t in got.values())[0], "")
        url, params, headers = center.calls[0]
        self.assertTrue(url.endswith("/jetton/transfers"))
        self.assertEqual(params, {"owner_address": MINE, "direction": "in", "jetton_master": MASTER,
                                  "start_utime": NEWEST - 1000 - 600, "sort": "asc", "limit": 100, "offset": 0})
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(NEWEST))
        self.assertIsNone(WatcherHealth.objects.get().failing_since)

    def test_ton_raw_and_friendly_addresses_compare_equal(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        row = data["jetton_transfers"][0]
        row["destination"] = ton_user_friendly(MINE)  # the same account, written the way a wallet shows it
        row["jetton_master"] = MASTER
        row["source"] = ton_user_friendly(row["source"].lower(), bounceable=True)
        del data["jetton_transfers"][1:]
        self.patch(data)
        (t,) = self.scan()
        self.assertEqual(t.to_address, MINE)
        self.assertTrue(t.from_address.startswith("0:") and t.from_address == t.from_address.lower())
        self.assertEqual(ton.canonical_hash(FIRST_HASH_B64), FIRST_HASH_HEX)
        self.assertEqual(ton.canonical_hash(FIRST_HASH_HEX.upper()), FIRST_HASH_HEX)
        self.assertEqual(ton.canonical_hash(FIRST_HASH_B64.replace("+", "-").replace("/", "_")), FIRST_HASH_HEX)
        self.assertIsNone(ton.canonical_hash("nonsense"))
        # a transfer to a different account is not ours
        data["jetton_transfers"][0]["destination"] = "0:" + "cd" * 32
        self.patch(data)
        self.assertEqual(self.scan(), [])

    def test_ton_young_transfer_not_final(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        del data["jetton_transfers"][1:]
        self.patch(data)
        self.now = NEWEST + 29
        (young,) = self.scan()
        self.assertFalse(young.final)
        self.now = NEWEST + 30
        (old,) = self.scan()  # the overlap re-read reports it again, same identity
        self.assertTrue(old.final)
        self.assertEqual((old.tx_hash, old.log_index), (young.tx_hash, young.log_index))

    def test_ton_same_transaction_transfers_are_numbered_deterministically(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        a = data["jetton_transfers"][0]
        b = copy.deepcopy(a)
        b["amount"] = "9"
        data["jetton_transfers"] = [b, a]
        self.patch(data)
        got = sorted((t.log_index, t.amount_base_units) for t in self.scan())
        self.assertEqual(got, [(0, 9), (1, 1_500_000)])

    def test_ton_paged_and_cursor_untouched_when_a_page_fails(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        row = data["jetton_transfers"][0]
        full = {"jetton_transfers": [copy.deepcopy(row) for _ in range(100)]}
        for i, r in enumerate(full["jetton_transfers"]):
            r["transaction_hash"] = base64.b64encode(bytes([i]) * 32).decode()
        center = self.patch(full, {"jetton_transfers": []})
        self.assertEqual(len(self.scan()), 100)
        self.assertEqual([c[1]["offset"] for c in center.calls], [0, 100])
        before = WatchCursor.objects.get(stream=self.stream).position
        self.patch(full, http.Unavailable("ton", "429"))
        with self.assertRaises(http.Unavailable):
            self.scan()
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, before)
        self.assertEqual(WatcherHealth.objects.get().last_error_kind, "429")

    def test_ton_first_scan_marks_the_start_and_garbage_is_unavailable(self):
        center = self.patch({})
        self.assertEqual(self.scan(), [])
        self.assertEqual(center.calls, [])
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(self.now))
        for bad in ({"jetton_transfers": [{"destination": MINE}]}, {"error": "x"}, {"jetton_transfers": "no"}):
            self.patch(bad)
            with self.assertRaises(http.Unavailable) as cm:
                self.scan()
            self.assertEqual(cm.exception.kind, "bad_answer")
        self.patch(http.Refused("ton", 401))
        with self.assertRaises(http.Unavailable) as cm:
            self.scan()
        self.assertEqual(cm.exception.kind, "401")

    def test_ton_foreign_master_ignored(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        del data["jetton_transfers"][1:]
        foreign = copy.deepcopy(data["jetton_transfers"][0])
        foreign["jetton_master"] = "0:" + "11" * 32
        foreign["transaction_hash"] = base64.b64encode(b"\x02" * 32).decode()
        data["jetton_transfers"].insert(0, foreign)
        self.patch(data)
        (t,) = self.scan()
        self.assertEqual((t.tx_hash, t.contract), (FIRST_HASH_HEX, MASTER))

    def test_ton_missing_aborted_flag_fails_closed(self):
        self.cursor(NEWEST - 1000)
        data = recorded()
        del data["jetton_transfers"][1:]
        del data["jetton_transfers"][0]["transaction_aborted"]
        self.patch(data)
        self.assertEqual(self.scan(), [])

    def test_ton_odd_sender_is_tolerated(self):
        for source in (None, "garbage", 5):
            self.cursor(NEWEST - 1000)
            data = recorded()
            del data["jetton_transfers"][1:]
            data["jetton_transfers"][0]["source"] = source
            self.patch(data)
            (t,) = self.scan()
            self.assertEqual((t.from_address, t.amount_base_units), ("", 1_500_000))
            WatchCursor.objects.all().delete()

    def test_ton_float_or_garbage_numbers_are_bad_answers(self):
        for field, value in (("amount", 1.5), ("transaction_now", 1793635051.5), ("transaction_now", 10**25),
                             ("amount", None)):
            self.cursor(NEWEST - 1000)
            data = recorded()
            del data["jetton_transfers"][1:]
            data["jetton_transfers"][0][field] = value
            self.patch(data)
            with self.assertRaises(http.Unavailable) as cm:
                self.scan()
            self.assertEqual(cm.exception.kind, "bad_answer", (field, value))
            WatchCursor.objects.all().delete()

    @override_settings(TONCENTER_API_KEY="k-1")
    def test_ton_api_key_header_sent(self):
        self.cursor(NEWEST)
        center = self.patch({"jetton_transfers": []})
        self.scan()
        self.assertEqual(center.calls[0][2]["X-API-Key"], "k-1")


class TonLookupTests(Base):
    def test_ton_transfers_in_tx_finds_the_hash_in_the_moment_of_the_transaction(self):
        tx_answer = {"transactions": [{"hash": FIRST_HASH_B64, "now": NEWEST}]}
        center = self.patch(tx_answer, recorded())
        for given in (FIRST_HASH_B64, FIRST_HASH_HEX.upper()):
            center.answers[:] = [tx_answer, recorded()]
            (t,) = adapter_for("ton").transfers_in_tx("ton", given)
            self.assertEqual((t.tx_hash, t.log_index, t.to_address, t.comment[:3]), (FIRST_HASH_HEX, 0, MINE, "100"))
        url, params, _ = center.calls[0]
        self.assertTrue(url.endswith("/transactions"))
        self.assertEqual(params["hash"], FIRST_HASH_HEX)
        self.assertEqual((center.calls[1][1]["start_utime"], center.calls[1][1]["end_utime"]), (NEWEST - 2, NEWEST + 2))

    def test_ton_lookup_has_the_identity_of_the_scan(self):
        self.cursor(NEWEST - 1000)
        self.patch(recorded())
        scanned = {(t.tx_hash, t.log_index) for t in self.scan()}
        self.patch({"transactions": [{"now": NEWEST}]}, recorded())
        (looked,) = adapter_for("ton").transfers_in_tx("ton", FIRST_HASH_HEX)
        self.assertIn((looked.tx_hash, looked.log_index), scanned)

    def test_ton_unknown_hash_is_empty_and_malformed_never_asks(self):
        center = self.patch({"transactions": []})
        self.assertEqual(adapter_for("ton").transfers_in_tx("ton", FIRST_HASH_HEX), [])
        self.assertEqual(adapter_for("ton").transfers_in_tx("ton", "../x"), [])
        self.assertEqual(len(center.calls), 1)

    def test_ton_lookup_running_out_of_pages_is_unavailable_never_partial(self):
        row = recorded()["jetton_transfers"][0]
        row["transaction_hash"] = base64.b64encode(b"\x07" * 32).decode()  # not the one asked for
        full = {"jetton_transfers": [row] * ton.PAGE_LIMIT}
        center = self.patch({"transactions": [{"now": NEWEST}]}, full)
        with self.assertRaises(http.Unavailable) as cm:
            adapter_for("ton").transfers_in_tx("ton", FIRST_HASH_HEX)
        self.assertEqual(cm.exception.kind, "backlog")
        self.assertEqual(len(center.calls), 1 + ton.MAX_PAGES)

    def test_ton_unpadded_base64_hash_is_the_same_hash(self):
        self.assertEqual(ton.canonical_hash(FIRST_HASH_B64.rstrip("=")), FIRST_HASH_HEX)

    def test_ton_status(self):
        pay = mock.Mock(network="ton", tx_hash=FIRST_HASH_HEX, log_index=0)
        self.patch({"transactions": [{"now": NEWEST}]}, recorded())
        self.now = NEWEST + 100
        self.assertEqual(adapter_for("ton").status(pay), "final")
        self.patch({"transactions": []})
        self.assertEqual(adapter_for("ton").status(pay), "dropped")


class TonGateTests(UnsealedVaultMixin, TestCase):
    """TON waits for the credit leg to be verified (project 2): until `TON_ENABLED`, no active
    TON account, no scan, no quote, and the pay page does not offer it (final review D2)."""

    def setUp(self):
        super().setUp()
        from apps.contacts.services import find_or_create_contact
        from apps.invoices.services import create_invoice, send_invoice

        self.org = Organization.objects.create(slug="lt", legal_name="LT s.r.o.")
        with override_settings(TON_ENABLED=True):  # made while it was enabled
            self.acct = RailAccount.objects.create(org=self.org, kind="ton", label="t", address=MINE,
                                                   networks=["ton"], assets=["USDT"])
        contact, _ = find_or_create_contact(self.org, "a@example.com", legal_name="Ján")
        inv = create_invoice(self.org, contact, currency="EUR", offered_methods=[self.acct.method_key],
                             lines=[{"name": "S", "quantity": 1, "unit_price_minor": 10000}])[0]
        self.invoice = send_invoice(inv, email_client=False)[0]

    def test_off_by_default(self):
        from django.conf import settings

        self.assertIs(settings.TON_ENABLED, False)

    def test_an_active_ton_account_cannot_be_saved(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as cm:
            RailAccount.objects.create(org=self.org, kind="ton", label="new", address=MINE, networks=["ton"],
                                       assets=["USDT"])
        self.assertIn("TON_ENABLED", str(cm.exception))
        self.assertIn("credit", str(cm.exception))
        with self.assertRaises(ValidationError) as cm:
            self.acct.save()  # the one made while it was enabled cannot be saved active either
        self.assertIn("active", cm.exception.message_dict)
        draft = RailAccount(org=self.org, kind="ton", label="form", address=MINE, networks=["ton"], assets=["USDT"])
        with self.assertRaises(ValidationError) as cm:
            draft.full_clean()  # the admin form shows it on the field
        self.assertIn("active", cm.exception.message_dict)
        inactive = RailAccount.objects.create(org=self.org, kind="ton", label="later", address=MINE,
                                              networks=["ton"], assets=["USDT"], active=False)
        inactive.full_clean()
        self.acct.active = False
        self.acct.save()

    def test_chain_scan_skips_ton(self):
        from apps.rails import jobs
        from apps.rails.chains import base as chains

        scanned = mock.Mock(return_value=[])
        with chains.override_adapter("ton", mock.Mock(scan=scanned)):
            jobs.chain_scan()
            scanned.assert_not_called()
            with override_settings(TON_ENABLED=True):
                jobs.chain_scan()
        scanned.assert_called_once()

    def test_quotes_on_ton_are_refused(self):
        from apps.quotes.errors import QuoteError
        from apps.quotes.services import create_quote

        with mock.patch("apps.rails.fx.price_in", side_effect=AssertionError("no rate is asked")):
            with self.assertRaises(QuoteError) as cm:
                create_quote(self.invoice, self.acct, "ton", "USDT")
        self.assertEqual(cm.exception.code, "rail_unavailable")

    def test_the_pay_page_does_not_offer_ton(self):
        from apps.portal import crypto

        self.assertEqual(crypto.options(self.invoice), [])
        self.assertEqual(crypto.offered_networks(self.invoice), [])
        self.assertEqual(crypto.account_pairs(self.acct), [])
        with override_settings(TON_ENABLED=True):
            self.assertEqual([(o["network"], o["asset"]) for o in crypto.options(self.invoice)], [("ton", "USDT")])

    def test_a_ton_declaration_is_not_looked_up(self):
        from apps.payments import matching
        from apps.payments.services import declare_payment

        declaration = declare_payment(self.invoice, network="ton", tx_hash=FIRST_HASH_HEX)
        with mock.patch("apps.rails.http.get_json", side_effect=AssertionError("toncenter is not asked")):
            self.assertIsNone(matching.verify_declaration(declaration))
