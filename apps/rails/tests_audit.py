"""Audit 2026-11-03, watchers: E2E-2, E2E-4 with MONEY-10a, MONEY-5 and MONEY-8.

- E2E-2: the first look at an account reads back to its earliest unreleased quote, so a payment
  made before the watcher ever looked is found (native ETH and Solana used to baseline a state
  that already held it).
- E2E-4 / MONEY-10a: an account switched off is still watched, and its declarations still looked
  up, while a quote of it is unreleased; meanwhile its address, networks, assets, kind and
  organization cannot change.
- MONEY-5: spam can no longer pin a cursor. Tron and TON continue a capped run from the last row
  they read, and say "backlog" meanwhile; EVM splits a refused range per contract and per block,
  reads a block no node will list from its receipts, and records a gap when even that fails.
- MONEY-8: a missing receipt is "not yet known"; a payment is dropped only from a final block; the
  log is read again at finality; an EVM transfer is identified by its place in its transaction;
  a native balance that rose with no visible transaction is for a person to check.

`TronPinnedCursorTests` and `PinnedLogScanTests` are the audit's PoCs (`poc_money_5.py`,
`poc_money_5b.py`), moved here with their assertions unchanged.
"""
import base64
import copy
import datetime
import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase, override_settings

from apps.contacts.services import find_or_create_contact
from apps.invoices.services import create_invoice, send_invoice
from apps.orgs.models import BankAccount, Organization
from apps.orgs.testing import add_bank
from apps.payments import matching
from apps.payments.models import Declaration, Payment
from apps.payments.tests_matching import EVM, K0, TX, MatchingBase, transfer
from apps.quotes.models import Quote
from apps.rails import http, jobs
from apps.rails.chains import base as chains
from apps.rails.chains import evm, tron
from apps.rails.chains.base import adapter_for
from apps.rails.models import RailAccount, ScanGap, WatchCursor, WatcherHealth
from apps.rails.networks import NETWORKS
from apps.rails.tests_evm import MINE, USDC_ARB, Base as EvmBase, my_log, rpc_error
from apps.rails.tests_jobs import ScriptedAdapter
from apps.rails.tests_solana import SIG, TOKEN_ACCOUNT, Base as SolanaBase, load
from apps.rails.tests_ton import NEWEST, Base as TonBase, recorded
from apps.rails.tests_tron import T_OLD, USDT, Base as TronBase, FakeGrid, page, row

USDT_ARB = NETWORKS["arbitrum"].assets["USDT"].contract.lower()
USDC_ETH = NETWORKS["ethereum"].assets["USDC"].contract.lower()
SKEW = datetime.timedelta(seconds=matching.CLOCK_SKEW_S)


def at(unix: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(unix, tz=datetime.timezone.utc)


class QuoteMixin:
    """Quotes on a rail account, each for an invoice of its own, written straight to the table."""

    _n = 0

    def quote_on(self, account, network, asset, issued, *, state="open", amount=None):
        org = account.org
        if not org.address:
            Organization.objects.filter(pk=org.pk).update(address="Main 1")
            org.refresh_from_db()
        if not BankAccount.objects.filter(org=org).exists():
            add_bank(org)
        QuoteMixin._n += 1
        contact, _ = find_or_create_contact(org, f"client{QuoteMixin._n}@example.com", legal_name="Client",
                                            preferred_language="en")
        invoice = create_invoice(org, contact, currency="EUR", title="Service",
                                 lines=[{"name": "Service", "quantity": 1, "unit_price_minor": 10000}])[0]
        invoice = send_invoice(invoice, email_client=False)[0]
        decimals = NETWORKS[network].assets[asset].decimals
        amount = amount or (108 * 10 ** decimals + QuoteMixin._n * 10 ** (decimals - 2))
        closed = None if state == "open" else issued + datetime.timedelta(seconds=1800)
        return Quote.objects.create(
            invoice=invoice, rail_account=account, network=network, asset=asset, address=account.address,
            amount_base_units=str(amount), min_accept_base_units=str(amount), ceiling_base_units=str(amount),
            invoice_amount_minor=10000, issued_at=issued, expires_at=issued + datetime.timedelta(seconds=1800),
            state=state, closed_at=closed)


def superuser_browser(test):
    """A superuser signed in with password and TOTP: the Django admin is open to them."""
    from apps.staff.models import StaffTOTPDevice

    from apps.staff.tests import token_for

    user = get_user_model().objects.create_user("root", password="pw-root-1", is_staff=True, is_superuser=True)
    device = StaffTOTPDevice.objects.create(user=user, name="t", confirmed=True)
    browser = Client()
    browser.force_login(user)
    test.assertEqual(browser.post("/verify/", {"token": token_for(device)}).status_code, 302)
    return browser


# ---------------------------------------------------------------------------
# E2E-2: the first look reads back to the earliest unreleased quote
# ---------------------------------------------------------------------------
class EvmFirstLookTests(QuoteMixin, EvmBase):
    def setUp(self):
        super().setUp()
        self.chain.block_s = 1  # block n was made 1000 - n seconds before the head

    def test_the_first_log_scan_reads_back_to_the_earliest_unreleased_quote(self):
        acct = self.account(assets=("USDC",))
        self.quote_on(acct, "arbitrum", "USDC", at(self.chain.ts - 300))
        self.chain.logs = [my_log(n=750, amount=5_000_000)]  # paid 250 s ago, before the watcher ever looked
        (t,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual((t.block_number, t.amount_base_units), (750, 5_000_000))
        self.assertLessEqual(self.chain.log_ranges[0][0], 1000 - 300 - matching.CLOCK_SKEW_S)
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "1000")

    def test_the_first_native_scan_walks_back_instead_of_baselining_the_payment(self):
        acct = self.account(assets=("ETH",))
        self.quote_on(acct, "arbitrum", "ETH", at(self.chain.ts - 60))
        h = "0x" + "c1" * 32
        self.chain.blocks[990] = {"number": hex(990), "timestamp": hex(self.chain.ts - 10), "transactions": [
            {"hash": h, "to": MINE, "from": "0x" + "33" * 20, "value": hex(10**15), "input": "0x"}]}
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(990), "logs": []}
        self.chain.balance = 10**15  # the balance the first look sees already holds the payment
        (t,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual((t.tx_hash, t.amount_base_units, t.log_index), (h, 10**15, -1))
        c = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual((c.position, c.extra), ("1000", {"balance": str(10**15), "nonce": 0}))
        self.assertFalse(ScanGap.objects.exists())

    def test_only_released_quotes_leave_the_first_look_at_the_head(self):
        acct = self.account(assets=("USDC", "ETH"))
        self.quote_on(acct, "arbitrum", "USDC", at(self.chain.ts - 300), state="released")
        self.chain.logs = [my_log(n=750)]
        self.assertEqual(evm.EvmAdapter().scan(acct, "arbitrum"), [])
        self.assertNotIn("eth_getLogs", [m for _, m in self.chain.calls])
        self.assertEqual(sorted(WatchCursor.objects.values_list("position", flat=True)), ["1000", "1000"])


class TronFirstLookTests(QuoteMixin, TronBase):
    def test_the_first_look_reads_from_the_earliest_unreleased_quote(self):
        self.quote_on(self.acct, "tron", "USDT", at(T_OLD / 1000))
        grid = self.patch(FakeGrid([page([row("ab" * 32, T_OLD + 30_000, 5_000_000)])]))
        with mock.patch("apps.rails.chains.tron.time.time", return_value=(T_OLD + 900_000) / 1000):
            (t,) = adapter_for("tron").scan(self.acct, "tron")  # fifteen minutes later
        self.assertEqual(t.amount_base_units, 5_000_000)
        self.assertEqual(grid.calls[0][1]["min_timestamp"], T_OLD - matching.CLOCK_SKEW_S * 1000)
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).position, str(T_OLD + 30_000))


