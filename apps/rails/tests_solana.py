import copy
import json
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.orgs.models import Organization
from apps.rails import http
from apps.rails.chains import solana
from apps.rails.chains.base import adapter_for
from apps.rails.models import RailAccount, WatchCursor, WatcherHealth
from apps.vault.testing import UnsealedVaultMixin

FIX = Path(__file__).parent / "fixtures" / "solana"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
RECORDED_OWNER = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
RECORDED_SENDER = "EAmPpCuTsJDVGLJZjX2y7wv3jk2nTYGuiT8EF7GjRDxT"
TOKEN_ACCOUNT = "2ocS3orPq3jyszjsJ4NozKWyhdotr3csDjAizmkj65aH"
SIG = "5BqsK33GVK3c9Q6anpK7CiLq5bQrvbU3JB5RcSgB6rNfRiUBSVqBGtdPetuQUwyuptmVH2wE2GULmBukKKKaexWd"
MINE = "Vote111111111111111111111111111111111111111"


def load(name, owner=MINE):
    return json.loads((FIX / name).read_text().replace(RECORDED_OWNER, owner))


class FakeRpc:
    """Answers JSON-RPC by method; records every call."""

    def __init__(self):
        self.accounts = {USDC: [TOKEN_ACCOUNT], USDT: []}
        self.signatures = {TOKEN_ACCOUNT: load("signatures.json")["result"]}
        self.txs = {SIG: load("transaction.json")["result"]}
        self.calls, self.errors = [], {}
        self.missing, self.default_tx = set(), None

    def __call__(self, url, payload, **kw):
        method, params = payload["method"], payload["params"]
        self.calls.append((url, method, params))
        if method in self.errors:
            return self.errors[method](url, params)
        if method == "getTokenAccountsByOwner":
            value = [{"pubkey": a} for a in self.accounts.get(params[1]["mint"], [])]
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": value}}
        if method == "getSignaturesForAddress":
            sigs = [s["signature"] for s in self.signatures.get(params[0], [])]
            listed = self.signatures.get(params[0], [])
            before, until = params[1].get("before"), params[1].get("until")
            if before:
                listed = listed[sigs.index(before) + 1:]
            if until and until in [s["signature"] for s in listed]:
                listed = listed[:[s["signature"] for s in listed].index(until)]
            return {"jsonrpc": "2.0", "id": 1, "result": listed[:params[1]["limit"]]}
        if method == "getTransaction":
            if params[0] in self.missing:
                return {"jsonrpc": "2.0", "id": 1, "result": None}
            if params[0] not in self.txs and self.default_tx is not None:
                return {"jsonrpc": "2.0", "id": 1, "result": self.default_tx}
            if params[0] not in self.txs:  # the real RPC refuses what is not a signature
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid param"}}
            return {"jsonrpc": "2.0", "id": 1, "result": self.txs[params[0]]}
        if method == "getSignatureStatuses":
            return {"jsonrpc": "2.0", "id": 1, "result": {"value": [self.txs.get("status:" + params[0][0])]}}
        raise AssertionError(method)

    def methods(self):
        return [m for _, m, _ in self.calls]


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="lt", legal_name="LT s.r.o.")
        self.acct = RailAccount.objects.create(org=self.org, kind="solana", label="s", address=MINE,
                                               networks=["solana"], assets=["USDC"])
        self.rpc = FakeRpc()
        p = mock.patch("apps.rails.http.post_json", side_effect=self.rpc)
        p.start()
        self.addCleanup(p.stop)

    def stream(self, symbol="USDC"):
        return f"sol-{symbol}:{self.acct.pk}"

    def cursor(self, accounts, symbol="USDC"):
        WatchCursor.objects.create(network="solana", address=MINE, stream=self.stream(symbol),
                                   position="x", extra={"accounts": accounts})

    def scan(self):
        return adapter_for("solana").scan(self.acct, "solana")


