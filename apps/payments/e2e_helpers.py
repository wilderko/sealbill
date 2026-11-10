"""Machinery shared by the end-to-end payment tests (`apps/payments/tests_e2e_*.py`).

A test drives an invoice through the system's own public doors only: the REST API (a token with
scopes) or the staff pages create and send it, the client opens the pay page through the
capability URL from the letter the outbox sent, asks for a quote there, and the worker's
registered jobs (or `manage.py run_worker --once`) find and settle the payment.

Only the outside world is faked, at the place where the system reaches it:

- `FakeInternet` stands in for the `requests` module inside `apps.rails.http`, the one seam every
  chain, gateway and exchange-rate call goes through (so `http._answer` still turns a timeout, a
  503 or garbage into "could not look"). Behind it sit fake services that answer the way the
  real ones do, from their documented shapes and the recorded fixtures in `apps/rails/fixtures/`:
  `FxMarket` (Kraken, Coinbase), `EvmNode` (JSON-RPC), `TronGrid`, `SolanaRpc`, `TonCenter` and
  `BTCPayServer` (Greenfield).
- `FakeHub` stands in for `nostr_sdk.NostrWalletConnect`, the Lightning wallet over NWC.
- The webhook receiver: `socket.getaddrinfo` answers a public address and
  `apps.webhooks.delivery.post` records what would have been POSTed.
- `Clock` moves time for the cases that need it (an expired quote, a transfer that ages into
  finality). It replaces `django.utils.timezone.now` and the `time` the Tron and TON watchers read.

Nothing here touches the matcher, a watcher's decisions or the models.
"""
from __future__ import annotations

import base64
import copy
import csv
import datetime
import hashlib
import hmac
import io
import itertools
import json
import re
import socket
import time
import types
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api.models import ApiToken
from apps.audit.models import AuditEntry
from apps.core import jobs as core_jobs
from apps.core import worker
from apps.invoices.models import Invoice
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.orgs.testing import add_bank
from apps.payments.models import Payment
from apps.quotes.models import Quote
from apps.rails import http as rails_http
from apps.rails.addresses import _b58encode, tron_hex_to_base58
from apps.rails.chains import evm as evm_chain
from apps.rails.networks import NETWORKS
from apps.rails.models import WatcherHealth
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks import delivery as webhook_delivery
from apps.webhooks.models import WebhookDelivery

STAFF = "staff@seller.example"
CLIENT = "client@example.com"
CLIENT_NAME = "Klient Example s.r.o."
HOOK_URL = "https://hooks.example.com/pay"
PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
SITE = "https://pay.example"

# One whole unit of each asset, in each currency (Kraken's price; Coinbase is a hair off).
PRICES = {
    ("USDC", "EUR"): "0.9237", ("USDC", "USD"): "1.0000",
    ("USDT", "EUR"): "0.9237", ("USDT", "USD"): "1.0000",
    ("ETH", "EUR"): "3000.00", ("ETH", "USD"): "3250.00",
    ("BTC", "EUR"): "60000.00", ("BTC", "USD"): "65000.00",
}

# The webhook body names ids, statuses, kinds and codes; these keys are all `data` may hold.
THIN_DATA_KEYS = {"payment_id", "invoice_id", "status", "rail", "reason", "kind", "declaration_id",
                  "invoice_status", "endpoint_id", "count", "watchers", "networks"}


def digest(*parts) -> bytes:
    return hashlib.sha256("/".join(map(str, parts)).encode()).digest()


def evm_address(tag: str) -> str:
    return "0x" + digest("evm", tag).hex()[:40]


def tron_address(tag: str) -> str:
    return tron_hex_to_base58("41" + digest("tron", tag).hex()[:40])


def tron_hex(address_b58: str) -> str:
    from apps.rails.addresses import _b58decode

    return "0x" + _b58decode(address_b58)[1:21].hex()


def solana_address(tag: str) -> str:
    return _b58encode(digest("sol", tag))


def ton_address(tag: str) -> str:
    return "0:" + digest("ton", tag).hex()


# ---------------------------------------------------------------------------
# The Internet
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, status=200, invalid_json=False):
        self.status_code, self._payload, self._invalid = status, payload, invalid_json

    def json(self):
        if self._invalid:
            raise ValueError("not JSON")
        return copy.deepcopy(self._payload)


class _FakeSession:
    """A `requests.Session` over the fake internet: the guard's adapter is mounted and never used, since
    nothing here opens a socket (the guard itself is tested in apps/rails/tests_destinations.py)."""

    trust_env = True

    def __init__(self, internet):
        self.internet = internet

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mount(self, prefix, adapter):
        pass

    def get(self, url, **kwargs):
        return self.internet.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.internet.post(url, **kwargs)

    def delete(self, url, **kwargs):
        return self.internet.delete(url, **kwargs)