class TonFirstLookTests(QuoteMixin, TonBase):
    def test_the_first_look_reads_from_the_earliest_unreleased_quote(self):
        self.quote_on(self.acct, "ton", "USDT", at(NEWEST - 3600))
        center = self.patch(recorded())
        got = self.scan()
        self.assertTrue(got)
        self.assertEqual(center.calls[0][1]["start_utime"], NEWEST - 3600 - matching.CLOCK_SKEW_S)


class SolanaFirstLookTests(QuoteMixin, SolanaBase):
    def test_the_first_look_reads_back_to_the_earliest_unreleased_quote_and_no_further(self):
        listed = load("signatures.json")["result"]
        paid_at = listed[0]["blockTime"]
        self.quote_on(self.acct, "solana", "USDC", at(paid_at - 60))
        older = [{"signature": "older", "err": None, "blockTime": paid_at - 3600},
                 {"signature": "oldest", "err": None, "blockTime": paid_at - 7200}]
        self.rpc.signatures[TOKEN_ACCOUNT] = listed + older
        (t,) = self.scan()
        self.assertEqual((t.tx_hash, t.amount_base_units), (SIG, 2_700_000))
        reads = [p[0] for _, m, p in self.rpc.calls if m == "getTransaction"]
        self.assertEqual(reads, [SIG])  # the history before the quote is not read
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: SIG})
        self.assertEqual(self.scan(), [])  # and the next look starts after it

    def test_a_first_look_finding_nothing_after_the_quote_remembers_where_it_stands(self):
        listed = load("signatures.json")["result"]
        self.quote_on(self.acct, "solana", "USDC", at(listed[0]["blockTime"] + 600))
        self.assertEqual(self.scan(), [])
        self.assertNotIn("getTransaction", self.rpc.methods())
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: SIG})


