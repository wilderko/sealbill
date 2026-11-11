"""EVM watcher: allow-listed ERC-20 transfers and native ETH, read from public JSON-RPC.

Reports what the chain shows and nothing else; matching decides what it settles. Every
failure to look is `Unavailable`, never an empty answer. Nothing here logs an address, an
amount or a hash: only the network and the kind of failure.

Identity of a transfer is `(tx_hash, log_index)`: the lowercase 0x hash, and the log's place
among the logs of its transaction (its receipt) for a token log, -1 for native value. `scan`
and `transfers_in_tx` build it the same way. A reorg that re-includes the transaction in
another block changes the log's block-level `logIndex`, never its place in the transaction, so
one transfer cannot be recorded twice under two identities (audit MONEY-8).

Blocks a person has to check are recorded as `ScanGap` rows with the cursor, never guessed at:
native blocks the walk skipped, blocks no node would list even one at a time and through their
receipts (MONEY-5: spam can do that), and blocks across which the native balance rose with no
transaction to explain it (MONEY-8).
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.rails import fx, health, http
from apps.rails.chains.base import Status, Transfer, scan_back_to
from apps.rails.models import ScanGap, WatchCursor
from apps.rails.networks import NETWORKS, asset_by_contract

logger = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
LOG_CHUNK = 2000
MIN_LOG_CHUNK = 100
# Calls one scan may spend narrowing ranges refused at MIN_LOG_CHUNK (per contract, then down to
# single blocks and their receipts). A spam block costs about fifteen; a node that refuses nearly
# every query would cost thousands a minute, and is treated as one that cannot answer instead.
SPLIT_BUDGET = 600
MAX_BLOCKS_PER_RUN = 10000
# Re-read per scan: a lagging node may clip getLogs at its own head. Measured in time, because
# block times differ fifty-fold (128 blocks was 32 seconds on Arbitrum, less than one scan).
LOG_OVERLAP_SECONDS = 600
# Seconds per block, at the small end of what each chain makes, so the overlap errs on more blocks.
BLOCK_TIME_S = {"ethereum": Decimal("12"), "arbitrum": Decimal("0.25"), "optimism": Decimal("2"),
                "base": Decimal("2"), "polygon": Decimal("2"), "bsc": Decimal("0.45"), "avalanche": Decimal("2")}
MIN_BLOCK_TIME_S = Decimal("0.25")  # a network missing from the table gets the longest overlap
NATIVE_SCAN_MAX_BLOCKS = 2000
NATIVE_SCAN_MAX_SECONDS = 120  # and at most this long a run, whichever comes first (residual R4)
_monotonic = time.monotonic  # the walk's clock (tests replace it)
L2_FAST_USD = 1000
L2_FAST_AGE_S = 300
# A transfer is final only once endpoints of this many different operators each say so (`corroborated`):
# one endpoint alone could invent a transfer, its receipt and a finalized block (audit 2026-11-11).
CORROBORATING_OPERATORS = 2

# Three operators a network, each answering the `finalized` tag (checked 2026-11-11): a transfer is
# final only once two of them agree (`corroborated`), so one of them may be down. cloudflare-eth.com
# does not know `finalized` and polygon-rpc.com answers 401; both are gone.
DEFAULT_RPC_URLS = {
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org", "https://eth-mainnet.public.blastapi.io"],
    "optimism": ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io", "https://optimism.drpc.org"],
    "polygon": ["https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org", "https://1rpc.io/matic"],
    "arbitrum": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"],
    "avalanche": ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc",
                  "https://avalanche.drpc.org"],
    "bsc": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org", "https://bsc.drpc.org"],
    "base": ["https://base-rpc.publicnode.com", "https://mainnet.base.org", "https://base.drpc.org"],
}

GAP_WORDS = {
    "skipped": "skipped %s native block(s) unread",
    "unreadable": "found %s block(s) no node would list; left for a person to check",
    "unexplained_balance": "saw the native balance rise across %s block(s) with no visible transaction; "
                           "left for a person to check",
}

_RANGE_WORDS = ("block range", "range too large", "range is too large", "query returned more than",
                "max block range", "exceed maximum block range", "exceeds the range", "log response size exceeded")


class RangeTooLarge(Exception):
    """The node refused an eth_getLogs span as too big: ask again with a smaller one."""


def rpc_urls(network: str) -> list[str]:
    configured = getattr(settings, "EVM_RPC_URLS", None) or {}
    urls = configured.get(network)
    if isinstance(urls, str):
        urls = [urls]
    return list(urls or DEFAULT_RPC_URLS.get(network, []))


def overlap_blocks(network: str) -> int:
    """How many blocks back from its cursor a log scan reads again: `LOG_OVERLAP_SECONDS` of them."""
    per_block = BLOCK_TIME_S.get(network, MIN_BLOCK_TIME_S)
    return int((Decimal(LOG_OVERLAP_SECONDS) / per_block).to_integral_value(rounding=ROUND_CEILING))


def _hex(n: int) -> str:
    return hex(n)


def _ask(url: str, network: str, method: str, params: list, *, refusals: list | None = None):
    """One JSON-RPC call to one endpoint. An `error` object is a failure of that endpoint
    (`http.Unavailable`), except a too-large range, which is the caller's (`RangeTooLarge`).
    `refusals`, when given, collects the endpoint when it answered and said no (an error object or
    a 4xx), as opposed to one that could not be reached."""
    service = f"evm:{network}"
    try:
        answer = http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                service=service, timeout=20)
    except http.Refused as refused:  # a 4xx from one endpoint says nothing about the next
        if refusals is not None:
            refusals.append(url)
        raise http.Unavailable(service, str(refused.status)) from None
    if not isinstance(answer, dict):
        raise http.Unavailable(service, "bad_answer")
    err = answer.get("error")
    if err:
        msg = str(err.get("message", "") if isinstance(err, dict) else err).lower()
        if method == "eth_getLogs" and any(w in msg for w in _RANGE_WORDS):
            raise RangeTooLarge()
        if refusals is not None:
            refusals.append(url)
        raise http.Unavailable(service, "rpc_error")
    if "result" not in answer:
        raise http.Unavailable(service, "bad_answer")
    return answer["result"]


def _call(network: str, method: str, params: list, *, refusals: list | None = None):
    """One JSON-RPC call, failing over across endpoints (`_ask` on each in turn)."""
    urls = rpc_urls(network)
    if not urls:
        raise http.Unavailable(f"evm:{network}", "no_endpoint")
    return http.first_answer(urls, lambda url: _ask(url, network, method, params, refusals=refusals))


def _operator(url: str) -> str:
    """Who runs an endpoint, near enough: the last two labels of its host (publicnode.com, drpc.org)."""
    host = (urlsplit(url).hostname or url).lower().rstrip(".")
    return ".".join(host.split(".")[-2:])


def operators(network: str) -> list[str]:
    """The configured endpoints of `network`, one per operator, in the configured order."""
    seen, out = set(), []
    for url in rpc_urls(network):
        who = _operator(url)
        if who not in seen:
            seen.add(who)
            out.append(url)
    return out


def _int(value, network: str) -> int:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        raise http.Unavailable(f"evm:{network}", "bad_answer") from None


def _hash(value, network: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise http.Unavailable(f"evm:{network}", "bad_answer")
    return value.lower()


def _pad(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _same_log(a: dict, b: dict) -> bool:
    """The same event: emitted by the same contract with the same topics and data."""
    def shape(log):
        return (str(log.get("address", "")).lower(), str(log.get("data", "")).lower(),
                [str(t).lower() for t in log.get("topics") or []])
    return shape(a) == shape(b)


def _ranges(numbers) -> list[tuple[int, int]]:
    """Block numbers as runs of consecutive blocks, (first, last) each."""
    out: list[list[int]] = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return [(lo, hi) for lo, hi in out]


class _Run:
    """State shared by one scan or lookup: the block-time cache and the finalized block."""

    def __init__(self, network: str, url: str | None = None):
        self.network = network
        self.url = url  # one endpoint only (a second opinion), else every endpoint with failover
        self.net = NETWORKS[network]
        self._times: dict[int, datetime.datetime] = {}
        self._finalized: int | None = None
        self._eth_usd: Decimal | None | bool = False  # False = not asked yet
        self._receipts: dict[str, dict | None] = {}
        self._block_receipts: dict[int, list | None] = {}
        self._split_calls = 0

    def spend_split_call(self) -> None:
        self._split_calls += 1
        if self._split_calls > SPLIT_BUDGET:
            raise self.unavailable("range")  # refuses nearly everything: could not look

    def unavailable(self, kind: str) -> http.Unavailable:
        return http.Unavailable(f"evm:{self.network}", kind)

    def call(self, method: str, params: list, *, refusals: list | None = None):
        if self.url is not None:
            return _ask(self.url, self.network, method, params, refusals=refusals)
        return _call(self.network, method, params, refusals=refusals)

    def receipt(self, tx_hash: str):
        if tx_hash not in self._receipts:
            self._receipts[tx_hash] = self.call("eth_getTransactionReceipt", [tx_hash])
        return self._receipts[tx_hash]

    def place_in_tx(self, log: dict) -> int:
        """The log's place among its transaction's logs: with the hash, its identity."""
        tx = _hash(log.get("transactionHash"), self.network)
        number = _int(log.get("blockNumber"), self.network)
        index = _int(log.get("logIndex"), self.network)
        receipt = self.receipt(tx)
        if receipt is None:  # listed, and its receipt not readable yet: we could not look
            raise self.unavailable("receipt_missing")
        if not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list):
            raise self.unavailable("bad_answer")
        same_block = _int(receipt.get("blockNumber"), self.network) == number and (
            not log.get("blockHash") or not receipt.get("blockHash")
            or str(log["blockHash"]).lower() == str(receipt["blockHash"]).lower())
        if not same_block:
            raise self.unavailable("reorg")  # the nodes disagree on its block: ask again next run
        for place, item in enumerate(receipt["logs"]):
            if not isinstance(item, dict):
                raise self.unavailable("bad_answer")
            if _int(item.get("logIndex"), self.network) == index and _same_log(item, log):
                return place
        raise self.unavailable("reorg")

    def block_receipts(self, number: int) -> list | None:
        """Every receipt of block `number`, or None when every node answered and refused them."""
        if number not in self._block_receipts:
            self.spend_split_call()
            refusals: list[str] = []
            try:
                result = self.call("eth_getBlockReceipts", [_hex(number)], refusals=refusals)
            except http.Unavailable:
                if refusals and len(refusals) == len(rpc_urls(self.network)):
                    self._block_receipts[number] = None
                    return None
                raise  # a node that could not be reached is "could not look", never a gap
            if result is None:
                raise self.unavailable("block_missing")
            if not isinstance(result, list):
                raise self.unavailable("bad_answer")
            self._block_receipts[number] = result
        return self._block_receipts[number]

    def block_at_or_before(self, head: int, when: datetime.datetime) -> int:
        """A block made no later than `when`, found back from `head` with the table's (short)
        block time and checked against the chain's own timestamps. Erring early only means
        reading more blocks."""
        target = int(when.timestamp())
        per = BLOCK_TIME_S.get(self.network, MIN_BLOCK_TIME_S)
        number = head
        for _ in range(64):
            stamp = int(self.block_time(number).timestamp())
            if stamp <= target or number == 0:
                return number
            back = int((Decimal(stamp - target) / per).to_integral_value(rounding=ROUND_CEILING))
            number = max(0, number - max(back, 1))
        raise self.unavailable("bad_answer")  # timestamps that never get older are not a chain

    def block_time(self, number: int) -> datetime.datetime:
        if number not in self._times:
            block = self.call("eth_getBlockByNumber", [_hex(number), False])
            if not isinstance(block, dict):
                raise http.Unavailable(f"evm:{self.network}", "block_missing")
            ts = _int(block.get("timestamp"), self.network)
            try:
                self._times[number] = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            except (OSError, OverflowError, ValueError):
                raise http.Unavailable(f"evm:{self.network}", "bad_answer") from None
        return self._times[number]

    def finalized_number(self) -> int:
        if self._finalized is None:
            block = self.call("eth_getBlockByNumber", ["finalized", False])
            if not isinstance(block, dict):
                raise http.Unavailable(f"evm:{self.network}", "block_missing")
            self._finalized = _int(block.get("number"), self.network)
        return self._finalized

    def _eth_price(self) -> Decimal | None:
        if self._eth_usd is False:
            try:
                self._eth_usd = fx.price_in("ETH", "USD").rate
            except Exception as exc:  # fx being down only means: wait for finalized
                logger.warning("evm %s: no ETH price (%s)", self.network, type(exc).__name__)
                self._eth_usd = None
        return self._eth_usd

    def is_final(self, contract: str, amount: int, block_number: int, block_time) -> bool:
        if block_number <= self.finalized_number():
            return True
        if self.net.finality != "l2":
            return False
        if (timezone.now() - block_time).total_seconds() < L2_FAST_AGE_S:
            return False
        if contract:
            asset = asset_by_contract(self.network, contract)
            if asset is None:
                return False
            usd = Decimal(amount) / Decimal(10) ** asset.decimals
        else:
            price = self._eth_price()
            if price is None:
                return False
            usd = Decimal(amount) / Decimal(10) ** 18 * price
        return usd <= L2_FAST_USD

    def token_transfer(self, log: dict, to_address: str | None = None, place: int | None = None) -> Transfer | None:
        """The allow-listed transfer a Transfer log records, or None. `place` is the log's place
        in its transaction when the caller read it off the receipt; else the receipt is asked."""
        topics = log.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC or log.get("removed"):
            return None
        contract = str(log.get("address", "")).lower()
        if asset_by_contract(self.network, contract) is None:
            return None
        data = log.get("data") or "0x"
        if len(data) != 66:
            return None
        amount = _int(data, self.network)
        if amount == 0:
            return None
        if not all(isinstance(t, str) for t in topics):
            raise http.Unavailable(f"evm:{self.network}", "bad_answer")
        to = _addr_from_topic(topics[2])
        if to_address is not None and to != to_address:
            return None
        number = _int(log.get("blockNumber"), self.network)
        when = self.block_time(number)
        tx = _hash(log.get("transactionHash"), self.network)
        return Transfer(
            network=self.network, contract=contract, to_address=to,
            from_address=_addr_from_topic(topics[1]), amount_base_units=amount, tx_hash=tx,
            log_index=self.place_in_tx(log) if place is None else place,
            block_number=number, block_time=when, final=self.is_final(contract, amount, number, when),
        )

    def native_transfer(self, tx: dict, number: int, when) -> Transfer | None:
        to, value = tx.get("to"), _int(tx.get("value") or "0x0", self.network)
        if not to or value <= 0:
            return None
        return Transfer(
            network=self.network, contract="", to_address=str(to).lower(),
            from_address=str(tx.get("from", "")).lower(), amount_base_units=value,
            tx_hash=_hash(tx.get("hash"), self.network), log_index=-1, block_number=number, block_time=when,
            final=self.is_final("", value, number, when),
        )


def _cursor(network, address, stream):
    return WatchCursor.objects.filter(network=network, address=address, stream=stream).first()


def _save(network, address, stream, position, extra=None):
    WatchCursor.objects.update_or_create(
        network=network, address=address, stream=stream,
        defaults={"position": str(position), "extra": extra or {}},
    )


def _head(network: str) -> int:
    return _int(_call(network, "eth_blockNumber", []), network)


class EvmAdapter:
    def scan(self, account, network: str) -> list[Transfer]:
        net = NETWORKS[network]
        address = account.address.lower()
        enabled = set(account.assets or [])
        contracts = [a.contract.lower() for s, a in net.assets.items() if a.contract and s in enabled]
        native = "ETH" in enabled and "ETH" in net.assets
        run = _Run(network)
        name = f"scan:evm:{network}:{account.pk}"
        found: list[Transfer] = []
        writes: list[tuple] = []
        gaps: list[tuple[int, int, str]] = []
        try:
            if contracts:
                got, w, g = self._scan_logs(run, account, address, contracts)
                found += got
                writes.append(w)
                gaps += g
            if native:
                got, w, g, behind = self._scan_native(run, account, address)
                found += got
                writes.append(w)
                gaps += g
        except http.Unavailable as exc:
            health.failed(name, exc.kind)
            raise
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            health.failed(name, "bad_answer")
            raise http.Unavailable(f"evm:{network}", "bad_answer") from None
        # Both streams read: only now do the cursors move, together, and the blocks a person
        # must check are recorded with them (a durable row a person acknowledges, never a health
        # state the next scan would clear). The same blocks for the same reason are one row,
        # however often an overlap re-read meets them.
        with transaction.atomic():
            for stream, position, extra in writes:
                _save(network, address, stream, position, extra)
            for lo, hi, reason in gaps:
                if not ScanGap.objects.filter(network=network, address=account.address, from_block=lo, to_block=hi,
                                              reason=reason).exists():
                    ScanGap.objects.create(network=network, address=account.address, from_block=lo, to_block=hi,
                                           reason=reason)
        for lo, hi, reason in gaps:
            logger.warning("evm %s: account %s " + GAP_WORDS[reason], network, account.pk, hi - lo + 1)
        if native and behind:  # progress was made; the rest of the first walk follows on the next runs
            health.failed(name, "backlog")
        else:
            health.ok(name)
        return found

    def _scan_logs(self, run: _Run, account, address: str, contracts: list[str]):
        network = run.network
        head = _head(network)
        stream = f"evm-logs:{account.pk}"
        cursor = _cursor(network, address, stream)
        if cursor is None:
            since = scan_back_to(account, network)
            if since is None:  # nothing anyone may be paying: start where the chain stands
                return [], (stream, head, None), []
            start = run.block_at_or_before(head, since)  # a client may have paid before this first look
            end = position = min(head, start + MAX_BLOCKS_PER_RUN)
        else:
            old = int(cursor.position)
            start = max(old - overlap_blocks(network) + 1, 0)
            end = min(head, old + MAX_BLOCKS_PER_RUN)
            position = max(end, old)
        found, unread = self._read_logs(run, start, end, address, contracts)
        return found, (stream, position, None), [(lo, hi, "unreadable") for lo, hi in _ranges(unread)]

    def _read_logs(self, run: _Run, start: int, end: int, address: str, contracts: list[str]):
        """The transfers to `address` in blocks `start` to `end`, and the blocks no node would
        list. A range refused as too large is halved down to `MIN_LOG_CHUNK`; below that it is
        asked for per contract, so one token's spam cannot hide another's payments, and then
        split down to single blocks (`_split`)."""
        padded = _pad(address)
        found: list[Transfer] = []
        unread: set[int] = set()
        chunk = LOG_CHUNK
        while start <= end:
            stop = min(end, start + chunk - 1)
            try:
                listed = [(log, None) for log in self._get_logs(run, start, stop, contracts, padded)]
            except RangeTooLarge:
                if chunk > MIN_LOG_CHUNK:
                    chunk = max(MIN_LOG_CHUNK, chunk // 2)
                    continue
                listed = []
                for contract in contracts:
                    listed += self._split(run, start, stop, [contract], padded, unread)
            for log, place in listed:
                t = run.token_transfer(log, address, place)
                if t is not None:
                    found.append(t)
            start = stop + 1
        return found, unread

    def _get_logs(self, run: _Run, lo: int, hi: int, contracts: list[str], padded: str) -> list:
        logs = _call(run.network, "eth_getLogs", [{
            "fromBlock": _hex(lo), "toBlock": _hex(hi), "address": contracts,
            "topics": [TRANSFER_TOPIC, None, padded]}])
        if not isinstance(logs, list):
            raise run.unavailable("bad_answer")
        return logs

    def _split(self, run: _Run, lo: int, hi: int, contracts: list[str], padded: str, unread: set) -> list:
        """(log, place) pairs for blocks `lo` to `hi`, halving a refused range down to one block;
        a single block still refused is read from its receipts (`_from_receipts`)."""
        run.spend_split_call()
        try:
            return [(log, None) for log in self._get_logs(run, lo, hi, contracts, padded)]
        except RangeTooLarge:
            pass
        if lo == hi:
            return self._from_receipts(run, lo, contracts, padded, unread)
        mid = (lo + hi) // 2
        return (self._split(run, lo, mid, contracts, padded, unread)
                + self._split(run, mid + 1, hi, contracts, padded, unread))

    def _from_receipts(self, run: _Run, number: int, contracts: list[str], padded: str, unread: set) -> list:
        """The Transfer logs to us in one block, filtered here from all its receipts; when every
        node refuses those too, the block goes to `unread` (a gap a person checks) and the scan
        moves on."""
        receipts = run.block_receipts(number)
        if receipts is None:
            unread.add(number)
            return []
        out = []
        for receipt in receipts:
            if not isinstance(receipt, dict) or not isinstance(receipt.get("logs") or [], list):
                raise run.unavailable("bad_answer")
            if receipt.get("status") != "0x1":
                continue
            for place, log in enumerate(receipt.get("logs") or []):
                if not isinstance(log, dict):
                    raise run.unavailable("bad_answer")
                topics = log.get("topics") or []
                if (str(log.get("address", "")).lower() in contracts and len(topics) == 3
                        and str(topics[0]).lower() == TRANSFER_TOPIC and str(topics[2]).lower() == padded):
                    out.append((log, place))
        return out

    def _scan_native(self, run: _Run, account, address: str):
        network = run.network
        head = _head(network)
        at = _hex(head)
        balance = _int(_call(network, "eth_getBalance", [address, at]), network)
        nonce = _int(_call(network, "eth_getTransactionCount", [address, at]), network)
        extra = {"balance": str(balance), "nonce": nonce}
        stream = f"evm-native:{account.pk}"
        cursor = _cursor(network, address, stream)
        if cursor is None:
            since = scan_back_to(account, network)
            if since is None:  # nothing anyone may be paying: start where the chain stands
                return [], (stream, head, extra), [], False
            # The balance seen now may already hold a client's payment: walk back to their quote.
            return self._first_walk(run, address, stream, run.block_at_or_before(head, since), head, extra)
        if (cursor.extra or {}).get("walking"):
            return self._first_walk(run, address, stream, int(cursor.position) + 1, head, extra, cursor.extra)
        if cursor.extra == extra:
            return [], (stream, head, extra), [], False
        # A later look behind by more than one run's walk is walked across runs like the first look,
        # never skipped (final review F7: on Arbitrum any worker downtime over ~8 minutes). The walk
        # carries where it began, the balance before it and what it has found, so the balance check
        # covers the whole backlog once the walk reaches the head.
        start = int(cursor.position) + 1
        before = _stored_balance(cursor.extra)
        walk = {"walking": True, "from": start, "balance": None if before is None else str(before), "found": "0"}
        return self._first_walk(run, address, stream, start, head, extra, walk)

    def _first_walk(self, run: _Run, address: str, stream: str, start: int, head: int, extra: dict,
                    walk: dict | None = None):
        """A walk of the native transfers to us, `NATIVE_SCAN_MAX_BLOCKS` per run and oldest first,
        never skipped: the first look's back to the earliest unreleased quote, and a later look's
        across a backlog. The cursor keeps the last block walked (`{"walking": true, ...}`) and the
        balance and nonce become the baseline only once the walk has reached the head. Until then
        the scan is behind ("backlog").

        `walk` is what the cursor carries: for a later look, where the walk began (`from`), the
        balance before it and the sum it has found so far, so that at the head a rise no transaction
        explains is left for a person, as a one-run look does. A first look has no balance before.

        A run reads at most `NATIVE_SCAN_MAX_BLOCKS` blocks or for `NATIVE_SCAN_MAX_SECONDS`, whichever
        comes first (residual R4), and says so in the log, never with an address, when the chain ran
        further ahead than the walk since the previous run (`behind`)."""
        walk = dict(walk or {"walking": True})
        stop = min(head, start + NATIVE_SCAN_MAX_BLOCKS - 1)
        found, stop = self._walk(run, address, start, stop, deadline=_monotonic() + NATIVE_SCAN_MAX_SECONDS)
        total = int(walk.get("found") or 0) + sum(t.amount_base_units for t in found)
        if stop < head:
            behind, before = head - stop, walk.get("behind")
            if isinstance(before, int) and behind > before:
                logger.warning("evm %s: %s native backlog grew from %s to %s blocks; the walk is falling behind",
                               run.network, stream, before, behind)
            return found, (stream, stop, {**walk, "found": str(total), "behind": behind}), [], True
        gaps: list[tuple[int, int, str]] = []
        before = _stored_balance(walk)
        if before is not None and int(walk.get("from", start)) <= head:
            # The balance rose by more than what reached us in the blocks read: an internal
            # transfer from a contract, a withdrawal, something no transaction shows. Never a
            # payment guessed at: a person looks at those blocks.
            if int(extra["balance"]) - before > total:
                gaps.append((int(walk.get("from", start)), head, "unexplained_balance"))
        return found, (stream, head, extra), gaps, False

    def _walk(self, run: _Run, address: str, start: int, head: int, deadline: float | None = None
              ) -> tuple[list[Transfer], int]:
        """The native transfers to us in blocks `start` to `head`, and the last block read: `head`, or an
        earlier one once `deadline` (on `_monotonic`) has passed. At least one block is read."""
        network = run.network
        found: list[Transfer] = []
        last = start - 1
        for number in range(start, head + 1):
            if deadline is not None and number > start and _monotonic() >= deadline:
                break
            block = _call(network, "eth_getBlockByNumber", [_hex(number), True])
            if not isinstance(block, dict):
                raise http.Unavailable(f"evm:{network}", "block_missing")
            try:
                when = datetime.datetime.fromtimestamp(_int(block.get("timestamp"), network), tz=datetime.timezone.utc)
            except (OSError, OverflowError, ValueError):
                raise http.Unavailable(f"evm:{network}", "bad_answer") from None
            for tx in block.get("transactions") or []:
                if not isinstance(tx, dict):
                    raise http.Unavailable(f"evm:{network}", "bad_answer")
                if str(tx.get("to") or "").lower() != address or _int(tx.get("value") or "0x0", network) <= 0:
                    continue
                receipt = _call(network, "eth_getTransactionReceipt", [tx["hash"]])
                if receipt is None:  # not found is not "not paid": we could not look
                    raise http.Unavailable(f"evm:{network}", "receipt_missing")
                if not isinstance(receipt, dict):
                    raise http.Unavailable(f"evm:{network}", "bad_answer")
                if receipt.get("status") != "0x1":
                    continue
                t = run.native_transfer(tx, number, when)
                if t is not None:
                    found.append(t)
            last = number
        return found, last

    def transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        """The allow-listed transfers of one transaction. A string that is not a transaction hash
        (a client's typo) is answered with nothing, without asking any node: it is not a failure
        of the chain. An answer that cannot be read is `Unavailable("bad_answer")`, as in `scan`."""
        tx_hash = (tx_hash or "").strip()
        if not TX_HASH.fullmatch(tx_hash):
            return []
        try:
            return self._transfers_in_tx(network, tx_hash.lower())
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            raise http.Unavailable(f"evm:{network}", "bad_answer") from None

    def _transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        receipt = _call(network, "eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
            return []
        tx = _call(network, "eth_getTransactionByHash", [tx_hash])
        run = _Run(network)
        found = []
        for place, log in enumerate(receipt.get("logs") or []):
            t = run.token_transfer(log, place=place)
            if t is not None:
                found.append(t)
        if isinstance(tx, dict) and "ETH" in NETWORKS[network].assets:
            number = _int(receipt.get("blockNumber"), network)
            t = run.native_transfer(tx, number, run.block_time(number))
            if t is not None:
                found.append(t)
        return found

    def status(self, payment) -> Status:
        """`final`, `pending` or `dropped` for the transfer behind `payment` (audit MONEY-8).

        A receipt nobody has is "not yet known", never "dropped": a node behind, or one that pruned
        it, answers the same. The transfer is gone only once the block it was seen in is final and
        no longer holds the transaction; a reverted receipt, or one whose logs no longer carry the
        recorded transfer (a reorg re-ran it differently), only from a final block. Matching then
        still wants that answer twice, `DROPPED_AFTER_S` apart. `final` reads the transfer again:
        the receipt must still hold a log of the recorded token, recipient and amount (the native
        transaction, its recipient and value)."""
        network = payment.network
        try:
            return self._status(_Run(network), payment, payment.tx_hash.strip().lower())
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            raise http.Unavailable(f"evm:{network}", "bad_answer") from None

    def _status(self, run: _Run, payment, tx: str) -> Status:
        network = run.network
        receipt = run.call("eth_getTransactionReceipt", [tx])
        if receipt is None:
            return self._not_found(run, payment, tx)
        if not isinstance(receipt, dict):
            raise run.unavailable("bad_answer")
        number = _int(receipt.get("blockNumber"), network)
        try:
            amount = int(payment.amount_base_units or 0)
        except ValueError:
            amount = 0
        if receipt.get("status") != "0x1" or not self._still_there(run, receipt, payment, tx, amount):
            return "dropped" if number <= run.finalized_number() else "pending"
        when = run.block_time(number)
        if not run.is_final(payment.contract or "", amount, number, when):
            return "pending"
        # Final as one endpoint tells it: final for us once another operator's endpoint agrees.
        return "final" if self.corroborated(payment) else "pending"

    def corroborated(self, transfer) -> bool:
        """Whether endpoints of `CORROBORATING_OPERATORS` different operators each hold the transfer as
        it was recorded (a successful receipt, the token log at its place or the native transaction,
        the same recipient and amount) in a block that endpoint itself calls final. One endpoint can be
        compromised or simply lie: it could invent a transfer, a receipt and a finalized block alone
        (audit 2026-11-11, high 2). With fewer operators configured, all of them must agree. An
        endpoint that cannot be reached, or does not agree, is no vote; nothing here says "dropped".
        `transfer` is a `Transfer` or a `Payment` (the same field names)."""
        network = transfer.network
        urls = operators(network)
        need = min(CORROBORATING_OPERATORS, len(urls))
        if need == 0:
            return False
        tx = str(transfer.tx_hash or "").strip().lower()
        try:
            amount = int(transfer.amount_base_units or 0)
        except (TypeError, ValueError):
            return False
        agreed = 0
        for url in urls:
            run = _Run(network, url=url)
            try:
                receipt = run.call("eth_getTransactionReceipt", [tx])
                if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
                    continue
                number = _int(receipt.get("blockNumber"), network)
                if not self._still_there(run, receipt, transfer, tx, amount):
                    continue
                if not run.is_final(transfer.contract or "", amount, number, run.block_time(number)):
                    continue
            except (http.Unavailable, RangeTooLarge, TypeError, ValueError, KeyError, AttributeError, OSError,
                    OverflowError):
                continue
            agreed += 1
            if agreed >= need:
                return True
        logger.info("evm %s: a final transfer is not yet confirmed by %s operators (%s agreed)", network, need, agreed)
        return False

    def _not_found(self, run: _Run, payment, tx: str) -> Status:
        number = payment.block_number
        if number is None or number > run.finalized_number():
            return "pending"  # not yet known: the block it was seen in is not final
        block = run.call("eth_getBlockByNumber", [_hex(number), False])
        if not isinstance(block, dict) or not isinstance(block.get("transactions"), list):
            raise run.unavailable("block_missing" if block is None else "bad_answer")
        listed = {str(t.get("hash") if isinstance(t, dict) else t).lower() for t in block["transactions"]}
        if tx in listed:
            raise run.unavailable("receipt_missing")  # the final block holds it: this node lacks the receipt
        return "dropped"

    def _still_there(self, run: _Run, receipt: dict, payment, tx: str, amount: int) -> bool:
        """Whether the successful receipt still carries the transfer as it was recorded."""
        network = run.network
        to = (payment.to_address or "").strip().lower()
        contract = (payment.contract or "").strip().lower()
        if not contract:
            native = run.call("eth_getTransactionByHash", [tx])
            if native is None:
                raise run.unavailable("tx_missing")
            if not isinstance(native, dict):
                raise run.unavailable("bad_answer")
            return (str(native.get("to") or "").lower() == to
                    and _int(native.get("value") or "0x0", network) == amount)
        logs = receipt.get("logs")
        if not isinstance(logs, list) or not all(isinstance(log, dict) for log in logs):
            raise run.unavailable("bad_answer")
        # The log at the payment's own place (`place_in_tx`), not the first one that looks alike:
        # two identical transfers in one transaction are two payments, and a re-mined receipt that
        # kept only one of them must not leave both standing (independent audit DEF-PAY-01).
        place = payment.log_index
        if not isinstance(place, int) or not 0 <= place < len(logs):
            return False
        log = logs[place]
        topics = log.get("topics") or []
        data = log.get("data") or "0x"
        return (str(log.get("address", "")).lower() == contract and len(topics) == 3 and not log.get("removed")
                and str(topics[0]).lower() == TRANSFER_TOPIC and _addr_from_topic(str(topics[2])) == to
                and len(data) == 66 and _int(data, network) == amount)


def _stored_balance(extra) -> int | None:
    try:
        return int((extra or {})["balance"])
    except (KeyError, TypeError, ValueError):
        return None