class FakeInternet:
    """What `apps.rails.http` calls `requests`: routes each URL to the service serving its prefix.

    `down(prefix, how)` makes a service fail: "timeout", "connection", "garbage" (200 with a body
    that is not JSON) or an HTTP status ("503", "429", "401"). A call nobody serves is recorded
    in `unexpected` and fails the test."""

    Timeout = requests.Timeout
    RequestException = requests.RequestException

    def __init__(self):
        self.services: list[tuple[str, object]] = []
        self.outages: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.unexpected: list[str] = []

    def serve(self, prefix: str, handler) -> None:
        self.services.append((prefix, handler))

    def down(self, prefix: str, how: str = "timeout") -> None:
        self.outages[prefix] = how

    def up(self, prefix: str) -> None:
        self.outages.pop(prefix, None)

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        return self._dispatch("GET", url, params or {}, None, headers or {})

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        return self._dispatch("POST", url, {}, json, headers or {})

    def delete(self, url, headers=None, timeout=None, allow_redirects=True):
        return self._dispatch("DELETE", url, {}, None, headers or {})

    def Session(self):  # noqa: N802 - stands in for `requests.Session` (the guarded calls to staff-chosen URLs)
        return _FakeSession(self)

    def _dispatch(self, method, url, params, body, headers):
        self.calls.append((method, url))
        for prefix, how in self.outages.items():
            if url.startswith(prefix):
                if how == "timeout":
                    raise requests.Timeout("timed out")
                if how == "connection":
                    raise requests.ConnectionError("refused")
                if how == "garbage":
                    return FakeResponse(status=200, invalid_json=True)
                return FakeResponse({"error": "down"}, status=int(how))
        for prefix, handler in self.services:
            if url.startswith(prefix):
                try:
                    answer = handler(method, url, params, body, headers)
                except Exception as exc:  # a fake that breaks must fail the test, not read as an outage
                    self.unexpected.append(f"{method} {url}: the fake raised {type(exc).__name__}: {exc}")
                    raise AssertionError(self.unexpected[-1]) from exc
                return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)
        self.unexpected.append(f"{method} {url}")
        raise AssertionError(f"a call to the outside nobody serves: {method} {url}")

    def count(self, prefix: str) -> int:
        return sum(1 for _m, url in self.calls if url.startswith(prefix))


class FxMarket:
    """Bitstamp's public ticker and Coinbase's spot price."""

    def __init__(self, internet: FakeInternet, prices=PRICES):
        self.prices = dict(prices)
        internet.serve("https://www.bitstamp.net/api/v2/ticker/", self.bitstamp)
        internet.serve("https://api.coinbase.com/v2/prices/", self.coinbase)

    def bitstamp(self, method, url, params, body, headers):
        pair = urlsplit(url).path.rstrip("/").rsplit("/", 1)[1]
        for (asset, fiat), price in self.prices.items():
            if pair == f"{asset}{fiat}".lower():
                return {"last": price, "timestamp": "1794260704"}
        return FakeResponse({"error": "not found"}, status=404)

    def coinbase(self, method, url, params, body, headers):
        m = re.search(r"/v2/prices/([A-Z]+)-([A-Z]+)/spot$", urlsplit(url).path)
        price = self.prices.get((m.group(1), m.group(2)))
        if price is None:
            return FakeResponse({"errors": [{"id": "not_found"}]}, status=404)
        return {"data": {"amount": price, "base": m.group(1), "currency": m.group(2)}}