class SolanaScanTests(Base):
    def test_solana_balance_delta_for_our_owner_only(self):
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        (t,) = self.scan()
        # The recorded transaction moved 2.7 USDC from another owner to ours; only ours counts.
        self.assertEqual((t.amount_base_units, t.to_address, t.from_address, t.contract),
                         (2_700_000, MINE, RECORDED_SENDER, USDC))
        self.assertEqual((t.tx_hash, t.log_index, t.network, t.final), (SIG, 2, "solana", True))
        self.assertEqual((t.block_number, t.block_time.timestamp()), (460488372, 1792768111))
        _, _, accounts_params = self.rpc.calls[0]
        self.assertEqual(accounts_params, [MINE, {"mint": USDC}, {"encoding": "jsonParsed", "commitment": "finalized"}])
        sig_params = next(p for _, m, p in self.rpc.calls if m == "getSignaturesForAddress")
        self.assertEqual(sig_params[0], TOKEN_ACCOUNT)
        self.assertEqual((sig_params[1]["until"], sig_params[1]["commitment"]), ("older", "finalized"))
        tx_params = next(p for _, m, p in self.rpc.calls if m == "getTransaction")
        self.assertEqual(tx_params[1], {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0})
        cur = WatchCursor.objects.get(stream=self.stream())
        self.assertEqual(cur.extra["accounts"], {TOKEN_ACCOUNT: SIG})  # the newest signature is the next `until`
        self.assertIsNone(WatcherHealth.objects.get(name=f"scan:solana:solana:{self.acct.pk}").failing_since)

    def test_solana_a_transfer_to_someone_else_is_not_ours(self):
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        tx = self.rpc.txs[SIG]
        for b in tx["meta"]["postTokenBalances"]:
            if b["owner"] == MINE:
                b["owner"] = "SomeoneElse1111111111111111111111111111111"
        self.assertEqual(self.scan(), [])

    def test_solana_failed_tx_skipped(self):
        self.cursor({TOKEN_ACCOUNT: "older"})
        sigs = load("signatures.json")["result"]
        sigs[0]["err"] = {"InstructionError": [0, "Custom"]}
        self.rpc.signatures[TOKEN_ACCOUNT] = sigs + [{"signature": "older", "err": None}]
        self.assertEqual(self.scan(), [])
        self.assertNotIn("getTransaction", self.rpc.methods())  # a failed signature is not even read
        # and a failed transaction that slipped through the listing moves nothing either
        self.rpc.txs[SIG]["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        self.cursor_reset()
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        self.assertEqual(self.scan(), [])
        self.assertEqual(adapter_for("solana").transfers_in_tx("solana", SIG), [])

    def cursor_reset(self):
        WatchCursor.objects.all().delete()
        self.cursor({TOKEN_ACCOUNT: "older"})

    def test_solana_no_token_account_yet_is_quiet(self):
        self.rpc.accounts[USDC] = []
        self.assertEqual(self.scan(), [])
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra, {"accounts": {}})
        self.assertIsNone(WatcherHealth.objects.get().failing_since)
        # The sender creates the account with the first payment: its history is all new.
        self.rpc.accounts[USDC] = [TOKEN_ACCOUNT]
        (t,) = self.scan()
        self.assertEqual((t.tx_hash, t.amount_base_units), (SIG, 2_700_000))

    def test_solana_first_sight_of_an_existing_account_reports_nothing(self):
        self.assertEqual(self.scan(), [])
        self.assertNotIn("getTransaction", self.rpc.methods())
        sig_params = next(p for _, m, p in self.rpc.calls if m == "getSignaturesForAddress")
        self.assertEqual(sig_params[1]["limit"], 1)
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: SIG})

    def test_solana_second_mint_failing_moves_no_cursor(self):
        self.acct.assets = ["USDC", "USDT"]
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        self.rpc.accounts[USDT] = ["acct2"]
        self.rpc.errors["getSignaturesForAddress"] = lambda url, p: (
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}} if p[0] == "acct2" else
            {"jsonrpc": "2.0", "id": 1, "result": []})
        self.cursor({"acct2": "u"}, symbol="USDT")
        with self.assertRaises(http.Unavailable):
            self.scan()
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: "older"})
        self.assertEqual(WatcherHealth.objects.get().last_error_kind, "rpc_error")

    def test_solana_failover_and_garbage(self):
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]

        def flaky(url, payload, **kw):
            if url == "https://one":
                raise http.Refused("solana", 403)
            return self.rpc(url, payload, **kw)

        with override_settings(SOLANA_RPC_URLS=["https://one", "https://two"]):
            with mock.patch("apps.rails.http.post_json", side_effect=flaky):
                self.assertEqual(len(self.scan()), 1)
            self.rpc.errors["getTokenAccountsByOwner"] = lambda url, p: {"jsonrpc": "2.0", "id": 1, "result": {"value": 5}}
            with self.assertRaises(http.Unavailable) as cm:
                self.scan()
        self.assertEqual(cm.exception.kind, "bad_answer")


