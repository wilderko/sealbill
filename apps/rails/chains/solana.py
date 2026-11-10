"""Solana watcher: allow-listed SPL token transfers, read from public JSON-RPC at `finalized`.

Reports what the chain shows and nothing else. Every failure to look is `Unavailable`, never
an empty answer; nothing here logs an address, an amount or a hash.

A wallet receives tokens into token accounts, and the sender may create one at any time, so
the owner's token accounts are asked for on every scan. Identity of a transfer is
`(signature, account index)`: the signature exactly as given (base58, case-sensitive) and the
index of the receiving token account in the transaction's `accountKeys`. The amount is the
positive change of that account's balance, post minus pre, so a transaction that routes value
through several instructions counts once, as the chain nets it.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.db import transaction

from apps.rails import health, http
from apps.rails.addresses import _b58decode
from apps.rails.chains.base import Status, Transfer, scan_back_to
from apps.rails.models import WatchCursor
from apps.rails.networks import NETWORKS, asset_by_contract

SERVICE = "solana"
SIG_PAGE = 1000
MAX_PAGES = 50  # of SIG_PAGE signatures: listing is cheap, reading each transaction is not
SOLANA_TX_PER_RUN = 25  # getTransaction calls per token account per run: spam cannot stall a scan


def _whole(value) -> int:
    """A whole number sent as text or integer; a float or anything else is a bad answer."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("not a whole number")
    return int(value)


def _is_signature(text: str) -> bool:
    try:
        return len(_b58decode(text)) == 64
    except ValueError:
        return False


def rpc_urls() -> list[str]:
    urls = getattr(settings, "SOLANA_RPC_URLS", None) or []
    return [urls] if isinstance(urls, str) else list(urls)


def _call(method: str, params: list):
    def one(url):
        try:
            answer = http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                    service=SERVICE, timeout=20)
        except http.Refused as refused:
            raise http.Unavailable(SERVICE, str(refused.status)) from None
        if not isinstance(answer, dict) or answer.get("error") or "result" not in answer:
            raise http.Unavailable(SERVICE, "rpc_error" if isinstance(answer, dict) and answer.get("error") else "bad_answer")
        return answer["result"]

    urls = rpc_urls()
    if not urls:
        raise http.Unavailable(SERVICE, "no_endpoint")
    return http.first_answer(urls, one)


def _token_accounts(owner: str, mint: str) -> list[str]:
    result = _call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "finalized"}])
    value = result["value"]
    if not isinstance(value, list):
        raise http.Unavailable(SERVICE, "bad_answer")
    return [str(v["pubkey"]) for v in value]


def _transaction(signature: str):
    result = _call("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized",
                                                   "maxSupportedTransactionVersion": 0}])
    if result is not None and not isinstance(result, dict):
        raise http.Unavailable(SERVICE, "bad_answer")
    return result


def _balances(tx: dict, mint: str | None) -> dict[int, dict]:
    """Per account index: the owner, the mint and the balance change of every token balance."""
    meta = tx["meta"]
    pre = {b["accountIndex"]: _whole(b["uiTokenAmount"]["amount"]) for b in meta.get("preTokenBalances") or []}
    out = {}
    for b in meta.get("postTokenBalances") or []:
        if mint is not None and b["mint"] != mint:
            continue
        idx = b["accountIndex"]
        out[idx] = {"owner": b["owner"], "mint": b["mint"],
                    "delta": _whole(b["uiTokenAmount"]["amount"]) - pre.get(idx, 0)}
    return out


def _transfers(network: str, signature: str, tx: dict, *, owner: str | None, mint: str | None) -> list[Transfer]:
    """The incoming transfers of an allow-listed mint in a successful transaction, to `owner`
    (or to anyone when `owner` is None)."""
    if tx["meta"].get("err") is not None:
        return []
    balances = _balances(tx, mint)
    block_time = datetime.datetime.fromtimestamp(_whole(tx["blockTime"]), tz=datetime.timezone.utc)
    found = []
    for idx, b in balances.items():
        if b["delta"] <= 0 or (owner is not None and b["owner"] != owner):
            continue
        if asset_by_contract(network, b["mint"]) is None:
            continue
        senders = [o for o in balances.values() if o["mint"] == b["mint"] and o["delta"] < 0]
        sender = min(senders, key=lambda o: o["delta"])["owner"] if senders else ""
        found.append(Transfer(
            network=network, contract=b["mint"], to_address=b["owner"], from_address=sender,
            amount_base_units=b["delta"], tx_hash=signature, log_index=idx, block_number=tx.get("slot"),
            block_time=block_time, final=True))
    return found