# ---------------------------------------------------------------------------
# EVM: a JSON-RPC node per network
# ---------------------------------------------------------------------------
def _pad(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


class EvmNode:
    """One network's JSON-RPC endpoint (`EVM_RPC_URLS[network]`), answering what a real node does:
    blocks with timestamps, a `finalized` tag, `eth_getLogs` honouring its address and topic
    filters, receipts with a status, balances and nonces. Blocks are mined by the helpers."""

    def __init__(self, internet: FakeInternet, network: str, *, head: int = 5_000_000, block_s: int = 2):
        self.network, self.url = network, f"https://rpc.{network}.test"
        internet.serve(self.url, self)
        self.head = self.finalized = head
        self.block_s = block_s
        self.t0 = int(timezone.now().timestamp())  # the head's time when the node was made
        self.base = head
        self.blocks: dict[int, dict] = {}
        self.logs: list[dict] = []
        self.receipts: dict[str, dict] = {}
        self.txs: dict[str, dict] = {}
        self.balances: dict[str, int] = {}
        self.nonces: dict[str, int] = {}
        self._hashes = itertools.count(1)

    # -- the wire
    def __call__(self, method, url, params, body, headers):
        name, args = body["method"], body["params"]
        result = getattr(self, "_" + name)(*args)
        return {"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}

    def _eth_blockNumber(self):
        return hex(self.head)

    def _eth_getBlockByNumber(self, tag, full):
        number = self.finalized if tag == "finalized" else int(tag, 16)
        if number > self.head:
            return None
        block = self.blocks.get(number) or {"number": hex(number), "timestamp": hex(self.time_of(number)),
                                            "transactions": []}
        out = dict(block)
        out["transactions"] = list(block["transactions"]) if full else [t["hash"] for t in block["transactions"]]
        return out

    def _eth_getLogs(self, flt):
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        wanted = {a.lower() for a in (flt["address"] if isinstance(flt["address"], list) else [flt["address"]])}
        topics = flt.get("topics") or []
        out = []
        for log in self.logs:
            n = int(log["blockNumber"], 16)
            if not lo <= n <= hi or log["address"].lower() not in wanted:
                continue
            if any(t is not None and i < len(log["topics"]) and log["topics"][i] != t for i, t in enumerate(topics)):
                continue
            out.append(log)
        return out

    def _eth_getTransactionReceipt(self, tx_hash):
        return self.receipts.get(tx_hash.lower())

    def _eth_getTransactionByHash(self, tx_hash):
        return self.txs.get(tx_hash.lower())

    def _eth_getBalance(self, address, _at):
        return hex(self.balances.get(address.lower(), 0))

    def _eth_getTransactionCount(self, address, _at):
        return hex(self.nonces.get(address.lower(), 0))

    # -- the chain
    def time_of(self, number: int) -> int:
        if number in self.blocks:
            return int(self.blocks[number]["timestamp"], 16)
        return self.t0 + (number - self.base) * self.block_s

    def mine(self, *, at: int | None = None) -> int:
        """A new head block, made `at` (a unix time; now by default)."""
        self.head += 1
        stamp = at if at is not None else max(int(timezone.now().timestamp()), self.time_of(self.head - 1) + 1)
        self.blocks[self.head] = {"number": hex(self.head), "timestamp": hex(stamp), "transactions": []}
        return self.head

    def finalize(self) -> None:
        self.finalized = self.head

    def new_hash(self) -> str:
        return "0x" + digest(self.network, "tx", next(self._hashes)).hex()

    def send_token(self, contract: str, to: str, amount: int, *, sender: str, ok: bool = True,
                   at: int | None = None) -> str:
        """An ERC-20 `transfer` in a block of its own. A reverted one has a receipt with status 0x0
        and, as on a real chain, no log."""
        n = self.mine(at=at)
        tx_hash = self.new_hash()
        tx = {"hash": tx_hash, "from": sender.lower(), "to": contract.lower(), "value": "0x0",
              "blockNumber": hex(n), "input": "0xa9059cbb"}
        logs = []
        if ok:
            index = sum(1 for log in self.logs if log["blockNumber"] == hex(n))
            logs = [{"address": contract.lower(), "topics": [evm_chain.TRANSFER_TOPIC, _pad(sender), _pad(to)],
                     "data": "0x" + format(amount, "064x"), "blockNumber": hex(n), "transactionHash": tx_hash,
                     "logIndex": hex(index), "transactionIndex": "0x0", "removed": False}]
            self.logs += logs
        self.receipts[tx_hash] = {"transactionHash": tx_hash, "blockNumber": hex(n), "status": "0x1" if ok else "0x0",
                                  "logs": logs}
        self.txs[tx_hash] = tx
        self.blocks[n]["transactions"].append(tx)
        self.nonces[sender.lower()] = self.nonces.get(sender.lower(), 0) + 1
        return tx_hash

    def send_batch(self, contract: str, legs: list[tuple[str, int]], *, sender: str) -> str:
        """One transaction moving `contract` to several (address, amount) legs: a batch withdrawal,
        one Transfer log per leg."""
        n = self.mine()
        tx_hash = self.new_hash()
        logs = [{"address": contract.lower(), "topics": [evm_chain.TRANSFER_TOPIC, _pad(sender), _pad(to)],
                 "data": "0x" + format(amount, "064x"), "blockNumber": hex(n), "transactionHash": tx_hash,
                 "logIndex": hex(i), "transactionIndex": "0x0", "removed": False} for i, (to, amount) in enumerate(legs)]
        self.logs += logs
        tx = {"hash": tx_hash, "from": sender.lower(), "to": contract.lower(), "value": "0x0", "blockNumber": hex(n),
              "input": "0x"}
        self.receipts[tx_hash] = {"transactionHash": tx_hash, "blockNumber": hex(n), "status": "0x1", "logs": logs}
        self.txs[tx_hash] = tx
        self.blocks[n]["transactions"].append(tx)
        return tx_hash

    def send_native(self, to: str, value: int, *, sender: str, ok: bool = True, at: int | None = None) -> str:
        n = self.mine(at=at)
        tx_hash = self.new_hash()
        tx = {"hash": tx_hash, "from": sender.lower(), "to": to.lower(), "value": hex(value),
              "blockNumber": hex(n), "input": "0x"}
        self.receipts[tx_hash] = {"transactionHash": tx_hash, "blockNumber": hex(n), "status": "0x1" if ok else "0x0",
                                  "logs": []}
        self.txs[tx_hash] = tx
        self.blocks[n]["transactions"].append(tx)
        self.nonces[sender.lower()] = self.nonces.get(sender.lower(), 0) + 1
        if ok:
            self.balances[to.lower()] = self.balances.get(to.lower(), 0) + value
        return tx_hash


# ---------------------------------------------------------------------------
# Tron: TronGrid
# ---------------------------------------------------------------------------
class TronGrid:
    """TronGrid's v1 API: an account's TRC-20 transfers, and one transaction's events, in the
    shapes of fixtures/tron/. Only confirmed transfers are listed when `only_confirmed` is asked."""

    def __init__(self, internet: FakeInternet, base: str = "https://api.trongrid.io"):
        self.base = base
        internet.serve(base + "/v1/", self)
        self.rows: list[dict] = []
        self._ids = itertools.count(1)

    def __call__(self, method, url, params, body, headers):
        path = urlsplit(url).path
        confirmed_only = params.get("only_confirmed") == "true"
        m = re.fullmatch(r"/v1/accounts/([^/]+)/transactions/trc20", path)
        if m:
            data = [r for r in self.rows if r["to"] == m.group(1) and r["contract"] == params["contract_address"]
                    and r["ts"] >= int(params.get("min_timestamp", 0)) and (r["confirmed"] or not confirmed_only)]
            data.sort(key=lambda r: r["ts"])
            return {"data": [self._listed(r) for r in data], "success": True,
                    "meta": {"at": int(time.time() * 1000), "page_size": len(data)}}
        m = re.fullmatch(r"/v1/transactions/([0-9a-f]{64})/events", path)
        if m:
            data = [self._event(r, i) for i, r in enumerate(x for x in self.rows if x["tx"] == m.group(1))
                    if r["confirmed"] or not confirmed_only]
            return {"data": data, "success": True, "meta": {"at": int(time.time() * 1000), "page_size": len(data)}}
        return FakeResponse({"success": False, "error": "no such route"}, status=404)

    @staticmethod
    def _listed(r):
        return {"transaction_id": r["tx"], "token_info": {"symbol": "USDT", "address": r["contract"], "decimals": 6,
                                                          "name": "Tether USD"},
                "block_timestamp": r["ts"], "from": r["from"], "to": r["to"], "type": "Transfer",
                "value": str(r["amount"])}

    @staticmethod
    def _event(r, i):
        return {"block_number": r["block"], "block_timestamp": r["ts"], "caller_contract_address": r["contract"],
                "contract_address": r["contract"], "event_index": i, "event_name": "Transfer",
                "result": {"from": tron_hex(r["from"]), "to": tron_hex(r["to"]), "value": str(r["amount"])},
                "event": "Transfer(address indexed from, address indexed to, uint256 value)",
                "transaction_id": r["tx"]}

    def send(self, contract: str, to: str, amount: int, *, sender: str, confirmed: bool = True,
             at_ms: int | None = None, tx: str | None = None) -> str:
        tx = tx or digest("tron", "tx", next(self._ids)).hex()
        ts = at_ms if at_ms is not None else int(timezone.now().timestamp() * 1000)
        self.rows.append({"tx": tx, "contract": contract, "from": sender, "to": to, "amount": amount, "ts": ts,
                          "confirmed": confirmed, "block": 77_000_000 + len(self.rows)})
        return tx

    def confirm(self, tx: str) -> None:
        for r in self.rows:
            if r["tx"] == tx:
                r["confirmed"] = True


# ---------------------------------------------------------------------------
# Solana: JSON-RPC
# ---------------------------------------------------------------------------
class SolanaRpc:
    """A Solana RPC node at `finalized`: token accounts by owner and mint, signatures per token
    account (newest first, `until` and `before` honoured), parsed transactions with pre/post
    token balances (fixtures/solana/), and signature statuses."""

    def __init__(self, internet: FakeInternet, url: str = "https://solana.rpc.test"):
        self.url = url
        internet.serve(url, self)
        self.token_accounts: dict[tuple[str, str], str] = {}  # (owner, mint) -> token account
        self.sigs: dict[str, list[dict]] = {}  # token account -> newest first
        self.txs: dict[str, dict] = {}
        self.statuses: dict[str, dict | None] = {}
        self.balances: dict[str, int] = {}  # token account -> balance
        self._n = itertools.count(1)
        self.slot = 460_000_000

    def __call__(self, method, url, params, body, headers):
        name, args = body["method"], body["params"]
        if name == "getTokenAccountsByOwner":
            owner, mint = args[0], args[1]["mint"]
            acct = self.token_accounts.get((owner, mint))
            value = [{"pubkey": acct, "account": {"data": {"parsed": {"info": {"mint": mint, "owner": owner}}}}}] if acct else []
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": value}}
        if name == "getSignaturesForAddress":
            listed = list(self.sigs.get(args[0], []))
            opts = args[1]
            names = [s["signature"] for s in listed]
            if opts.get("before") in names:
                listed = listed[names.index(opts["before"]) + 1:]
                names = [s["signature"] for s in listed]
            if opts.get("until") in names:
                listed = listed[:names.index(opts["until"])]
            return {"jsonrpc": "2.0", "id": 1, "result": listed[:opts["limit"]]}
        if name == "getTransaction":
            if args[0] not in self.txs:
                return {"jsonrpc": "2.0", "id": 1, "result": None}
            return {"jsonrpc": "2.0", "id": 1, "result": self.txs[args[0]]}
        if name == "getSignatureStatuses":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot},
                                                          "value": [self.statuses.get(args[0][0])]}}
        raise AssertionError(f"solana method {name} is not faked")

    def account_for(self, owner: str, mint: str) -> str:
        key = (owner, mint)
        if key not in self.token_accounts:
            self.token_accounts[key] = _b58encode(digest("sol-ata", owner, mint))
        return self.token_accounts[key]

    def send(self, mint: str, to_owner: str, amount: int, *, sender: str, err=None,
             at: int | None = None) -> str:
        signature = _b58encode(hashlib.sha512(f"sig/{next(self._n)}".encode()).digest())
        dest, src = self.account_for(to_owner, mint), self.account_for(sender, mint)
        pre_dest, pre_src = self.balances.get(dest, 0), self.balances.get(src, 10**12)
        moved = 0 if err is not None else amount
        post_dest, post_src = pre_dest + moved, pre_src - moved
        self.balances[dest], self.balances[src] = post_dest, post_src
        self.slot += 10

        def bal(index, owner, value):
            return {"accountIndex": index, "mint": mint, "owner": owner,
                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "uiTokenAmount": {"amount": str(value), "decimals": 6, "uiAmountString": str(value / 10**6)}}

        stamp = at if at is not None else int(timezone.now().timestamp())
        self.txs[signature] = {
            "blockTime": stamp, "slot": self.slot,
            "meta": {"err": err, "fee": 5000, "preTokenBalances": [bal(1, sender, pre_src), bal(2, to_owner, pre_dest)],
                     "postTokenBalances": [bal(1, sender, post_src), bal(2, to_owner, post_dest)]},
            "transaction": {"message": {"accountKeys": [{"pubkey": sender}, {"pubkey": src}, {"pubkey": dest}]},
                            "signatures": [signature]},
        }
        entry = {"signature": signature, "slot": self.slot, "err": err, "blockTime": stamp,
                 "confirmationStatus": "finalized", "memo": None}
        for acct in (dest, src):
            self.sigs.setdefault(acct, []).insert(0, dict(entry))
        self.statuses[signature] = {"slot": self.slot, "confirmations": None, "err": err,
                                    "confirmationStatus": "finalized"}
        return signature