# ---------------------------------------------------------------------------
# E2E-4: an account switched off is watched while a quote of it is unreleased
# ---------------------------------------------------------------------------
class RetiredAccountTests(MatchingBase):
    def test_an_inactive_account_is_scanned_until_its_last_quote_is_released(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(active=False)
        adapter = ScriptedAdapter({(self.acct.pk, "arbitrum"): [transfer(K0)]})
        with chains.override_adapter("evm", adapter):
            jobs.chain_scan()
        self.assertEqual(adapter.calls, [(self.acct.pk, "arbitrum")])
        (p,) = Payment.objects.all()
        self.assertEqual((p.state, p.invoice_id), ("confirmed", self.invoice.pk))
        Quote.objects.filter(rail_account=self.acct).update(state="released")
        with chains.override_adapter("evm", adapter):
            jobs.chain_scan()
        self.assertEqual(adapter.calls, [(self.acct.pk, "arbitrum")])  # nothing left to watch for

    def test_a_declaration_finds_a_transfer_to_an_inactive_account_with_an_unreleased_quote(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(active=False)
        self.adapter.txs[TX] = [transfer(K0)]
        declaration = Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash=TX)
        payment = matching.verify_declaration(declaration)
        self.assertIsNotNone(payment)
        self.assertEqual((payment.rail_account_id, payment.invoice_id), (self.acct.pk, self.invoice.pk))
        Quote.objects.filter(rail_account=self.acct).update(state="released")
        Payment.objects.all().delete()
        again = Declaration.objects.create(invoice=self.invoice, network="arbitrum", tx_hash=TX)
        self.assertIsNone(matching.verify_declaration(again))  # retired and nothing held: not ours to look at


class LockedWhileQuotedTests(MatchingBase):
    """MONEY-10a: what the watcher reads cannot change while a client may still pay a quote."""

    def changes(self):
        other = Organization.objects.create(slug="other", legal_name="Other s.r.o.")
        return {"address": "0x" + "ee" * 20, "networks": ["arbitrum", "base"], "assets": ["USDC"],
                "org": other, "kind": "tron"}

    def test_clean_refuses_what_the_watcher_reads_while_a_quote_is_unreleased(self):
        for field, value in self.changes().items():
            with self.subTest(field=field):
                account = RailAccount.objects.get(pk=self.acct.pk)
                setattr(account, field, value)
                with self.assertRaises(ValidationError) as cm:
                    account.full_clean()
                self.assertIn(field, cm.exception.message_dict)
        account = RailAccount.objects.get(pk=self.acct.pk)
        account.active, account.label = False, "retired"
        account.full_clean()  # switching it off and renaming it stay allowed

    def test_once_its_quotes_are_released_the_account_may_change(self):
        Quote.objects.filter(rail_account=self.acct).update(state="released")
        account = RailAccount.objects.get(pk=self.acct.pk)
        account.address, account.networks = "0x" + "ee" * 20, ["arbitrum", "base"]
        account.full_clean()

    def test_the_admin_shows_the_refusal_and_keeps_the_account(self):
        browser = superuser_browser(self)
        form = {"org": self.org.pk, "kind": "evm", "label": "EVM", "address": "0x" + "ee" * 20,
                "networks": json.dumps(["arbitrum"]), "assets": json.dumps(["USDC", "ETH"]), "config": "{}",
                "active": "on", "_save": "Save"}
        r = browser.post(f"/hidden-admin-xyz/rails/railaccount/{self.acct.pk}/change/", form)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "a client may still pay")
        self.assertEqual(RailAccount.objects.get(pk=self.acct.pk).address, EVM)
        r = browser.post(f"/hidden-admin-xyz/rails/railaccount/{self.acct.pk}/change/",
                         {**form, "address": EVM, "networks": json.dumps(["arbitrum", "base"])})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RailAccount.objects.get(pk=self.acct.pk).networks, ["arbitrum"])
        retire = {k: v for k, v in form.items() if k != "active"}
        r = browser.post(f"/hidden-admin-xyz/rails/railaccount/{self.acct.pk}/change/", {**retire, "address": EVM})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(RailAccount.objects.get(pk=self.acct.pk).active)


# ---------------------------------------------------------------------------
# MONEY-5: spam cannot pin a cursor
# ---------------------------------------------------------------------------
class TronPinnedCursorTests(TronBase):
    """poc_money_5.py: more than MAX_PAGES * PAGE_LIMIT (5000) TronGrid rows inside the 600 s
    overlap window before the cursor pinned the Tron watcher for good."""

    def test_zero_value_spam_in_the_overlap_pins_the_cursor_and_hides_a_payment(self):
        self.cursor(T_OLD)
        real = row("ab" * 32, T_OLD + 60_000, 1_560_020_000)  # a client's payment one minute after the cursor
        spam = [row(f"{i:064x}", T_OLD - 300_000 + i, 0) for i in range(tron.PAGE_LIMIT * tron.MAX_PAGES)]

        def grid(url, *, params=None, headers=None, service, timeout=10):
            # TronGrid, oldest first: the spam (inside the overlap) and then the payment.
            rows = spam + [real]
            n = int((params or {}).get("fingerprint") or 0)
            chunk = rows[n * tron.PAGE_LIMIT:(n + 1) * tron.PAGE_LIMIT]
            more = (n + 1) * tron.PAGE_LIMIT < len(rows)
            return page(chunk, fingerprint=str(n + 1) if more else None)

        seen = []
        with mock.patch("apps.rails.http.get_json", side_effect=grid):
            for _ in range(3):  # three minutes of chain_scan
                seen += adapter_for("tron").scan(self.acct, "tron")
        health = WatcherHealth.objects.get(name=f"scan:tron:tron:{self.acct.pk}")
        position = WatchCursor.objects.get(stream=self.stream).position
        self.assertTrue(
            any(t.amount_base_units == 1_560_020_000 for t in seen),
            f"payment never reported; cursor still {position} (was {T_OLD}); "
            f"health failing_since={health.failing_since}")


