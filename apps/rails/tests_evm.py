import copy
import datetime
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings

from apps.orgs.models import Organization
from apps.rails import fx, http
from apps.rails.chains import evm
from apps.rails.chains.base import adapter_for
from apps.rails.models import RailAccount, ScanGap, WatchCursor, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin

FIX = Path(__file__).parent / "fixtures" / "evm"
MINE = "0x1111111111111111111111111111111111111111"
USDC_ARB = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
USDC_BSC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
TRANSFER = evm.TRANSFER_TOPIC


def pad(addr):
    return "0x" + addr[2:].rjust(64, "0")


def my_log(n=0x100, idx=1, amount=1_000_000, contract=USDC_ARB, tx="0x" + "ab" * 32):
    return {"address": contract, "topics": [TRANSFER, pad("0x" + "22" * 20), pad(MINE)],
            "data": "0x" + format(amount, "064x"), "blockNumber": hex(n), "transactionHash": tx,
            "logIndex": hex(idx), "removed": False}


class FakeChain:
    """Answers JSON-RPC from a dict; records calls. `fail` maps method -> exception factory.

    A block not in `blocks` is made at `ts`, or `block_s` seconds per block before the head when
    that is set. A transaction with logs in `logs` and no entry in `receipts` has the receipt a
    node would give: successful, in the block of its logs, holding them in order."""

    def __init__(self, head=1000, finalized=900, ts=1_700_000_000):
        self.head, self.finalized, self.ts = head, finalized, ts
        self.logs, self.blocks, self.receipts, self.txs = [], {}, {}, {}
        self.balance, self.nonce = 0, 0
        self.calls, self.log_ranges, self.errors = [], [], {}
        self.max_span = None
        self.block_s = 0

    def __call__(self, url, payload, **kw):
        m, p = payload["method"], payload["params"]
        self.calls.append((url, m))
        if m in self.errors:
            e = self.errors[m](url, p)
            if e is not None:
                return e
        res = getattr(self, "_" + m)(*p)
        return {"jsonrpc": "2.0", "id": 1, "result": res}

    def _eth_blockNumber(self):
        return hex(self.head)

    def _eth_getBlockByNumber(self, n, full):
        if n == "finalized":
            return {"number": hex(self.finalized), "timestamp": hex(self.ts)}
        num = int(n, 16)
        stamp = int(self.ts - (self.head - num) * self.block_s)
        b = self.blocks.get(num, {"number": hex(num), "timestamp": hex(stamp), "transactions": []})
        return b

    def _eth_getLogs(self, f):
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        self.log_ranges.append((lo, hi))
        if self.max_span and hi - lo + 1 > self.max_span:
            raise AssertionError("range")
        return [log for log in self.logs if lo <= int(log["blockNumber"], 16) <= hi
                and log["topics"][2] == f["topics"][2] and log["address"] in f["address"]]

    def _eth_getTransactionReceipt(self, h):
        if h in self.receipts:
            return self.receipts[h]
        logs = [log for log in self.logs if log["transactionHash"] == h]
        if not logs:
            return None
        return {"transactionHash": h, "blockNumber": logs[0]["blockNumber"], "status": "0x1", "logs": logs}

    def _eth_getBlockReceipts(self, n):
        num = int(n, 16)
        hashes = list(dict.fromkeys(log["transactionHash"] for log in self.logs if int(log["blockNumber"], 16) == num))
        hashes += [h for h, r in self.receipts.items() if h not in hashes and r.get("blockNumber") == hex(num)]
        return [self._eth_getTransactionReceipt(h) for h in hashes]

    def _eth_getTransactionByHash(self, h):
        return self.txs.get(h)

    def _eth_getBalance(self, a, at):
        return hex(self.balance)

    def _eth_getTransactionCount(self, a, at):
        return hex(self.nonce)