# ---------------------------------------------------------------------------
# TON: toncenter v3
# ---------------------------------------------------------------------------
def ton_raw_upper(address: str) -> str:
    from apps.rails.addresses import normalize

    text = address.lower() if re.fullmatch(r"-?\d+:[0-9A-Fa-f]{64}", address) else address
    wc, _, h = normalize("ton", text).partition(":")
    return f"{wc}:{h.upper()}"


class TonCenter:
    """toncenter v3: `/jetton/transfers` (by owner and direction, or by time window) and
    `/transactions?hash=`, in the shapes of fixtures/ton/ (raw addresses in upper case, base64
    transaction hashes)."""

    def __init__(self, internet: FakeInternet, base: str = "https://toncenter.com/api/v3"):
        internet.serve(base + "/", self)
        self.rows: list[dict] = []
        self._n = itertools.count(1)

    def __call__(self, method, url, params, body, headers):
        path = urlsplit(url).path
        if path.endswith("/jetton/transfers"):
            rows = [r for r in self.rows if ton_raw_upper(r["jetton_master"]) == ton_raw_upper(params["jetton_master"])
                    and r["transaction_now"] >= int(params.get("start_utime", 0))
                    and r["transaction_now"] <= int(params.get("end_utime", 2**40))]
            if "owner_address" in params:
                rows = [r for r in rows if ton_raw_upper(r["destination"]) == ton_raw_upper(params["owner_address"])]
            rows.sort(key=lambda r: r["transaction_now"])
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
            return {"jetton_transfers": rows[offset:offset + limit], "address_book": {}, "metadata": {}}
        if path.endswith("/transactions"):
            want = params["hash"]
            found = [r for r in self.rows if base64.b64decode(r["transaction_hash"]).hex() == want]
            return {"transactions": [{"hash": found[0]["transaction_hash"], "now": found[0]["transaction_now"]}] if found
                    else [], "address_book": {}}
        return FakeResponse({"error": "not found"}, status=404)

    def send(self, master: str, to: str, amount: int, *, sender: str, comment: str = "", aborted: bool = False,
             at: int | None = None) -> str:
        """A jetton transfer; returns its hash as lowercase hex (what the system stores)."""
        raw = digest("ton", "tx", next(self._n))
        self.rows.append({
            "query_id": "1", "source": ton_raw_upper(sender), "destination": ton_raw_upper(to), "amount": str(amount),
            "source_wallet": ton_raw_upper("0:" + digest("ton", "wallet", sender).hex()),
            "jetton_master": ton_raw_upper(master), "transaction_hash": base64.b64encode(raw).decode(),
            "transaction_lt": "1", "transaction_now": at if at is not None else int(timezone.now().timestamp()),
            "transaction_aborted": aborted, "response_destination": ton_raw_upper(sender),
            "custom_payload": None, "decoded_custom_payload": None, "forward_ton_amount": "1",
            "forward_payload": None,
            "decoded_forward_payload": {"@type": "text_comment", "comment": comment, "type": "text_comment"} if comment
            else None,
            "trace_id": "x",
        })
        return raw.hex()