class SolanaProgressTests(Base):
    def pending(self, n, prefix="s"):
        """n signatures newer than `base`, newest first, each readable."""
        sigs = [{"signature": f"{prefix}{i}", "err": None} for i in reversed(range(n))]
        self.rpc.signatures[TOKEN_ACCOUNT] = sigs + [{"signature": "base", "err": None}]
        self.rpc.default_tx = self.rpc.txs[SIG]
        self.cursor({TOKEN_ACCOUNT: "base"})
        return sigs

    def health(self):
        return WatcherHealth.objects.get(name=f"scan:solana:solana:{self.acct.pk}")

    def test_solana_backlog_drains_25_per_run(self):
        self.pending(60)
        seen = []
        for expected in (25, 25, 10):
            got = self.scan()
            self.assertEqual(len(got), expected)
            seen += [t.tx_hash for t in got]
        self.assertEqual(sorted(seen, key=lambda x: int(x[1:])), [f"s{i}" for i in range(60)])  # none skipped, none twice
        self.assertEqual(self.scan(), [])
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: "s59"})
        reads = [p for _, m, p in self.rpc.calls if m == "getTransaction"]
        self.assertEqual(len(reads), 60)

    def test_solana_backlog_reported_in_health(self):
        self.pending(30)
        self.assertEqual(len(self.scan()), 25)
        self.assertEqual((self.health().last_error_kind, bool(self.health().failing_since)), ("backlog", True))
        self.assertEqual(len(self.scan()), 5)
        self.assertEqual((self.health().last_error_kind, self.health().failing_since), ("", None))

    def test_solana_failed_read_commits_nothing(self):
        self.pending(30)
        calls = []

        def flaky(url, payload, **kw):
            if payload["method"] == "getTransaction":
                calls.append(1)
                if len(calls) >= 10:
                    return {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}
            return self.rpc(url, payload, **kw)

        with mock.patch("apps.rails.http.post_json", side_effect=flaky):
            with self.assertRaises(http.Unavailable):
                self.scan()
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: "base"})

    def test_solana_null_transaction_is_unavailable(self):
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        self.rpc.missing.add(SIG)
        with self.assertRaises(http.Unavailable) as cm:
            self.scan()
        self.assertEqual(cm.exception.kind, "tx_missing")
        self.assertEqual(WatchCursor.objects.get(stream=self.stream()).extra["accounts"], {TOKEN_ACCOUNT: "older"})

    def test_solana_pages_with_before(self):
        sigs = [{"signature": f"s{i}", "err": None} for i in reversed(range(1500))]
        self.rpc.signatures[TOKEN_ACCOUNT] = sigs + [{"signature": "base", "err": None}]
        self.rpc.default_tx = self.rpc.txs[SIG]
        self.cursor({TOKEN_ACCOUNT: "base"})
        got = self.scan()
        listings = [p[1] for _, m, p in self.rpc.calls if m == "getSignaturesForAddress"]
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[1]["before"], "s500")
        self.assertEqual(sorted(int(t.tx_hash[1:]) for t in got), list(range(25)))  # the oldest 25, not the newest

    def test_solana_listing_that_never_ends_is_unavailable(self):
        self.rpc.signatures[TOKEN_ACCOUNT] = [{"signature": f"s{i}", "err": None} for i in range(solana.SIG_PAGE * solana.MAX_PAGES + 5)]
        self.cursor({TOKEN_ACCOUNT: "base"})
        with self.assertRaises(http.Unavailable) as cm:
            self.scan()
        self.assertEqual(cm.exception.kind, "backlog")

    def test_solana_garbage_numbers_are_a_bad_answer(self):
        for edit in (lambda tx: tx.update(blockTime=1e30), lambda tx: tx.update(blockTime=10**30),
                     lambda tx: tx["meta"]["postTokenBalances"][1]["uiTokenAmount"].update(amount=1.5)):
            self.cursor({TOKEN_ACCOUNT: "older"})
            self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
            self.rpc.txs[SIG] = tx = load("transaction.json")["result"]
            edit(tx)
            with self.assertRaises(http.Unavailable) as cm:
                self.scan()
            self.assertEqual(cm.exception.kind, "bad_answer")
            WatchCursor.objects.all().delete()

    def test_solana_malformed_signature_returns_empty(self):
        for bad in ("nope", "", "0OIl" * 20, "1" * 63, "../x?y"):
            self.assertEqual(adapter_for("solana").transfers_in_tx("solana", bad), [])
        self.assertNotIn("getTransaction", self.rpc.methods())


class SolanaLookupTests(Base):
    def test_solana_recorded_transaction_matches_scan_identity(self):
        (looked,) = adapter_for("solana").transfers_in_tx("solana", f" {SIG} ")
        self.cursor({TOKEN_ACCOUNT: "older"})
        self.rpc.signatures[TOKEN_ACCOUNT] = load("signatures.json")["result"] + [{"signature": "older", "err": None}]
        (scanned,) = self.scan()
        self.assertEqual((looked.tx_hash, looked.log_index), (scanned.tx_hash, scanned.log_index))
        self.assertEqual(looked.to_address, MINE)

    def test_solana_unknown_transaction_is_empty_and_other_mints_ignored(self):
        self.rpc.missing.add("1" * 64)
        self.assertEqual(adapter_for("solana").transfers_in_tx("solana", "1" * 64), [])
        tx = copy.deepcopy(self.rpc.txs[SIG])
        for key in ("preTokenBalances", "postTokenBalances"):
            for b in tx["meta"][key]:
                b["mint"] = "MintNotOnTheList11111111111111111111111111"
        self.rpc.txs["other"] = tx
        self.assertEqual(adapter_for("solana").transfers_in_tx("solana", "other"), [])

    def test_solana_status(self):
        pay = mock.Mock(network="solana", tx_hash=SIG, log_index=2)
        self.rpc.txs["status:" + SIG] = {"confirmationStatus": "finalized", "err": None}
        self.assertEqual(adapter_for("solana").status(pay), "final")
        self.rpc.txs["status:" + SIG] = {"confirmationStatus": "confirmed", "err": None}
        self.assertEqual(adapter_for("solana").status(pay), "pending")
        self.rpc.txs["status:" + SIG] = None
        self.assertEqual(adapter_for("solana").status(pay), "dropped")