class SolanaAdapter:
    def scan(self, account, network: str) -> list[Transfer]:
        net = NETWORKS[network]
        owner = account.address
        enabled = set(account.assets or [])
        mints = [(s, a.contract) for s, a in net.assets.items() if a.contract and s in enabled]
        name = f"scan:solana:{network}:{account.pk}"
        found: dict[tuple[str, int], Transfer] = {}
        writes: list[tuple[str, str, dict]] = []
        behind = False
        try:
            for symbol, mint in mints:
                write, more = self._scan_mint(network, account, symbol, mint, found)
                writes.append(write)
                behind = behind or more
        except http.Unavailable as exc:
            health.failed(name, exc.kind)
            raise
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            health.failed(name, "bad_answer")
            raise http.Unavailable(SERVICE, "bad_answer") from None
        with transaction.atomic():  # every mint read: only now do the cursors move, together
            for stream, position, extra in writes:
                WatchCursor.objects.update_or_create(network=network, address=owner, stream=stream,
                                                     defaults={"position": position, "extra": extra})
        if behind:  # progress was made; the rest is read on the next runs
            health.failed(name, "backlog")
        else:
            health.ok(name)
        return list(found.values())

    def _scan_mint(self, network, account, symbol, mint, found):
        """Read what is new for one mint. Returns the cursor write and whether signatures remain.

        On the first sight of the stream (no cursor) each token account's history is read back to
        the earliest unreleased quote on the address, since a client may have paid before this
        first look (audit E2E-2); with no such quote it only remembers where each account stands."""
        owner, pk = account.address, account.pk
        stream = f"sol-{symbol}:{pk}"
        cursor = WatchCursor.objects.filter(network=network, address=owner, stream=stream).first()
        known: dict[str, str] = dict((cursor.extra or {}).get("accounts") or {}) if cursor else {}
        accounts = _token_accounts(owner, mint)  # a sender may have created one since the last scan
        since = scan_back_to(account, network) if cursor is None else None
        baseline = cursor is None and since is None  # nothing anyone may be paying: report nothing
        newest = dict(known)
        behind = False
        for token_account in accounts:
            if since is not None:
                sigs, before = self._signatures_since(token_account, int(since.timestamp()))
                if before is not None:
                    newest[token_account] = before  # where the history this look reads begins
            else:
                sigs = self._signatures(token_account, known.get(token_account), newest_only=baseline)
            if baseline:
                if sigs:
                    newest[token_account] = sigs[0]["signature"]
                continue
            reads = 0
            for sig in reversed(sigs):  # oldest first
                if sig.get("err") is None:
                    if reads >= SOLANA_TX_PER_RUN:
                        behind = True
                        break
                    reads += 1
                    tx = _transaction(sig["signature"])
                    if tx is None:  # listed as finalized and not readable yet: we could not look
                        raise http.Unavailable(SERVICE, "tx_missing")
                    for t in _transfers(network, sig["signature"], tx, owner=owner, mint=mint):
                        found[(t.tx_hash, t.log_index)] = t
                newest[token_account] = sig["signature"]  # fully processed
        last = next(iter(newest.values()), "")
        return (stream, last, {"accounts": newest}), behind

    def _signatures(self, token_account: str, until: str | None, *, newest_only: bool) -> list[dict]:
        """Signatures newer than `until`, newest first, paged back with `before`."""
        out: list[dict] = []
        before = None
        for _ in range(1 if newest_only else MAX_PAGES):
            opts = {"commitment": "finalized", "limit": 1 if newest_only else SIG_PAGE}
            if until:
                opts["until"] = until
            if before:
                opts["before"] = before
            page = _call("getSignaturesForAddress", [token_account, opts])
            if not isinstance(page, list):
                raise http.Unavailable(SERVICE, "bad_answer")
            out += page
            if newest_only or len(page) < SIG_PAGE:
                return out
            before = page[-1]["signature"]
        raise http.Unavailable(SERVICE, "backlog")

    def _signatures_since(self, token_account: str, since: int) -> tuple[list[dict], str | None]:
        """Signatures from `since` (a unix time) on, newest first, and the newest one before it
        (None when the account's whole history is newer). A signature without a block time
        counts as newer: reading one more transaction is the safe side."""
        out: list[dict] = []
        before = None
        for _ in range(MAX_PAGES):
            opts = {"commitment": "finalized", "limit": SIG_PAGE}
            if before:
                opts["before"] = before
            page = _call("getSignaturesForAddress", [token_account, opts])
            if not isinstance(page, list):
                raise http.Unavailable(SERVICE, "bad_answer")
            for sig in page:
                stamp = sig.get("blockTime")
                if stamp is not None and _whole(stamp) < since:
                    return out, sig["signature"]
                out.append(sig)
            if len(page) < SIG_PAGE:
                return out, None
            before = page[-1]["signature"]
        raise http.Unavailable(SERVICE, "backlog")

    def transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        signature = tx_hash.strip()
        if not _is_signature(signature):  # the RPC would answer -32602, which reads as an outage
            return []
        try:
            tx = _transaction(signature)
            if tx is None:
                return []
            return _transfers(network, signature, tx, owner=None, mint=None)
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            raise http.Unavailable(SERVICE, "bad_answer") from None

    def status(self, payment) -> Status:
        try:
            result = _call("getSignatureStatuses", [[payment.tx_hash.strip()], {"searchTransactionHistory": True}])
            entry = result["value"][0]
            if entry is None or entry.get("err") is not None:
                return "dropped"
            return "final" if entry.get("confirmationStatus") == "finalized" else "pending"
        except (TypeError, ValueError, KeyError, AttributeError, IndexError):
            raise http.Unavailable(SERVICE, "bad_answer") from None