# ---------------------------------------------------------------------------
# BTCPay Server (Greenfield)
# ---------------------------------------------------------------------------
class BTCPayServer:
    """A BTCPay store: invoices created with a fiat amount, priced per payment method at `rates`,
    re-read by id. `pay()` and `status()` move an invoice the way BTCPay does."""

    METHOD_IDS = {"BTC": "BTC-CHAIN", "XMR": "XMR-CHAIN", "ZEC": "ZEC-CHAIN"}
    SCHEMES = {"BTC": "bitcoin", "XMR": "monero", "ZEC": "zcash"}
    DECIMALS = {"BTC": 8, "XMR": 12, "ZEC": 8}

    def __init__(self, internet: FakeInternet, *, base="https://btcpay.example", store="store1", api_key="k",
                 rates=None):
        self.base, self.store, self.api_key = base, store, api_key
        self.rates = rates or {"BTC": Decimal("60000"), "XMR": Decimal("150"), "ZEC": Decimal("40")}
        internet.serve(f"{base}/api/v1/stores/{store}/invoices", self)
        self.invoices: dict[str, dict] = {}
        self._n = itertools.count(1)
        self.unauthorized = 0

    def __call__(self, method, url, params, body, headers):
        if headers.get("Authorization") != f"token {self.api_key}":
            self.unauthorized += 1
            return FakeResponse({"code": "unauthenticated"}, status=401)
        rest = urlsplit(url).path.split("/invoices", 1)[1].strip("/")
        if method == "POST" and not rest:
            return self._create(body)
        parts = rest.split("/")
        inv = self.invoices.get(parts[0])
        if inv is None:
            return FakeResponse({"code": "invoice-not-found"}, status=404)
        if len(parts) == 1:
            return {"id": inv["id"], "storeId": self.store, "status": inv["status"],
                    "additionalStatus": inv["additional"], "amount": inv["amount"], "currency": inv["currency"],
                    "checkoutLink": inv["checkout"], "expirationTime": inv["expires"]}
        if parts[1] == "payment-methods":
            return [dict(m) for m in inv["methods"]]
        return FakeResponse({"code": "not-found"}, status=404)

    def _create(self, body):
        eid = f"BTCP{next(self._n):04d}"
        fiat = Decimal(body["amount"])
        methods = []
        for asset in body["checkout"]["paymentMethods"]:
            q = Decimal(10) ** -self.DECIMALS[asset]
            due = (fiat / self.rates[asset]).quantize(q, rounding="ROUND_CEILING")
            methods.append({"paymentMethodId": self.METHOD_IDS[asset], "amount": format(due, "f"),
                            "rate": format(self.rates[asset], "f"), "paymentMethodPaid": "0",
                            "paymentLink": f"{self.SCHEMES[asset]}:addr{eid}?amount={due}", "destination": f"addr{eid}"})
        self.invoices[eid] = {"id": eid, "status": "New", "additional": "None", "amount": body["amount"],
                              "currency": body["currency"], "checkout": f"{self.base}/i/{eid}",
                              "expires": int(timezone.now().timestamp()) + 1800, "methods": methods, "metadata": body["metadata"]}
        return {"id": eid, "storeId": self.store, "checkoutLink": f"{self.base}/i/{eid}",
                "expirationTime": int(timezone.now().timestamp()) + 1800, "status": "New"}

    def due(self, eid: str, method_id: str | None = None) -> Decimal:
        methods = self.invoices[eid]["methods"]
        m = next(m for m in methods if method_id is None or m["paymentMethodId"] == method_id)
        return Decimal(m["amount"])

    def pay(self, eid: str, paid: Decimal, *, status: str = "Settled", additional: str = "None",
            method_id: str | None = None) -> None:
        inv = self.invoices[eid]
        target = method_id or inv["methods"][0]["paymentMethodId"]
        if not any(m["paymentMethodId"] == target for m in inv["methods"]):
            inv["methods"].append({"paymentMethodId": target, "amount": "0", "rate": "1", "paymentMethodPaid": "0",
                                   "paymentLink": "", "destination": ""})
        for m in inv["methods"]:
            if m["paymentMethodId"] == target:
                m["paymentMethodPaid"] = format(paid, "f")
        inv["status"], inv["additional"] = status, additional