class TronBacklogTests(TronBase):
    def health(self):
        return WatcherHealth.objects.get(name=f"scan:tron:tron:{self.acct.pk}")

    def capped_pages(self, last_rows, n=tron.MAX_PAGES):
        pages = [page([row(f"{i + 1:064x}", T_OLD + 1000 * (i + 1), 1)], fingerprint=f"F{i}") for i in range(n - 1)]
        return pages + [page(last_rows, fingerprint=f"F{n - 1}")]

    def test_a_capped_run_is_a_backlog_never_ok_and_the_next_run_continues_where_it_stopped(self):
        self.cursor(T_OLD)
        grid = self.patch(FakeGrid(self.capped_pages([row("cd" * 32, T_OLD + 50_000, 7)])
                                   + [page([row("ef" * 32, T_OLD + 60_000, 9)])]))
        adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual((self.health().last_error_kind, bool(self.health().failing_since)), ("backlog", True))
        (t,) = [t for t in adapter_for("tron").scan(self.acct, "tron") if t.amount_base_units == 9]
        resumed = grid.calls[tron.MAX_PAGES][1]
        self.assertEqual(resumed["fingerprint"], f"F{tron.MAX_PAGES - 1}")
        self.assertEqual(resumed["min_timestamp"], grid.calls[0][1]["min_timestamp"])  # the same query, continued
        self.assertEqual((self.health().last_error_kind, self.health().failing_since), ("", None))
        self.assertEqual(WatchCursor.objects.get(stream=self.stream).extra, {})

    def test_a_transaction_cut_by_the_cap_is_numbered_whole(self):
        self.cursor(T_OLD)
        cut = "cd" * 32
        self.patch(FakeGrid(self.capped_pages([row(cut, T_OLD + 50_000, 1)])
                            + [page([row(cut, T_OLD + 50_000, 2), row("ef" * 32, T_OLD + 60_000, 3)])]))
        first = adapter_for("tron").scan(self.acct, "tron")
        self.assertNotIn(cut, [t.tx_hash for t in first])  # held until the rest of its transaction is read
        second = adapter_for("tron").scan(self.acct, "tron")
        self.assertEqual(sorted((t.tx_hash, t.log_index, t.amount_base_units) for t in second),
                         [(cut, 0, 1), (cut, 1, 2), ("ef" * 32, 0, 3)])

    def test_a_continuation_trongrid_no_longer_accepts_is_dropped_and_the_next_run_starts_afresh(self):
        self.cursor(T_OLD)
        self.patch(FakeGrid(self.capped_pages([row("cd" * 32, T_OLD + 50_000, 7)])))
        adapter_for("tron").scan(self.acct, "tron")
        position = WatchCursor.objects.get(stream=self.stream).position
        with mock.patch("apps.rails.http.get_json", side_effect=http.Refused("tron", 400)):
            with self.assertRaises(http.Unavailable):
                adapter_for("tron").scan(self.acct, "tron")
        c = WatchCursor.objects.get(stream=self.stream)
        self.assertEqual((c.position, c.extra), (position, {"since": {USDT: T_OLD + 50_000}}))  # its own last read
        grid = self.patch(FakeGrid([page([row("cd" * 32, T_OLD + 50_000, 7)])]))
        (t,) = adapter_for("tron").scan(self.acct, "tron")  # read again from the cursor's overlap
        self.assertEqual(t.amount_base_units, 7)
        self.assertNotIn("fingerprint", grid.calls[0][1])
        self.assertEqual(grid.calls[0][1]["min_timestamp"], int(position) - tron.OVERLAP_MS)


class TonBacklogTests(TonBase):
    def rows(self, n, start=0):
        base = recorded()["jetton_transfers"][0]
        out = []
        for i in range(start, start + n):
            r = copy.deepcopy(base)
            r["transaction_hash"] = base64.b64encode(i.to_bytes(32, "big")).decode()
            out.append(r)
        return out

    def test_a_capped_run_continues_from_the_last_row_it_read(self):
        from apps.rails.chains import ton

        self.cursor(NEWEST - 1000)
        full = [{"jetton_transfers": self.rows(ton.PAGE_LIMIT, p * ton.PAGE_LIMIT)} for p in range(ton.MAX_PAGES)]
        paid = copy.deepcopy(recorded()["jetton_transfers"][0])
        paid["transaction_hash"] = base64.b64encode(b"\xee" * 32).decode()
        paid["amount"] = "7000000"
        center = self.patch(*full, {"jetton_transfers": [paid]})
        self.scan()
        h = WatcherHealth.objects.get(name=f"scan:ton:ton:{self.acct.pk}")
        self.assertEqual((h.last_error_kind, bool(h.failing_since)), ("backlog", True))
        got = self.scan()
        self.assertIn(7_000_000, [t.amount_base_units for t in got])
        resumed = center.calls[ton.MAX_PAGES][1]  # one row back, through the hash read last
        self.assertEqual((resumed["offset"], resumed["start_utime"]),
                         (ton.MAX_PAGES * ton.PAGE_LIMIT - 1, center.calls[0][1]["start_utime"]))
        h.refresh_from_db()
        self.assertIsNone(h.failing_since)