def rpc_error(msg):
    return lambda url, p: {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": msg}}


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="lt", legal_name="LT s.r.o.")
        self.chain = FakeChain()
        patcher = mock.patch("apps.rails.http.post_json", side_effect=self.chain)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime.datetime.fromtimestamp(1_700_000_000 + 10, tz=datetime.timezone.utc)
        n = mock.patch("apps.rails.chains.evm.timezone.now", side_effect=lambda: self.now)
        n.start()
        self.addCleanup(n.stop)

    acct = None

    def account(self, networks=("arbitrum",), assets=("ETH", "USDC", "USDT")):
        if self.acct is None:
            self.acct = RailAccount.objects.create(org=self.org, kind="evm", label="evm", address=MINE)
        self.acct.networks, self.acct.assets = list(networks), list(assets)
        self.acct.save()
        return self.acct

    def cursor(self, network, stream, position, extra=None):
        pk = (self.acct or self.account()).pk
        WatchCursor.objects.create(network=network, address=MINE, stream=f"{stream}:{pk}",
                                   position=str(position), extra=extra or {})


class LogScanTests(Base):
    def test_garbage_block_timestamp_is_bad_answer(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.logs = [my_log(n=995, idx=7)]
        self.chain.blocks[995] = {"number": hex(995), "timestamp": hex(10**25), "transactions": []}
        with self.assertRaises(http.Unavailable) as cm:
            evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(cm.exception.kind, "bad_answer")
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "990")

    def test_log_parsed_into_transfer(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.logs = [my_log(n=995, idx=7, amount=12_345_678)]
        (t,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual((t.amount_base_units, t.to_address, t.from_address, t.contract),
                         (12_345_678, MINE, "0x" + "22" * 20, USDC_ARB))
        # The log's place in its transaction (the only log there), not the block's logIndex 7:
        # a reorg moving the transaction must not give it a second identity (audit MONEY-8).
        self.assertEqual((t.tx_hash, t.log_index, t.block_number), ("0x" + "ab" * 32, 0, 995))
        self.assertEqual(t.block_time.timestamp(), 1_700_000_000)
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "1000")

    def test_recorded_usdc_log_is_parsed(self):
        log = json.loads((FIX / "usdc_logs.json").read_text())["result"][0]
        log["topics"][2] = pad(MINE)
        block = json.loads((FIX / "usdc_block.json").read_text())["result"]
        n = int(log["blockNumber"], 16)
        self.chain.head, self.chain.finalized = n + 5, n - 1
        self.chain.blocks[n] = block
        self.chain.logs = [log]
        receipt = json.loads((FIX / "usdc_receipt.json").read_text())["result"]  # the same transaction
        for item in receipt["logs"]:
            if item["address"] == USDC_ARB:
                item["topics"][2] = pad(MINE)  # rewritten like the listed log above
        self.chain.receipts[log["transactionHash"]] = receipt
        self.cursor("arbitrum", "evm-logs", n - 1)
        (t,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual((t.tx_hash, t.log_index, t.amount_base_units), (log["transactionHash"], 1, 0x3c47150c))
        self.assertEqual(t.block_time.timestamp(), int(block["timestamp"], 16))

    def test_first_run_starts_at_head(self):
        self.chain.logs = [my_log(n=500)]
        self.assertEqual(evm.EvmAdapter().scan(self.account(), "arbitrum"), [])
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "1000")
        self.assertNotIn("eth_getLogs", [m for _, m in self.chain.calls])

    def test_cursor_not_advanced_when_a_chunk_fails(self):
        self.cursor("arbitrum", "evm-logs", 100)
        self.chain.head = 100 + 2 * evm.LOG_CHUNK
        self.chain.logs = [my_log(n=150)]
        seen = []

        def second_chunk_fails(url, p):
            seen.append(p[0]["fromBlock"])
            return None if len(seen) == 1 else rpc_error("boom")(url, p)

        self.chain.errors["eth_getLogs"] = second_chunk_fails
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "100")
        self.assertTrue(WatcherHealth.objects.get(name__startswith="scan:evm:arbitrum:").failing_since)

    def test_range_error_halves_chunk(self):
        self.cursor("arbitrum", "evm-logs", 100)
        self.chain.head = 100 + 2000
        self.chain.logs = [my_log(n=1900)]
        self.chain.max_span = 600

        def too_large(url, p):
            f = p[0]
            span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
            return rpc_error("query returned more than 10000 results / block range too large")(url, p) if span > 600 else None

        self.chain.errors["eth_getLogs"] = too_large
        (t,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(t.block_number, 1900)
        spans = [hi - lo + 1 for lo, hi in self.chain.log_ranges]
        self.assertTrue(all(s <= 600 for s in spans))
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "2100")

    def test_range_error_at_minimum_chunk_gives_up(self):
        self.cursor("arbitrum", "evm-logs", 100)
        self.chain.head = 5000
        self.chain.errors["eth_getLogs"] = rpc_error("block range too large")
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "100")

    def test_failover_to_second_endpoint(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.logs = [my_log(n=995)]
        first = evm.rpc_urls("arbitrum")[0]

        def first_down(url, p):
            return rpc_error("down")(url, p) if url == first else None

        for m in ("eth_blockNumber", "eth_getLogs", "eth_getBlockByNumber"):
            self.chain.errors[m] = first_down
        self.assertEqual(len(evm.EvmAdapter().scan(self.account(), "arbitrum")), 1)
        self.assertIn(evm.rpc_urls("arbitrum")[1], [u for u, _ in self.chain.calls])

    def test_every_network_has_two_endpoints_and_override_replaces(self):
        from apps.rails.networks import NETWORKS
        for key, net in NETWORKS.items():
            if net.family == "evm":
                self.assertGreaterEqual(len(evm.rpc_urls(key)), 2, key)
        with override_settings(EVM_RPC_URLS={"base": ["https://mine.example"]}):
            self.assertEqual(evm.rpc_urls("base"), ["https://mine.example"])

    def test_other_recipient_and_foreign_contract_ignored(self):
        self.cursor("arbitrum", "evm-logs", 990)
        foreign = my_log(n=995, contract="0x" + "99" * 20)
        short = my_log(n=996)
        short["topics"] = short["topics"] + [pad(MINE)]  # an ERC-721 shape
        zero = my_log(n=997, amount=0)
        self.chain.logs = [foreign, short, zero]
        self.assertEqual(evm.EvmAdapter().scan(self.account(), "arbitrum"), [])

    def test_bsc_18_decimal_amount_exact(self):
        self.cursor("bsc", "evm-logs", 990)
        amount = 123_456_789_012_345_678_901
        self.chain.logs = [my_log(n=995, amount=amount, contract=USDC_BSC)]
        (t,) = evm.EvmAdapter().scan(self.account(("bsc",), ("USDC",)), "bsc")
        self.assertEqual(t.amount_base_units, amount)


class FinalityTests(Base):
    def one(self, network="arbitrum", block=995, amount=5_000_000, contract=USDC_ARB):
        self.cursor(network, "evm-logs", 990)
        self.chain.logs = [my_log(n=block, amount=amount, contract=contract)]
        return evm.EvmAdapter().scan(self.account((network,), ("USDC", "ETH")), network)[0]

    def test_finalized_vs_latest(self):
        self.chain.finalized = 994
        self.assertFalse(self.one(network="ethereum", contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48").final)
        WatchCursor.objects.all().delete()
        self.chain.finalized = 995
        self.assertTrue(self.one(network="ethereum", contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48").final)

    def test_l1_style_network_never_takes_the_fast_path(self):
        self.now += datetime.timedelta(days=1)
        self.chain.finalized = 10
        self.assertFalse(self.one(network="polygon", contract="0x3c499c542cef5e3811e1192ce70d8cc03d5c3359").final)

    def test_l2_small_amount_final_after_300s(self):
        self.chain.finalized = 10
        self.assertFalse(self.one().final)  # 10 s old
        WatchCursor.objects.all().delete()
        self.now += datetime.timedelta(seconds=300)
        self.assertTrue(self.one().final)
        WatchCursor.objects.all().delete()
        self.assertFalse(self.one(amount=1_000_000_001).final)  # just over 1000 USD

    def test_l2_eth_uses_fx_and_waits_when_fx_is_down(self):
        self.chain.finalized = 10
        self.now += datetime.timedelta(seconds=400)
        run = evm._Run("arbitrum")
        old = self.now - datetime.timedelta(seconds=400)
        with mock.patch("apps.rails.fx.price_in", return_value=fx.RatePair(Decimal(3000), Decimal(3000), "x")):
            self.assertTrue(run.is_final("", 10**17, 995, old))  # 300 USD
            self.assertFalse(evm._Run("arbitrum").is_final("", 10**18, 995, old))  # 3000 USD
        with mock.patch("apps.rails.fx.price_in", side_effect=fx.RatesUnavailable("down")):
            with self.assertLogs("apps.rails.chains.evm", "WARNING") as logs:
                self.assertFalse(evm._Run("arbitrum").is_final("", 10**15, 995, old))
        self.assertNotIn("0x", "".join(logs.output))


class NativeTests(Base):
    def tx(self, h, to=MINE, value=10**15):
        return {"hash": h, "to": to, "from": "0x" + "33" * 20, "value": hex(value), "input": "0x"}

    def put_block(self, n, txs):
        self.chain.blocks[n] = {"number": hex(n), "timestamp": hex(self.chain.ts), "transactions": txs}

    def test_native_scanned_only_when_balance_or_nonce_changed(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.put_block(995, [self.tx("0x" + "c1" * 32)])
        self.chain.receipts["0x" + "c1" * 32] = {"status": "0x1", "logs": []}
        a = evm.EvmAdapter()
        self.assertEqual(a.scan(self.account(assets=("ETH",)), "arbitrum"), [])  # unchanged: no block read
        self.assertNotIn("eth_getBlockByNumber", [m for _, m in self.chain.calls])
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-native").position, "1000")
        self.cursor_pos(990)
        self.chain.balance = 10**15
        (t,) = a.scan(self.account(assets=("ETH",)), "arbitrum")
        self.assertEqual((t.contract, t.log_index, t.amount_base_units, t.block_number), ("", -1, 10**15, 995))
        c = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual((c.position, c.extra), ("1000", {"balance": str(10**15), "nonce": 0}))

    def cursor_pos(self, pos):
        WatchCursor.objects.filter(stream__startswith="evm-native").update(position=str(pos))

    def test_nonce_change_alone_triggers_scan(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.nonce = 1
        self.evm_scan()
        self.assertIn("eth_getBlockByNumber", [m for _, m in self.chain.calls])

    def evm_scan(self):
        return evm.EvmAdapter().scan(self.account(assets=("ETH",)), "arbitrum")

    def test_reverted_native_tx_ignored(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.balance = 5
        self.put_block(995, [self.tx("0x" + "c2" * 32)])
        self.chain.receipts["0x" + "c2" * 32] = {"status": "0x0", "logs": []}
        with self.assertLogs("apps.rails.chains.evm", "WARNING"):
            self.assertEqual(self.evm_scan(), [])
        # The balance rose by 5 and the only transaction is reverted: nothing is reported, and the
        # rise is left for a person (audit MONEY-8).
        self.assertEqual(ScanGap.objects.get().reason, "unexplained_balance")

    def test_first_run_native_starts_at_head(self):
        self.chain.balance = 7
        self.assertEqual(self.evm_scan(), [])
        c = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual((c.position, c.extra), ("1000", {"balance": "7", "nonce": 0}))

    def slow_chain(self, seconds_per_block: float):
        """Every block read costs `seconds_per_block` on a fake clock (the walk's own clock)."""
        clock = SimpleNamespace(now=1000.0)
        real = self.chain._eth_getBlockByNumber

        def slow(n, full):
            if n != "finalized":
                clock.now += seconds_per_block
            return real(n, full)

        self.chain._eth_getBlockByNumber = slow
        self.enterContext(mock.patch.object(evm, "_monotonic", side_effect=lambda: clock.now))
        return clock

    def test_a_native_walk_stops_after_its_time_budget_and_carries_on(self):
        """Residual R4: a run walks at most NATIVE_SCAN_MAX_BLOCKS blocks or NATIVE_SCAN_MAX_SECONDS
        seconds, whichever comes first, and stays "backlog" until it reaches the head."""
        self.assertEqual(evm.NATIVE_SCAN_MAX_SECONDS, 120)
        start = 10
        self.cursor("arbitrum", "evm-native", start, {"balance": "0", "nonce": 0})
        self.chain.head, self.chain.balance = start + 500, 10**15
        h = "0x" + "ca" * 32
        self.put_block(start + 400, [self.tx(h)])
        self.chain.receipts[h] = {"status": "0x1", "logs": []}
        self.slow_chain(1.0)  # one second a block: 120 blocks a run
        name = f"scan:evm:arbitrum:{self.account(assets=('ETH',)).pk}"
        self.assertEqual(self.evm_scan(), [])
        cursor = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual(int(cursor.position), start + 120)
        self.assertTrue(cursor.extra["walking"])
        self.assertEqual(WatcherHealth.objects.get(name=name).last_error_kind, "backlog")
        seen, runs = [], 1
        while (WatchCursor.objects.get(stream__startswith="evm-native").extra or {}).get("walking"):
            seen += self.evm_scan()
            runs += 1
            self.assertLess(runs, 10)
        self.assertEqual(runs, 5)  # 500 blocks at 120 a run
        self.assertEqual([t.tx_hash for t in seen], [h])
        self.assertFalse(ScanGap.objects.exists())
        self.assertIsNone(WatcherHealth.objects.get(name=name).failing_since)

    def test_a_growing_native_backlog_is_logged_without_addresses(self):
        start = 10
        self.cursor("arbitrum", "evm-native", start, {"balance": "0", "nonce": 0})
        self.chain.head, self.chain.nonce = start + 1000, 1  # the nonce moved: the walk starts
        self.slow_chain(1.0)
        with self.assertNoLogs("apps.rails.chains.evm", "WARNING"):
            self.evm_scan()  # 880 behind after the first run: nothing to compare yet
        self.chain.head += 300  # the chain ran ahead faster than the walk (120 a run)
        with self.assertLogs("apps.rails.chains.evm", "WARNING") as logs:
            self.evm_scan()
        self.assertIn("grew", logs.output[0])
        self.assertIn("880", logs.output[0])
        self.assertIn("1060", logs.output[0])
        self.assertNotIn(MINE, "".join(logs.output).lower())
        with self.assertNoLogs("apps.rails.chains.evm", "WARNING"):
            self.evm_scan()  # 940 behind now: shrinking is not news

    def test_a_native_backlog_is_walked_across_runs_and_nothing_is_skipped(self):
        """Final review F7: a later look more than NATIVE_SCAN_MAX_BLOCKS behind used to read only the
        newest blocks and record the rest as skipped (on Arbitrum, any worker downtime over eight
        minutes). It walks the whole backlog over several runs, oldest first, reporting "backlog"
        meanwhile, as the first look does; a payment in the middle of it is found and no block is skipped."""
        start, size = 10, evm.NATIVE_SCAN_MAX_BLOCKS
        self.cursor("arbitrum", "evm-native", start, {"balance": "0", "nonce": 0})
        self.chain.head, self.chain.balance = start + 3 * size, 10**15
        middle, h = start + 3 * size // 2, "0x" + "c9" * 32
        self.put_block(middle, [self.tx(h)])
        self.chain.receipts[h] = {"status": "0x1", "logs": []}
        name = f"scan:evm:arbitrum:{self.account(assets=('ETH',)).pk}"
        seen, runs, blocks = [], 0, []
        real = self.chain._eth_getBlockByNumber

        def spy(n, full):
            if n != "finalized":
                blocks.append(int(n, 16))
            return real(n, full)

        self.chain._eth_getBlockByNumber = spy
        while True:
            seen += self.evm_scan()
            runs += 1
            cursor = WatchCursor.objects.get(stream__startswith="evm-native")
            if not (cursor.extra or {}).get("walking"):
                break
            health = WatcherHealth.objects.get(name=name)
            self.assertEqual((health.last_error_kind, bool(health.failing_since)), ("backlog", True))
            self.assertLess(runs, 10)
        self.assertEqual(runs, 3)
        self.assertEqual(sorted(blocks), list(range(start + 1, start + 3 * size + 1)))  # every block, once
        self.assertEqual([(t.tx_hash, t.block_number) for t in seen], [(h, middle)])
        self.assertFalse(ScanGap.objects.exists())
        self.assertEqual((cursor.position, cursor.extra), (str(start + 3 * size), {"balance": str(10**15), "nonce": 0}))
        self.assertIsNone(WatcherHealth.objects.get(name=name).failing_since)

    def test_a_native_backlog_still_reports_a_rise_no_transaction_explains(self):
        start, size = 10, evm.NATIVE_SCAN_MAX_BLOCKS
        self.cursor("arbitrum", "evm-native", start, {"balance": "0", "nonce": 0})
        self.chain.head, self.chain.balance = start + 2 * size, 5  # risen by 5, and no transaction to us
        with self.assertLogs("apps.rails.chains.evm", "WARNING") as logs:
            for _ in range(3):
                self.evm_scan()
        gap = ScanGap.objects.get()
        self.assertEqual((gap.reason, gap.from_block, gap.to_block), ("unexplained_balance", start + 1, start + 2 * size))
        self.assertNotIn(MINE, "".join(logs.output))

    def test_native_gap_is_written_with_the_cursor_or_not_at_all(self):
        # A native look no longer skips blocks (F7); the gap here is a rise no transaction explains.
        self.cursor("arbitrum", "evm-native", 10, {"balance": "0", "nonce": 0})
        self.chain.head, self.chain.balance = 1_000, 1
        with mock.patch.object(ScanGap.objects, "create", side_effect=OperationalError("database is locked")), \
                self.assertRaises(OperationalError):
            self.evm_scan()
        self.assertFalse(ScanGap.objects.exists())
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-native").position, "10")  # not moved

    def test_native_failure_leaves_cursor(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.balance = 5
        self.chain.errors["eth_getBlockByNumber"] = rpc_error("boom")
        with self.assertRaises(http.Unavailable):
            self.evm_scan()
        c = WatchCursor.objects.get(stream__startswith="evm-native")
        self.assertEqual((c.position, c.extra["balance"]), ("990", "0"))


class TxPathTests(Base):
    def test_tx_hash_path_returns_native_minus_one_and_token_logs(self):
        receipt = json.loads((FIX / "usdc_receipt.json").read_text())["result"]
        tx = json.loads((FIX / "usdc_tx.json").read_text())["result"]
        block = json.loads((FIX / "usdc_block.json").read_text())["result"]
        n = int(receipt["blockNumber"], 16)
        for log in receipt["logs"]:
            if log["address"] == USDC_ARB:
                log["topics"][2] = pad(MINE)
        h = receipt["transactionHash"]
        self.chain.receipts[h], self.chain.txs[h], self.chain.blocks[n] = receipt, tx, block
        self.chain.finalized = n
        transfers = evm.EvmAdapter().transfers_in_tx("arbitrum", h.upper().replace("0X", "0x"))
        self.assertEqual([(t.log_index, t.contract) for t in transfers], [(1, USDC_ARB)])
        self.assertEqual(transfers[0].tx_hash, h)
        self.assertTrue(transfers[0].final)

        nr = json.loads((FIX / "native_receipt.json").read_text())["result"]
        nt = json.loads((FIX / "native_tx.json").read_text())["result"]
        nb = json.loads((FIX / "native_block.json").read_text())["result"]
        nt["to"] = MINE
        nn = int(nr["blockNumber"], 16)
        self.chain.receipts[nr["transactionHash"]], self.chain.txs[nr["transactionHash"]] = nr, nt
        self.chain.blocks[nn] = nb
        (t,) = evm.EvmAdapter().transfers_in_tx("arbitrum", nr["transactionHash"])
        self.assertEqual((t.log_index, t.contract, t.to_address, t.amount_base_units),
                         (-1, "", MINE, int(nt["value"], 16)))

    def test_scan_and_tx_path_give_the_same_identity(self):
        self.cursor("arbitrum", "evm-logs", 990)
        log = my_log(n=995, idx=7)
        self.chain.logs = [log]
        (a,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        h = log["transactionHash"]
        self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": [log]}
        self.chain.txs[h] = {"hash": h, "to": log["address"], "value": "0x0", "from": "0x" + "22" * 20}
        (b,) = evm.EvmAdapter().transfers_in_tx("arbitrum", h)
        self.assertEqual((a.tx_hash, a.log_index), (b.tx_hash, b.log_index))

    def test_failed_or_unknown_tx_gives_nothing(self):
        self.chain.receipts["0xdead"] = {"status": "0x0", "blockNumber": "0x1", "logs": []}
        self.assertEqual(evm.EvmAdapter().transfers_in_tx("arbitrum", "0xdead"), [])
        self.assertEqual(evm.EvmAdapter().transfers_in_tx("arbitrum", "0xbeef"), [])

    def test_malformed_hash_is_never_looked_up(self):
        for bad in ("0x123", "0x" + "g" * 64, "ab" * 32, "0x" + "ab" * 33, "", "0x" + "ab" * 31 + "a"):
            self.assertEqual(evm.EvmAdapter().transfers_in_tx("arbitrum", bad), [], bad)
        self.assertEqual(self.chain.calls, [])
        self.assertFalse(WatcherHealth.objects.exists())
        h = "0x" + "AB" * 32  # a well-formed hash in capitals is looked up
        self.assertEqual(evm.EvmAdapter().transfers_in_tx("arbitrum", f" {h} "), [])
        self.assertEqual(self.chain.calls[0][1], "eth_getTransactionReceipt")

    def test_garbage_receipt_is_a_bad_answer(self):
        h = "0x" + "ab" * 32
        self.chain.txs[h] = {"hash": h, "to": MINE, "value": "0x0", "from": "0x" + "22" * 20}
        for logs in (["not a log"], "garbage", 5):
            self.chain.receipts[h] = {"status": "0x1", "blockNumber": hex(995), "logs": logs}
            with self.assertRaises(http.Unavailable) as cm:
                evm.EvmAdapter().transfers_in_tx("arbitrum", h)
            self.assertEqual(cm.exception.kind, "bad_answer", logs)

    def pay(self, h, network="arbitrum"):
        return SimpleNamespace(network=network, tx_hash=h, contract=USDC_ARB, amount_base_units="1000000",
                               to_address=MINE, log_index=0, block_number=995)

    def test_status_dropped_pending_final(self):
        a = evm.EvmAdapter()
        # No receipt is "not yet known" while its block is not final (audit MONEY-8), never "dropped".
        self.assertEqual(a.status(self.pay("0xmissing")), "pending")
        # `final` reads the transfer again: the receipt holds the recorded log.
        self.chain.receipts["0xhere"] = {"status": "0x1", "blockNumber": hex(995),
                                         "logs": [my_log(n=995, amount=1_000_000, tx="0xhere")]}
        self.chain.finalized = 10
        self.assertEqual(a.status(self.pay("0xhere", "ethereum")), "pending")
        self.chain.finalized = 995
        self.assertEqual(a.status(self.pay("0xhere", "ethereum")), "final")
        self.chain.receipts["0xrev"] = {"status": "0x0", "blockNumber": hex(995), "logs": []}
        self.assertEqual(a.status(self.pay("0xrev")), "dropped")

    def test_registered_for_evm_networks(self):
        self.assertIsInstance(adapter_for("base"), evm.EvmAdapter)


class FixRoundTests(Base):
    def test_native_failure_after_logs_keeps_both_cursors(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.logs = [my_log(n=995)]
        self.chain.balance = 5
        self.chain.errors["eth_getBlockByNumber"] = lambda url, p: (
            rpc_error("boom")(url, p) if p[1] is True else None)
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(sorted(WatchCursor.objects.values_list("position", flat=True)), ["990", "990"])

    def test_health_recovers_after_a_failure(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.errors["eth_getLogs"] = rpc_error("boom")
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertTrue(WatcherHealth.objects.get().failing_since)
        del self.chain.errors["eth_getLogs"]
        evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertIsNone(WatcherHealth.objects.get().failing_since)

    def test_null_receipt_in_native_scan_is_unavailable(self):
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.balance = 5
        self.chain.blocks[995] = {"number": hex(995), "timestamp": hex(self.chain.ts), "transactions": [
            {"hash": "0x" + "c3" * 32, "to": MINE, "from": "0x" + "33" * 20, "value": "0x5", "input": "0x"}]}
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(assets=("ETH",)), "arbitrum")
        c = WatchCursor.objects.get()
        self.assertEqual((c.position, c.extra["balance"]), ("990", "0"))

    def test_rate_limit_message_fails_over(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.logs = [my_log(n=995)]
        first = evm.rpc_urls("arbitrum")[0]
        self.chain.errors["eth_getLogs"] = lambda url, p: (
            rpc_error("daily request limit exceeded, too many requests")(url, p) if url == first else None)
        self.assertEqual(len(evm.EvmAdapter().scan(self.account(), "arbitrum")), 1)
        spans = [hi - lo + 1 for lo, hi in self.chain.log_ranges]
        self.assertEqual(len(set(spans)), 1)  # the chunk was not halved

    def test_http_4xx_fails_over(self):
        self.cursor("arbitrum", "evm-logs", 990)
        first = evm.rpc_urls("arbitrum")[0]

        def refuse(url, payload, **kw):
            if url == first:
                raise http.Refused("evm:arbitrum", 403)
            return self.chain(url, payload, **kw)

        with mock.patch("apps.rails.http.post_json", side_effect=refuse):
            evm.EvmAdapter().scan(self.account(), "arbitrum")
            with mock.patch("apps.rails.chains.evm.rpc_urls", return_value=[first]):
                with self.assertRaises(http.Unavailable):
                    evm.EvmAdapter().scan(self.account(), "arbitrum")

    def test_log_scan_rereads_an_overlap(self):
        self.cursor("arbitrum", "evm-logs", 990)
        self.chain.head = 990
        self.chain.logs = [my_log(n=980)]  # a lagging node had answered [] for this earlier
        (t,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(t.block_number, 980)
        self.assertEqual(self.chain.log_ranges[0][0], 0)  # 2400 blocks back, clipped at genesis
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "990")  # never backwards

    def test_overlap_is_ten_minutes_of_blocks_on_every_network(self):
        self.assertEqual(evm.LOG_OVERLAP_SECONDS, 600)
        expected = {"ethereum": 50, "arbitrum": 2400, "optimism": 300, "base": 300, "polygon": 300, "bsc": 1334,
                    "avalanche": 300}
        from apps.rails.networks import NETWORKS
        self.assertEqual(set(expected), {k for k, n in NETWORKS.items() if n.family == "evm"})
        for network, blocks in expected.items():
            self.assertEqual(evm.overlap_blocks(network), blocks, network)
        # Arbitrum makes four blocks a second: 128 blocks was 32 s, less than one 60 s scan.
        self.cursor("arbitrum", "evm-logs", 100_000)
        self.chain.head = 100_240  # one minute later
        self.chain.logs = [my_log(n=100_000 - 2000)]  # eight minutes before the cursor: a node had clipped it
        (t,) = evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(t.block_number, 98_000)
        self.assertEqual(self.chain.log_ranges[0][0], 100_000 - 2400 + 1)
        self.assertEqual(WatchCursor.objects.get(stream__startswith="evm-logs").position, "100240")

    def test_garbage_answer_is_unavailable(self):
        self.cursor("arbitrum", "evm-logs", 990)
        bad = my_log(n=995)
        bad["data"] = "0x" + "zz" * 32
        nohash = my_log(n=995)
        del nohash["transactionHash"]
        for log in (bad, nohash):
            self.chain.logs = [log]
            with self.assertRaises(http.Unavailable):
                evm.EvmAdapter().scan(self.account(), "arbitrum")
        self.assertEqual(WatchCursor.objects.get().position, "990")
        self.chain.logs = []
        self.cursor("arbitrum", "evm-native", 990, {"balance": "0", "nonce": 0})
        self.chain.balance = 1
        self.chain.blocks[995] = {"number": hex(995), "timestamp": hex(1), "transactions": ["0xabc"]}
        with self.assertRaises(http.Unavailable):
            evm.EvmAdapter().scan(self.account(), "arbitrum")

    def test_accounts_sharing_an_address_keep_separate_cursors(self):
        a = self.account(assets=("USDC",))
        b = RailAccount.objects.create(org=self.org, kind="evm", label="two", address=MINE,
                                       networks=["arbitrum"], assets=["USDT"])
        evm.EvmAdapter().scan(a, "arbitrum")
        evm.EvmAdapter().scan(b, "arbitrum")
        self.assertEqual(WatchCursor.objects.count(), 2)
        self.chain.head = 1010
        self.chain.logs = [my_log(n=1005, contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9")]
        self.assertEqual(evm.EvmAdapter().scan(a, "arbitrum"), [])
        self.assertEqual(len(evm.EvmAdapter().scan(b, "arbitrum")), 1)