# ---------------------------------------------------------------------------
# Lightning: an Alby Hub over NWC
# ---------------------------------------------------------------------------
class FakeHub:
    """What `nostr_sdk.NostrWalletConnect(uri)` returns: a wallet that makes invoices and says what
    became of them. `state` per payment hash: PENDING, ACCEPTED (a hold), SETTLED or EXPIRED.
    `offline` makes every request fail the way an unreachable relay does."""

    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.offline = False
        self.made = []
        self._n = itertools.count(1)

    def connect(self, uri):
        return self

    async def make_invoice(self, req):
        if self.offline:
            raise TimeoutError("relay did not answer")
        payment_hash = digest("ln", next(self._n)).hex()
        self.made.append(req)
        self.invoices[payment_hash] = {"amount": req.amount, "state": "PENDING", "settled_msat": 0}
        return types.SimpleNamespace(invoice=f"lnbc{req.amount // 1000}n1p{payment_hash[:20]}",
                                     payment_hash=payment_hash)

    async def lookup_invoice(self, req):
        if self.offline:
            raise TimeoutError("relay did not answer")
        inv = self.invoices[req.payment_hash]
        return types.SimpleNamespace(state=types.SimpleNamespace(name=inv["state"]),
                                     amount=inv["settled_msat"] if inv["state"] == "SETTLED" else inv["amount"],
                                     payment_hash=req.payment_hash)

    def settle(self, payment_hash: str, msat: int | None = None, state: str = "SETTLED") -> None:
        inv = self.invoices[payment_hash]
        inv["state"] = state
        inv["settled_msat"] = inv["amount"] if msat is None else msat


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
class Clock:
    """Wall time for the code under test, starting now: `advance(seconds)` moves it. Replaces
    `django.utils.timezone.now` (models, quotes, matching, EVM finality) and the `time` module the
    Tron and TON watchers read."""

    def __init__(self, test: TestCase):
        self.at = timezone.now()
        test.enterContext(mock.patch("django.utils.timezone.now", side_effect=lambda: self.at))
        fake_time = types.SimpleNamespace(time=lambda: self.at.timestamp(), monotonic=time.monotonic,
                                          sleep=time.sleep)
        test.enterContext(mock.patch("apps.rails.chains.tron.time", fake_time))
        test.enterContext(mock.patch("apps.rails.chains.ton.time", fake_time))

    def advance(self, seconds: float) -> None:
        self.at += datetime.timedelta(seconds=seconds)

    def unix(self) -> int:
        return int(self.at.timestamp())