class PinnedLogScanTests(EvmBase):
    """poc_money_5b.py: a block range holding more Transfer logs to our address than the node
    will return was refused at every chunk size down to MIN_LOG_CHUNK, and the cursor never
    moved past it."""

    def test_a_spam_block_pins_the_log_cursor(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 400)
        spam_block = 450

        def too_many(url, params):
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            if lo <= spam_block <= hi:  # 20 000 zero-value Transfer logs in that block
                return {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": -32005, "message": "query returned more than 10000 results"}}
            return None

        self.chain.errors["eth_getLogs"] = too_many
        self.chain.logs.append(my_log(n=900, amount=1_560_020_000))  # a client's payment, later
        reported = []
        for _ in range(5):
            try:
                reported += adapter_for("arbitrum").scan(acct, "arbitrum")
            except http.Unavailable:
                pass
        position = WatchCursor.objects.get(stream=f"evm-logs:{acct.pk}").position
        self.assertTrue(any(t.amount_base_units == 1_560_020_000 for t in reported),
                        f"the payment at block 900 was never reported; log cursor still {position}")


class EvmRefusedRangeTests(EvmBase):
    SPAM = 450

    def refuse(self, contract=None):
        def too_many(url, params):
            flt = params[0]
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            if lo <= self.SPAM <= hi and (contract is None or contract in flt["address"]):
                return rpc_error("query returned more than 10000 results")(url, params)
            return None
        self.chain.errors["eth_getLogs"] = too_many

    def test_a_block_no_node_will_list_is_read_from_its_receipts(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 400)
        self.refuse()
        h = "0x" + "a5" * 32
        self.chain.logs = [my_log(n=self.SPAM, idx=40, amount=7_000_000, tx=h)]
        (t,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual((t.tx_hash, t.log_index, t.block_number, t.amount_base_units), (h, 0, self.SPAM, 7_000_000))
        self.assertIn("eth_getBlockReceipts", [m for _, m in self.chain.calls])
        self.assertFalse(ScanGap.objects.exists())
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "1000")

    def test_one_spammed_contract_does_not_hide_the_other(self):
        acct = self.account(assets=("USDC", "USDT"))
        self.cursor("arbitrum", "evm-logs", 400)
        self.refuse(contract=USDC_ARB)
        self.chain.logs = [my_log(n=self.SPAM, amount=3_000_000, contract=USDT_ARB, tx="0x" + "a6" * 32)]
        (t,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual((t.contract, t.amount_base_units), (USDT_ARB, 3_000_000))
        usdt_alone = [c for c in self.chain.log_ranges if c[0] <= self.SPAM <= c[1]]
        self.assertTrue(usdt_alone)

    def test_a_block_whose_receipts_are_refused_too_is_a_gap_and_the_cursor_moves_on(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 400)
        self.refuse()
        self.chain.errors["eth_getBlockReceipts"] = rpc_error("response size exceeded")
        self.chain.logs = [my_log(n=900, amount=1_560_020_000)]
        with self.assertLogs("apps.rails.chains.evm", "WARNING") as logs:
            (t,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual(t.block_number, 900)
        gap = ScanGap.objects.get()
        self.assertEqual((gap.network, gap.address, gap.from_block, gap.to_block, gap.reason),
                         ("arbitrum", MINE, self.SPAM, self.SPAM, "unreadable"))
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "1000")
        self.assertIsNone(WatcherHealth.objects.get(name__startswith="scan:evm:arbitrum:").failing_since)
        self.assertNotIn(MINE, "".join(logs.output))
        with self.assertLogs("apps.rails.chains.evm", "WARNING"):
            evm.EvmAdapter().scan(acct, "arbitrum")  # the overlap reads the block again
        self.assertEqual(ScanGap.objects.count(), 1)  # one row for one block, however often it is refused

    def test_a_receipts_outage_is_could_not_look_never_a_gap(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 400)
        self.refuse()

        def down(url, params):
            raise http.Unavailable("evm:arbitrum", "timeout")

        self.chain.errors["eth_getBlockReceipts"] = down
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertFalse(ScanGap.objects.exists())
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "400")


# ---------------------------------------------------------------------------
# MONEY-8: EVM finality and the native scan
# ---------------------------------------------------------------------------
class EvmStatusTests(EvmBase):
    def payment(self, tx, *, block=995, log_index=0, contract=USDC_ETH, amount=1_000_000, to=MINE):
        return SimpleNamespace(network="ethereum", tx_hash=tx, contract=contract, amount_base_units=str(amount),
                               to_address=to, log_index=log_index, block_number=block)

    def test_a_missing_receipt_is_not_yet_known_until_its_block_is_final_without_it(self):
        a, h = evm.EvmAdapter(), "0x" + "a1" * 32
        p = self.payment(h)
        self.chain.finalized = 900
        self.assertEqual(a.status(p), "pending")  # a node behind, or a block not final: never "dropped"
        self.chain.finalized = 1000
        self.chain.blocks[995] = {"number": hex(995), "timestamp": hex(self.chain.ts), "transactions": [h]}
        with self.assertRaises(http.Unavailable) as cm:
            a.status(p)
        self.assertEqual(cm.exception.kind, "receipt_missing")  # the final block holds it: this node lacks it
        self.chain.blocks[995]["transactions"] = []
        self.assertEqual(a.status(p), "dropped")  # final, and the block no longer holds it

    def test_on_finality_the_log_is_read_again_and_must_still_match(self):
        a, h = evm.EvmAdapter(), "0x" + "a2" * 32
        log = my_log(n=995, amount=1_000_000, contract=USDC_ETH, tx=h)
        other = my_log(n=995, idx=0, amount=999_999, contract=USDC_ETH, tx=h)  # executed differently
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": [other]}
        p = self.payment(h)
        self.chain.finalized = 900
        self.assertEqual(a.status(p), "pending")
        self.chain.finalized = 995
        self.assertEqual(a.status(p), "dropped")
        self.chain.receipts[h]["logs"] = [other, log]
        self.assertEqual(a.status(self.payment(h, log_index=1)), "final")

    def test_a_reverted_receipt_is_dropped_only_from_a_final_block(self):
        a, h = evm.EvmAdapter(), "0x" + "a3" * 32
        self.chain.receipts[h] = {"status": "0x0", "blockNumber": hex(995), "logs": []}
        self.chain.finalized = 900
        self.assertEqual(a.status(self.payment(h)), "pending")
        self.chain.finalized = 995
        self.assertEqual(a.status(self.payment(h)), "dropped")

    def test_a_native_payment_is_checked_against_its_transaction_again(self):
        a, h = evm.EvmAdapter(), "0x" + "a4" * 32
        self.chain.finalized = 995
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": []}
        self.chain.txs[h] = {"hash": h, "to": MINE, "from": "0x" + "33" * 20, "value": hex(5), "input": "0x"}
        p = self.payment(h, contract="", log_index=-1, amount=5)
        self.assertEqual(a.status(p), "final")
        self.chain.txs[h]["value"] = hex(4)
        self.assertEqual(a.status(p), "dropped")
        self.chain.txs[h] = {**self.chain.txs[h], "value": hex(5), "to": "0x" + "44" * 20}
        self.assertEqual(a.status(p), "dropped")


class EvmIdentityTests(EvmBase):
    def test_a_reorg_moving_a_transfer_to_another_block_keeps_its_identity(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 990)
        h = "0x" + "ab" * 32
        self.chain.logs = [my_log(n=995, idx=7, tx=h)]
        (first,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.chain.logs = [my_log(n=997, idx=2, tx=h)]  # the same transaction, re-included elsewhere
        (again,) = evm.EvmAdapter().scan(acct, "arbitrum")
        self.assertEqual((first.tx_hash, first.log_index), (again.tx_hash, again.log_index))
        matching.ingest(first, account=acct)
        matching.ingest(again, account=acct)
        self.assertEqual(Payment.objects.count(), 1)  # one transfer, one row

    def test_scan_and_lookup_use_the_place_in_the_transaction(self):
        acct = self.account(assets=("USDC",))
        self.cursor("arbitrum", "evm-logs", 990)
        h = "0x" + "ac" * 32
        before = my_log(n=995, idx=7, contract="0x" + "99" * 20, tx=h)  # another token's log, first in the tx
        ours = my_log(n=995, idx=8, tx=h)
        self.chain.logs = [ours]
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": [before, ours]}
        self.chain.txs[h] = {"hash": h, "to": USDC_ARB, "value": "0x0", "from": "0x" + "22" * 20}
        (scanned,) = evm.EvmAdapter().scan(acct, "arbitrum")
        (looked,) = evm.EvmAdapter().transfers_in_tx("arbitrum", h)
        self.assertEqual((scanned.log_index, looked.log_index), (1, 1))


@override_settings(STAFF_NOTIFY_EMAILS=["ops@example.com"], SITE_URL="https://pay.example")
class NativeBalanceTests(EvmBase):
    def scan(self):
        return evm.EvmAdapter().scan(self.account(assets=("ETH",)), "arbitrum")

    def test_a_balance_that_rose_with_no_visible_transaction_is_for_a_person(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.balance = 10**15  # an internal transfer from a contract: no transaction to us
        with self.assertLogs("apps.rails.chains.evm", "WARNING") as logs:
            self.assertEqual(self.scan(), [])  # nothing is guessed
        gap = ScanGap.objects.get()
        self.assertEqual((gap.from_block, gap.to_block, gap.reason), (991, 1000, "unexplained_balance"))
        self.assertIsNone(WatcherHealth.objects.get(name__startswith="scan:evm:arbitrum:").failing_since)
        self.assertNotIn(MINE, "".join(logs.output))
        c = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual((c.position, c.extra["balance"]), ("1000", str(10**15)))
        jobs.watcher_alert()
        (letter,) = mail.outbox
        self.assertIn("balance rose with no visible transaction", letter.subject.lower())
        self.assertIn("991", letter.body)
        self.assertNotIn(MINE, letter.body)
        self.scan()
        self.assertEqual(ScanGap.objects.count(), 1)

    def test_a_rise_its_transactions_explain_is_not(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        h = "0x" + "c1" * 32
        self.chain.blocks[995] = {"number": hex(995), "timestamp": hex(self.chain.ts), "transactions": [
            {"hash": h, "to": MINE, "from": "0x" + "33" * 20, "value": hex(10**15), "input": "0x"}]}
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": []}
        self.chain.balance = 10**15
        (t,) = self.scan()
        self.assertEqual(t.amount_base_units, 10**15)
        self.assertFalse(ScanGap.objects.exists())

    def test_a_balance_that_fell_is_not(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "10", "nonce": 0})
        self.chain.balance, self.chain.nonce = 3, 1  # we sent something
        self.assertEqual(self.scan(), [])
        self.assertFalse(ScanGap.objects.exists())


class ScanGapMigrationTests(TransactionTestCase):
    """The reason column arrives on a database that already holds gaps: they were all skipped
    native blocks, the only kind there was."""

    BEFORE = ("rails", "0003_staff_alert_per_channel")

    def setUp(self):
        self.addCleanup(self.migrate_to_latest)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_gaps_are_skipped_blocks(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.BEFORE])
        old = executor.loader.project_state([self.BEFORE]).apps.get_model("rails", "ScanGap")
        pk = old.objects.create(network="arbitrum", address=MINE, from_block=11, to_block=8000).pk
        self.migrate_to_latest()
        gap = ScanGap.objects.get(pk=pk)
        self.assertEqual((gap.reason, gap.from_block, gap.to_block), ("skipped", 11, 8000))


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------
class NativeFirstLookCatchUpTests(QuoteMixin, EvmBase):
    """A first native look longer than one run's walk is carried across runs, like the logs."""

    def test_a_long_first_native_look_is_walked_over_several_runs_and_nothing_is_skipped(self):
        self.chain.head, self.chain.block_s = 10_000, 0.25  # Arbitrum's four blocks a second
        self.chain.finalized = 10_000
        acct = self.account(assets=("ETH",))
        self.quote_on(acct, "arbitrum", "ETH", at(self.chain.ts - 1500))  # 6480 blocks back, with the skew
        h, paid = "0x" + "c7" * 32, 4000  # in the oldest third of the walk
        self.chain.blocks[paid] = {"number": hex(paid), "timestamp": hex(self.chain.ts - 1500), "transactions": [
            {"hash": h, "to": MINE, "from": "0x" + "33" * 20, "value": hex(10**15), "input": "0x"}]}
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(paid), "logs": []}
        self.chain.balance = 10**15  # the balance the first look sees already holds the payment
        name, seen, runs = f"scan:evm:arbitrum:{acct.pk}", [], 0
        baseline = {"balance": str(10**15), "nonce": 0}
        while True:
            seen += evm.EvmAdapter().scan(acct, "arbitrum")
            runs += 1
            cursor = WatchCursor.objects.get(stream__startswith="evm-native")
            if cursor.extra == baseline:
                break
            h_ = WatcherHealth.objects.get(name=name)
            self.assertEqual((h_.last_error_kind, bool(h_.failing_since)), ("backlog", True))
            self.assertLess(runs, 10)
        self.assertGreaterEqual(runs, 3)
        self.assertEqual([(t.tx_hash, t.amount_base_units) for t in seen], [(h, 10**15)])
        self.assertFalse(ScanGap.objects.exists())
        self.assertEqual(cursor.position, "10000")  # the baseline is taken only at the head
        self.assertIsNone(WatcherHealth.objects.get(name=name).failing_since)


class ReplacedAccountFirstLookTests(QuoteMixin, EvmBase):
    """A new account on a retired one's address: the old account's own watching covers its
    quotes, so the new one does not read back a day for them."""

    def setUp(self):
        super().setUp()
        self.old = self.account(assets=("ETH",))
        self.old_quote = self.quote_on(self.old, "arbitrum", "ETH", at(self.chain.ts - 86_000))
        RailAccount.objects.filter(pk=self.old.pk).update(active=False)
        self.new = RailAccount.objects.create(org=self.org, kind="evm", label="new", address=MINE,
                                              networks=["arbitrum"], assets=["ETH"])

    def old_cursor(self, written):
        WatchCursor.objects.create(network="arbitrum", address=MINE, stream=f"evm-native:{self.old.pk}",
                                   position="990", extra={"balance": "0", "nonce": 0})
        WatchCursor.objects.filter(stream=f"evm-native:{self.old.pk}").update(updated_at=written)

    def test_a_quote_another_accounts_cursor_covers_is_not_read_back_for(self):
        self.old_cursor(self.old_quote.issued_at + datetime.timedelta(seconds=60))
        self.assertIsNone(chains.scan_back_to(self.new, "arbitrum"))
        self.assertEqual(evm.EvmAdapter().scan(self.new, "arbitrum"), [])
        self.assertFalse(ScanGap.objects.exists())
        walked = [m for _, m in self.chain.calls if m == "eth_getBlockByNumber"]
        self.assertEqual(walked, [])
        self.assertEqual(WatchCursor.objects.get(stream=f"evm-native:{self.new.pk}").position, "1000")

    def test_a_quote_no_cursor_covered_yet_is_still_read_back_for(self):
        self.old_cursor(self.old_quote.issued_at - datetime.timedelta(seconds=60))  # stopped before the quote
        self.assertEqual(chains.scan_back_to(self.new, "arbitrum"), self.old_quote.issued_at - SKEW)
        self.assertEqual(chains.scan_back_to(self.new, "base"), self.old_quote.issued_at - SKEW)  # no cursor there
        own = self.quote_on(self.new, "arbitrum", "ETH", at(self.chain.ts - 600))
        WatchCursor.objects.filter(stream=f"evm-native:{self.old.pk}").update(
            updated_at=self.old_quote.issued_at + datetime.timedelta(seconds=60))
        self.assertEqual(chains.scan_back_to(self.new, "arbitrum"), own.issued_at - SKEW)  # its own always count


def ton_rows(n, start=0):
    base = recorded()["jetton_transfers"][0]
    out = []
    for i in range(start, start + n):
        r = copy.deepcopy(base)
        r["transaction_hash"] = base64.b64encode(i.to_bytes(32, "big")).decode()
        out.append(r)
    return out


class ListingCenter:
    """toncenter's /jetton/transfers over a listing the test can shift between runs (offset and
    limit honoured, oldest first)."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, url, *, params=None, headers=None, service, timeout=10):
        self.calls.append(dict(params or {}))
        offset, limit = int(params["offset"]), int(params["limit"])
        listed = [r for r in self.rows if r["transaction_now"] >= int(params["start_utime"])]
        return {"jetton_transfers": listed[offset:offset + limit]}


class TonContinuationTests(TonBase):
    def run_capped(self):
        from apps.rails.chains import ton

        self.cursor(NEWEST - 1000)
        rows = ton_rows(ton.PAGE_LIMIT * ton.MAX_PAGES)
        self.after, self.paid = ton_rows(1, 10_000)[0], ton_rows(1, 10_001)[0]
        self.paid["amount"] = "7000000"
        center = ListingCenter(rows + [self.after, self.paid])
        p = mock.patch("apps.rails.http.get_json", side_effect=center)
        p.start()
        self.addCleanup(p.stop)
        first = self.scan()
        self.boundary = ton.canonical_hash(rows[-1]["transaction_hash"])
        self.assertNotIn(self.boundary, [t.tx_hash for t in first])  # held for the next run
        return center

    def hashes(self, *rows):
        from apps.rails.chains import ton

        return [ton.canonical_hash(r["transaction_hash"]) for r in rows]

    def test_a_listing_shifted_forward_does_not_read_the_boundary_row_twice(self):
        center = self.run_capped()
        center.rows.insert(0, ton_rows(1, 20_000)[0])  # a row indexed late, before the cut
        got = self.scan()
        ids = sorted((t.tx_hash, t.log_index) for t in got)
        self.assertEqual(ids, sorted((h, 0) for h in [self.boundary, *self.hashes(self.after, self.paid)]))

    def test_a_listing_shifted_back_does_not_skip_the_row_after_the_cut(self):
        center = self.run_capped()
        del center.rows[0]  # a row before the cut is gone
        got = self.scan()
        self.assertEqual(sorted(t.tx_hash for t in got),
                         sorted([self.boundary, *self.hashes(self.after, self.paid)]))

    def test_a_continuation_toncenter_refuses_is_dropped_and_the_next_run_starts_afresh(self):
        center = self.run_capped()
        stopped_at = max(r["transaction_now"] for r in center.rows[:2000])
        position = WatchCursor.objects.get(stream=self.stream).position
        with mock.patch("apps.rails.http.get_json", side_effect=http.Refused("ton", 400)):
            with self.assertRaises(http.Unavailable) as cm:
                self.scan()
        self.assertEqual(cm.exception.kind, "continuation_refused")
        c = WatchCursor.objects.get(stream=self.stream)
        self.assertEqual(c.position, position)
        self.assertNotIn("resume", c.extra)
        center.calls.clear()
        got = self.scan()
        self.assertEqual(center.calls[0]["offset"], 0)  # afresh, from no later than where it had read to
        self.assertLessEqual(center.calls[0]["start_utime"], stopped_at)
        got += self.scan()  # the listing is as long as before: one more run reads past the cut
        self.assertIn(7_000_000, [t.amount_base_units for t in got])


class TronTwoContractTests(TronBase):
    """A refused continuation of one contract falls back to that contract's own last read, not to
    the cursor every contract shares (only USDT is listed today; a second is patched in)."""

    OTHER = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"

    def setUp(self):
        super().setUp()
        import dataclasses

        from apps.rails.networks import NETWORKS as NETS, Asset

        net = NETS["tron"]
        two = dataclasses.replace(net, assets={**net.assets, "USDX": Asset("USDX", self.OTHER, 6, 10**4)})
        self.enterContext(mock.patch.dict(NETS, {"tron": two}))
        RailAccount.objects.filter(pk=self.acct.pk).update(assets=["USDT", "USDX"])
        self.acct.refresh_from_db()

    def test_a_refused_continuation_resumes_from_its_own_last_read(self):
        self.cursor(T_OLD)
        n = tron.PAGE_LIMIT * tron.MAX_PAGES
        spam = [row(f"{i + 1:064x}", T_OLD - 300_000 + i, 0) for i in range(n + 1)]  # more than one run reads
        late = row("cd" * 32, T_OLD - 100_000, 5_000_000)  # a USDT payment after the cut, not read yet
        listing, refuse, calls = spam + [late], {"on": False}, []

        def grid(url, *, params=None, headers=None, service, timeout=10):
            calls.append(dict(params))
            if params["contract_address"] == self.OTHER:  # USDX reads to its end, far ahead
                return page([row("ef" * 32, T_OLD + 900_000, 1, contract=self.OTHER)])
            if "fingerprint" in params and refuse["on"]:
                raise http.Refused("tron", 400)
            rows = [r for r in listing if r["block_timestamp"] >= params["min_timestamp"]]
            k = int(params.get("fingerprint") or 0)
            more = (k + 1) * tron.PAGE_LIMIT < len(rows)
            return page(rows[k * tron.PAGE_LIMIT:(k + 1) * tron.PAGE_LIMIT], fingerprint=str(k + 1) if more else None)

        with mock.patch("apps.rails.http.get_json", side_effect=grid):
            adapter_for("tron").scan(self.acct, "tron")  # USDT capped; USDX moves the shared cursor far ahead
            position = int(WatchCursor.objects.get(stream=self.stream).position)
            self.assertGreater(position - tron.OVERLAP_MS, late["block_timestamp"])
            refuse["on"] = True
            with self.assertRaises(http.Unavailable):
                adapter_for("tron").scan(self.acct, "tron")
            refuse["on"] = False
            calls.clear()
            got = adapter_for("tron").scan(self.acct, "tron")
        usdt = next(p for p in calls if p["contract_address"] == USDT)
        self.assertLessEqual(usdt["min_timestamp"], late["block_timestamp"])
        self.assertIn(5_000_000, [t.amount_base_units for t in got])


class SolanaCappedFirstLookTests(QuoteMixin, SolanaBase):
    def test_a_first_look_with_more_than_one_run_of_history_skips_nothing(self):
        paid_at = 1_792_768_111
        self.quote_on(self.acct, "solana", "USDC", at(paid_at - 600))
        newer = [{"signature": f"s{i}", "err": None, "blockTime": paid_at + i} for i in reversed(range(40))]
        older = [{"signature": "before", "err": None, "blockTime": paid_at - 3600}]
        self.rpc.signatures[TOKEN_ACCOUNT] = newer + older
        self.rpc.default_tx = self.rpc.txs[SIG]
        name = f"scan:solana:solana:{self.acct.pk}"
        first = self.scan()
        h = WatcherHealth.objects.get(name=name)
        self.assertEqual((len(first), h.last_error_kind, bool(h.failing_since)), (25, "backlog", True))
        second = self.scan()
        self.assertEqual(len(second), 15)
        self.assertEqual(sorted(int(t.tx_hash[1:]) for t in first + second), list(range(40)))
        reads = [p[0] for _, m, p in self.rpc.calls if m == "getTransaction"]
        self.assertNotIn("before", reads)
        self.assertIsNone(WatcherHealth.objects.get(name=name).failing_since)