# ---------------------------------------------------------------------------
# The test base
# ---------------------------------------------------------------------------
class E2EBase(UnsealedVaultMixin, TestCase):
    """An organization with a bank account and a webhook endpoint, an API token, the worker's
    jobs registered, and the outside world faked (see the module docstring)."""

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            RAILS_HTTP_ENABLED=True, STAFF_NOTIFY_EMAILS=[STAFF], SITE_URL=SITE,
            EVM_RPC_URLS={k: [f"https://rpc.{k}.test"] for k, n in NETWORKS.items() if n.family == "evm"},
            SOLANA_RPC_URLS=["https://solana.rpc.test"], TRONGRID_API_URL="https://api.trongrid.io",
            TONCENTER_API_URL="https://toncenter.com/api/v3", TRONGRID_API_KEY="", TONCENTER_API_KEY="",
        ))
        self.internet = FakeInternet()
        self.enterContext(mock.patch.object(rails_http, "requests", self.internet))
        self.addCleanup(lambda: self.assertEqual(self.internet.unexpected, [], "calls nobody serves"))
        self.fx = FxMarket(self.internet)
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=PUBLIC_ADDRINFO))
        self.received_hooks: list[tuple[str, bytes, dict]] = []
        self.enterContext(mock.patch("apps.webhooks.delivery.post", side_effect=self._receive_hook))
        self.org = Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.", address="Main 1",
                                               email_from="billing@seller.example")
        self.bank = add_bank(self.org)
        self.endpoint, self.hook_secret = webhook_delivery.create_endpoint(self.org, HOOK_URL, events=["*"])
        self.api, self.token = self.api_client(("read", "invoices:write"))
        worker.reset()
        core_jobs.register_all()
        self.addCleanup(worker.reset)
        mail.outbox = []

    # -- the outside world
    def _receive_hook(self, url, body, headers):
        self.received_hooks.append((url, body, dict(headers)))
        return 200

    def evm_node(self, network: str, **kw) -> EvmNode:
        return EvmNode(self.internet, network, **kw)

    # -- the API
    def api_client(self, scopes):
        token, plain = ApiToken.issue(self.org, f"t{ApiToken.objects.count()}", list(scopes))
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {plain}")
        return client, token

    def api_post(self, path, body=None, client=None):
        return (client or self.api).post(path, json.dumps(body or {}), content_type="application/json")

    def create_invoice(self, methods: list[str], *, total_minor: int = 10000, currency: str = "EUR",
                       email: str = CLIENT, title: str = "Residency file") -> int:
        r = self.api_post("/api/v1/invoices", {
            "contact": {"email": email, "legal_name": CLIENT_NAME, "preferred_language": "en"},
            "currency": currency, "title": title, "due_days": 14, "offered_methods": methods,
            "lines": [{"name": "Service", "quantity": "1", "unit_price_minor": total_minor}],
        })
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def send_invoice(self, invoice_pk: int, *, email_client: bool = True) -> str:
        """Sends through the API; returns the pay page's path from the `pay_url` it hands back."""
        r = self.api_post(f"/api/v1/invoices/{invoice_pk}/send", {"email_client": email_client})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "sent")
        return urlsplit(r.json()["pay_url"]).path

    def invoice(self, methods, **kw) -> tuple[int, str]:
        pk = self.create_invoice(methods, **{k: v for k, v in kw.items() if k != "email_client"})
        return pk, self.send_invoice(pk, email_client=kw.get("email_client", False))

    # -- staff
    def staff_client(self, *, superuser: bool = False):
        """A staff member signed in with password and TOTP, on a browser of their own (a superuser
        may also use the Django admin)."""
        from apps.staff.testing import verified_staff_client

        return verified_staff_client(superuser=superuser)

    def declare(self, pay_path: str, network: str, tx_hash: str, note: str = "paid from my wallet"):
        """The client presses "I have paid" on the pay page."""
        r = self.client.post(pay_path + "declare", {"network": network, "tx_hash": tx_hash, "note": note})
        self.assertEqual(r.status_code, 302, r.content[:500])
        return r

    # -- the worker
    def work(self, *names: str) -> None:
        """Run registered worker jobs by name, in the order given (as the worker would)."""
        for name in names:
            worker._jobs[name]["fn"]()

    def run_worker_once(self) -> str:
        out = io.StringIO()
        call_command("run_worker", "--once", stdout=out)
        return out.getvalue()

    def deliver_webhooks(self) -> list[dict]:
        """Run the webhook flush job until nothing is due; returns every body the receiver has had
        so far, each checked (signature, thin payload)."""
        for _ in range(100):
            if not WebhookDelivery.objects.filter(state="queued").exists():
                break
            self.work("webhooks_flush")
        self.assertFalse(WebhookDelivery.objects.filter(state="queued").exists())
        self.assertTrue(all(url == HOOK_URL for url, _b, _h in self.received_hooks))
        return [self.check_hook(body, headers) for _url, body, headers in self.received_hooks]

    def check_hook(self, body: bytes, headers: dict) -> dict:
        """A delivery the receiver can verify with its secret, and a thin payload."""
        expected = "sha256=" + hmac.new(self.hook_secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(headers["X-Pay-Signature"], expected), "bad webhook signature")
        payload = json.loads(body)
        self.assertEqual(set(payload), {"id", "type", "created_at", "org", "data"})
        self.assertEqual(headers["X-Pay-Event"], payload["type"])
        self.assertEqual(headers["X-Pay-Event-Id"], payload["id"])
        self.assertLessEqual(set(payload["data"]), THIN_DATA_KEYS, payload)
        for value in payload["data"].values():
            for item in value if isinstance(value, list) else [value]:
                self.assertIsInstance(item, (int, str, bool, type(None)))
        return payload

    def assertHooksThin(self, payloads, *secrets: str) -> None:
        text = json.dumps(payloads)
        for secret in secrets:
            if secret:
                self.assertNotIn(str(secret).lower(), text.lower(), "a webhook carried client or chain data")

    # -- the client
    def letter_pay_path(self) -> str:
        """Flush the outbox, read the invoice letter the client got, return its pay link's path."""
        self.work("outbox")
        letters = [m for m in mail.outbox if m.to == [CLIENT] and "Invoice" in m.subject]
        self.assertEqual(len(letters), 1, [m.subject for m in mail.outbox])
        letter = letters[0]
        self.assertTrue(any(name.endswith(".pdf") for name, _c, _t in letter.attachments))
        found = re.search(re.escape(SITE) + r"(/p/[A-Za-z0-9_-]+/)", letter.body)
        self.assertIsNotNone(found, letter.body)
        view = re.search(re.escape(SITE) + r"(/i/[A-Za-z0-9_-]+/)", letter.body)
        self.assertIsNotNone(view)
        return found.group(1)

    def quote(self, pay_path: str, method: str, network: str, asset: str, *, refresh: bool = False) -> Quote:
        """Press "get an amount" on the pay page; returns the quote it shows."""
        from apps.invoices import tokens

        data = {"method": method, "network": network, "asset": asset}
        if refresh:
            data["refresh"] = "1"
        r = self.client.post(pay_path + "quote/", data, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'class="error"')
        invoice = tokens.resolve(pay_path.strip("/").split("/")[1], "pay")
        quote = (Quote.objects.filter(invoice_id=invoice.pk, rail_account__pk=int(method.split(":")[1]),
                                      network=network, asset=asset, state="open").order_by("-pk").first())
        self.assertIsNotNone(quote)
        from apps.portal.crypto import decimals_for
        from apps.rails.uris import decimal_amount

        decimals = decimals_for(quote.network, quote.asset)
        self.assertContains(r, f"{decimal_amount(int(quote.amount_base_units), decimals)} {quote.asset}")
        if quote.address:
            self.assertContains(r, quote.address)
        return quote

    def pay_page(self, pay_path: str) -> str:
        r = self.client.get(pay_path)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def status_text(self, pay_path: str) -> str:
        r = self.client.get(pay_path + "status")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # -- what the system shows
    def inv(self, pk: int) -> Invoice:
        return Invoice.objects.get(pk=pk)

    def payments(self, invoice_pk=None, **flt):
        rows = Payment.objects.all() if invoice_pk is None else Payment.objects.filter(invoice_id=invoice_pk)
        return list(rows.filter(**flt).order_by("pk"))

    def actions(self, obj_model: str, pk) -> list[str]:
        return list(AuditEntry.objects.filter(model_label=obj_model, object_pk=str(pk))
                    .order_by("pk").values_list("action", flat=True))

    def csv_rows(self, kind: str = "payments") -> list[dict]:
        today = timezone.now().date()
        since = today - datetime.timedelta(days=30)
        r = self.api.get(f"/api/v1/exports/{kind}.csv", {"from": since.isoformat(), "to": today.isoformat()})
        self.assertEqual(r.status_code, 200)
        text = b"".join(r.streaming_content).decode()
        return list(csv.DictReader(io.StringIO(text)))

    def staff_letters(self, kind: str) -> list[OutboxMessage]:
        return list(OutboxMessage.objects.filter(kind=kind, recipient="staff").order_by("pk"))

    def assertPaidEverywhere(self, invoice_pk: int, payment: Payment, pay_path: str, *, status: str = "paid",
                             secrets=(), csv_amount: str | None = None) -> None:
        """Everything a settled payment must leave behind, seen from outside where possible."""
        invoice = self.inv(invoice_pk)
        self.assertEqual(invoice.status, status)
        self.assertIsNotNone(invoice.paid_at)
        payment = Payment.objects.get(pk=payment.pk)
        self.assertEqual((payment.state, payment.invoice_id), ("confirmed", invoice_pk))
        self.assertTrue(payment.applied)
        if payment.quote_id:
            self.assertEqual(Quote.objects.get(pk=payment.quote_id).state, "paid")
        self.assertFalse(Quote.objects.filter(invoice_id=invoice_pk, state="open").exists())
        # The receipt: queued for the contact, then sent with the PDF.
        receipts = OutboxMessage.objects.filter(kind="receipt", invoice_id=invoice_pk, recipient="contact")
        self.assertEqual(receipts.count(), 1)
        self.work("outbox")
        got = [m for m in mail.outbox if m.to == [CLIENT] and m.subject.startswith(f"Receipt for {invoice.number} ")]
        self.assertEqual(len(got), 1, [m.subject for m in mail.outbox])
        self.assertTrue(any(name.endswith(".pdf") for name, _c, _t in got[0].attachments))
        # The webhooks: signed, thin, and saying what happened.
        payloads = self.deliver_webhooks()
        kinds = [(p["type"], p["data"].get("payment_id"), p["data"].get("invoice_id")) for p in payloads]
        self.assertIn(("payment.confirmed", payment.pk, invoice_pk), kinds)
        self.assertIn((f"invoice.{status}", None, invoice_pk), kinds)
        self.assertHooksThin(payloads, CLIENT, CLIENT_NAME, payment.tx_hash, payment.to_address,
                             payment.from_address, payment.amount_base_units if len(payment.amount_base_units or "") > 3 else "")
        # The audit trail.
        actions = self.actions("payments.payment", payment.pk)
        self.assertTrue({"payment.confirm", "payment.record"} & set(actions), actions)
        invoice_actions = self.actions("invoices.invoice", invoice_pk)
        self.assertIn("invoice.create", invoice_actions)
        self.assertIn("invoice.send", invoice_actions)
        # The books.
        rows = [row for row in self.csv_rows() if row["payment_id"] == str(payment.pk)]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["state"], row["rail"], row["network"], row["invoice_number"]),
                         ("confirmed", payment.rail, payment.network, invoice.number))
        self.assertEqual(row["tx_hash"], payment.tx_hash)
        if csv_amount is not None:
            self.assertEqual(row["amount"], csv_amount)
        self.assertNotEqual(row["confirmed_at"], "")
        # The API, as an integration reads it.
        self.assertEqual(self.api.get(f"/api/v1/invoices/{invoice_pk}").json()["status"], status)
        listed = {p["id"]: p for p in self.api.get("/api/v1/payments", {"limit": 200}).json()["results"]}
        self.assertEqual((listed[payment.pk]["state"], listed[payment.pk]["invoice_id"]), ("confirmed", invoice_pk))
        # The client's page.
        self.assertIn("Paid. Thank you.", self.pay_page(pay_path))
        self.assertIn("Paid. Thank you.", self.status_text(pay_path))

    def assertNotSettled(self, invoice_pk: int, pay_path: str | None = None, *, status: str = "sent") -> None:
        """No money counted: the invoice as it was, no receipt, no paid event."""
        self.assertEqual(self.inv(invoice_pk).status, status)
        self.assertFalse(Payment.objects.filter(invoice_id=invoice_pk, state="confirmed").exists())
        self.assertFalse(OutboxMessage.objects.filter(kind="receipt", invoice_id=invoice_pk).exists())
        from apps.webhooks.models import WebhookEvent

        self.assertFalse(WebhookEvent.objects.filter(type__in=("invoice.paid", "invoice.overpaid"),
                                                     invoice_id=invoice_pk).exists())
        if pay_path:
            self.assertNotIn("Paid. Thank you.", self.status_text(pay_path))

    def assertInReview(self, payment: Payment, reason: str, *, invoice_pk=None, pay_path=None) -> Payment:
        """In the review queue for `reason`, staff told by e-mail and by webhook, and the client
        told a person is checking (when the payment is tied to their invoice)."""
        p = Payment.objects.get(pk=payment.pk)
        self.assertEqual((p.state, p.review_reason), ("needs_review", reason))
        self.assertEqual(p.invoice_id, invoice_pk)
        self.assertTrue(OutboxMessage.objects.filter(kind="review_staff", recipient="staff",
                                                     plain_context__payment_id=p.pk,
                                                     plain_context__reason=reason).exists())
        from apps.webhooks.models import WebhookEvent

        self.assertTrue(WebhookEvent.objects.filter(type="payment.needs_review", payment_id=p.pk).exists())
        self.assertIn("payment.review", self.actions("payments.payment", p.pk))
        if pay_path and invoice_pk:
            self.assertIn("Payment received, our staff are checking it", self.status_text(pay_path))
        return p

    def health(self, name: str) -> WatcherHealth | None:
        return WatcherHealth.objects.filter(name=name).first()
