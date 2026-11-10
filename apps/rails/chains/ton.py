"""TON watcher: USDT jetton transfers, read from toncenter v3.

Reports what the chain shows and nothing else. Every failure to look is `Unavailable`, never
an empty answer; nothing here logs an address, an amount or a hash.

Identity of a transfer is `(tx_hash, log_index)`: the transaction hash as lowercase hex
(toncenter gives base64) and the ordinal of the transfer among that transaction's transfers
to the same recipient, after a sort by (jetton master, sender, amount). Addresses are compared
in the raw `wc:hex` form `addresses.normalize` gives; toncenter prints raw addresses in upper
case, so they are lowered first.

A transfer younger than `FINAL_AFTER_S` is reported as not final. The 600 s overlap re-read
reports it again once it is old enough, with the same identity.

A run reads at most `MAX_PAGES` pages. One that stops there continues on the next run with the
same `start_utime` (kept in the cursor), and its health meanwhile is "backlog", never ok (audit
MONEY-5). The listing can shift between runs (a row indexed late, a row gone), so the next run
starts one row before where this one stopped and skips through the hash it read last: a shift
by one either way neither reads that row twice nor skips the one after it. A TON transaction
carries at most one jetton transfer to us, so identical rows of one transaction are one
transfer. The rows of the transaction a run stopped in wait in the cursor until the rest of it
is read. A continuation toncenter refuses is dropped, and the contract is read afresh from the
continuation's own last-read time (`extra.since`).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import re
import time

from django.conf import settings
from django.db import transaction

from apps.rails import health, http
from apps.rails.addresses import normalize
from apps.rails.chains.base import Status, Transfer, scan_back_to
from apps.rails.models import WatchCursor
from apps.rails.networks import NETWORKS, asset_by_contract

SERVICE = "ton"
PAGE_LIMIT = 100
MAX_PAGES = 20
OVERLAP_S = 600
FINAL_AFTER_S = 30
LOOKUP_WINDOW_S = 2
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _get(path: str, params: dict) -> dict:
    headers = {"Accept": "application/json"}
    key = getattr(settings, "TONCENTER_API_KEY", "")
    if key:
        headers["X-API-Key"] = key
    url = settings.TONCENTER_API_URL.rstrip("/") + path
    try:
        answer = http.get_json(url, params=params, headers=headers, service=SERVICE, timeout=20)
    except http.Refused as refused:
        raise http.Unavailable(SERVICE, str(refused.status)) from None
    if not isinstance(answer, dict) or "error" in answer:
        raise http.Unavailable(SERVICE, "bad_answer")
    return answer


def _raw(address) -> str:
    """The raw `wc:hex` form of an address in any form toncenter or a client writes it."""
    text = str(address).strip()
    return normalize("ton", text.lower() if re.match(r"^-?\d+:[0-9A-Fa-f]{64}$", text) else text)


def canonical_hash(value: str) -> str | None:
    """A TON transaction hash as lowercase hex, from hex or base64 (standard or URL-safe)."""
    text = (value or "").strip()
    if _HEX.match(text.lower()):
        return text.lower()
    try:
        raw = base64.b64decode(text.replace("-", "+").replace("_", "/").ljust(44, "="), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw.hex() if len(raw) == 32 else None


def _whole(value) -> int:
    """A whole number a service sent as text or integer; a float or anything else is a bad answer."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("not a whole number")
    return int(value)


def _comment(row: dict) -> str:
    payload = row.get("decoded_forward_payload")
    if isinstance(payload, dict) and payload.get("type") == "text_comment" and isinstance(payload.get("comment"), str):
        return payload["comment"]
    return ""


def _numbered(rows: list[dict]) -> list[Transfer]:
    """Number each transaction's transfers to one recipient, after collapsing rows that are the
    same transfer read twice (a held row and the listing's own copy of it)."""
    groups: dict[tuple[str, str], dict[tuple, dict]] = {}
    for row in rows:
        same = (row["contract"], row["from_address"], row["amount_base_units"], row["block_time"], row["comment"])
        group = groups.setdefault((row["tx_hash"], row["to_address"]), {})
        if same in group:
            group[same] = {**group[same], "final": group[same]["final"] or row["final"]}
        else:
            group[same] = row
    out: list[Transfer] = []
    for group in groups.values():
        listed = sorted(group.values(), key=lambda r: (r["contract"], r["from_address"], r["amount_base_units"]))
        out += [Transfer(log_index=i, **row) for i, row in enumerate(listed)]
    return out


def _refused(exc: http.Unavailable) -> bool:
    """toncenter understood the request and said no (a 4xx other than 429)."""
    return exc.kind.isdigit() and 400 <= int(exc.kind) < 500 and exc.kind != "429"


class _ContinuationRefused(Exception):
    """toncenter no longer accepts the continuation a capped run kept."""


def _row(network: str, row: dict, master_raw: str, contract: str, to_raw: str | None) -> dict | None:
    """The Transfer fields of one toncenter row, or None when it is not ours to report."""
    if row.get("transaction_aborted") is not False:  # absent or odd: fail closed
        return None
    if _raw(row["jetton_master"]) != master_raw:
        return None
    to = _raw(row["destination"])
    if to_raw is not None and to != to_raw:
        return None
    amount = _whole(row["amount"])
    tx_hash = canonical_hash(row["transaction_hash"])
    if amount <= 0 or tx_hash is None:
        return None
    when = datetime.datetime.fromtimestamp(_whole(row["transaction_now"]), tz=datetime.timezone.utc)
    age = time.time() - when.timestamp()
    try:
        sender = _raw(row.get("source"))
    except ValueError:  # informational only: a row without a readable sender is still a payment
        sender = ""
    return dict(network=network, contract=contract, to_address=to, from_address=sender,
                amount_base_units=amount, tx_hash=tx_hash, block_number=None, block_time=when,
                final=age >= FINAL_AFTER_S, comment=_comment(row))


def _held(row: dict) -> dict:
    """A row read but not reported yet, as the cursor keeps it (JSON)."""
    return {**row, "block_time": int(row["block_time"].timestamp())}


def _unheld(row: dict) -> dict:
    when = datetime.datetime.fromtimestamp(row["block_time"], tz=datetime.timezone.utc)
    return {**row, "block_time": when, "final": time.time() - when.timestamp() >= FINAL_AFTER_S}


class TonAdapter:
    def scan(self, account, network: str) -> list[Transfer]:
        net = NETWORKS[network]
        address = account.address
        enabled = set(account.assets or [])
        contracts = [a.contract for s, a in net.assets.items() if a.contract and s in enabled]
        name = f"scan:ton:{network}:{account.pk}"
        stream = f"ton-jetton:{account.pk}"
        cursor = WatchCursor.objects.filter(network=network, address=address, stream=stream).first()
        kept = (cursor.extra or {}) if cursor is not None else {}
        stored, stored_since = dict(kept.get("resume") or {}), dict(kept.get("since") or {})
        resume, since_of = dict(stored), dict(stored_since)
        capped = False
        try:
            since = scan_back_to(account, network) if cursor is None else None
            if cursor is None and since is None:  # nothing anyone may be paying: start now
                position, found = int(time.time()), []
            else:
                if cursor is None:  # a client may have paid before this first look: read from their quote
                    position = start = int(since.timestamp())
                else:
                    position = int(cursor.position)
                    start = position - OVERLAP_S
                rows, newest = [], position
                for contract in contracts:
                    begin = min(start, int(since_of.get(contract, start)))
                    try:
                        got, seen, more = self._read(network, address, contract, begin, resume.get(contract))
                    except _ContinuationRefused:
                        # Drop only that continuation, and nothing read this run; that contract is
                        # read afresh from where the continuation had read to.
                        cont = stored[contract]
                        since_left = {**stored_since, contract: int(cont.get("last_time", cont["start"]))}
                        self._save(network, address, stream, cursor.position,
                                   {k: v for k, v in stored.items() if k != contract}, since_left)
                        raise http.Unavailable(SERVICE, "continuation_refused") from None
                    rows += got
                    newest = max(newest, seen)
                    since_of.pop(contract, None)
                    if more is None:
                        resume.pop(contract, None)
                    else:
                        resume[contract], capped = more, True
                found, position = _numbered(rows), newest
        except http.Unavailable as exc:
            health.failed(name, exc.kind)
            raise
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            health.failed(name, "bad_answer")
            raise http.Unavailable(SERVICE, "bad_answer") from None
        with transaction.atomic():
            self._save(network, address, stream, position, resume, since_of)
        if capped:  # progress was made; the rest is read on the next runs
            health.failed(name, "backlog")
        else:
            health.ok(name)
        return found

    @staticmethod
    def _save(network, address, stream, position, resume, since):
        extra = {key: value for key, value in (("resume", resume), ("since", since)) if value}
        WatchCursor.objects.update_or_create(network=network, address=address, stream=stream,
                                             defaults={"position": str(position), "extra": extra})

    def _read(self, network: str, address: str, contract: str, start: int, resume: dict | None):
        """Rows from `start` on, or where a capped run stopped (`resume`). Returns the rows to
        report, the newest time read, and what the next run continues from (None when the
        listing ended)."""
        master_raw = _raw(contract)
        if resume:
            # One row back, then through the hash read last: a listing shifted by one either way
            # neither reads that row twice nor skips the next one.
            start, offset = int(resume["start"]), max(int(resume["offset"]) - 1, 0)
            rows = [_unheld(r) for r in resume.get("held") or []]
            skip_through = resume.get("last_hash")
        else:
            offset, rows, skip_through = 0, [], None
        newest, last_tx = 0, None
        for page in range(MAX_PAGES):
            try:
                answer = _get("/jetton/transfers", {
                    "owner_address": address, "direction": "in", "jetton_master": contract,
                    "start_utime": max(start, 0), "sort": "asc", "limit": PAGE_LIMIT,
                    "offset": offset + page * PAGE_LIMIT})
            except http.Unavailable as exc:
                if resume and page == 0 and _refused(exc):
                    raise _ContinuationRefused() from None
                raise
            data = answer.get("jetton_transfers")
            if not isinstance(data, list):
                raise http.Unavailable(SERVICE, "bad_answer")
            listed = data
            if page == 0 and skip_through:
                hashes = [canonical_hash(str(row.get("transaction_hash") or "")) for row in data]
                if skip_through in hashes:
                    listed = data[hashes.index(skip_through) + 1:]
            for row in listed:
                newest = max(newest, _whole(row["transaction_now"]))
                last_tx = canonical_hash(str(row.get("transaction_hash") or ""))
                fields = _row(network, row, master_raw, contract, address)
                if fields is not None:
                    rows.append(fields)
            if len(data) < PAGE_LIMIT:
                return rows, newest, None
        # Capped: the next run continues after the last row read; its transaction waits with it.
        held = [r for r in rows if r["tx_hash"] == last_tx]
        rows = [r for r in rows if r["tx_hash"] != last_tx]
        return rows, newest, {"start": max(start, 0), "offset": offset + MAX_PAGES * PAGE_LIMIT,
                              "last_hash": last_tx, "last_time": newest, "held": [_held(r) for r in held]}

    def transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        tx = canonical_hash(tx_hash)
        if tx is None:
            return []
        try:
            # Find the transaction's time, then read the jetton transfers of that moment: the
            # transfers endpoint cannot be asked for one hash.
            found = _get("/transactions", {"hash": tx, "limit": 1}).get("transactions")
            if not isinstance(found, list):
                raise http.Unavailable(SERVICE, "bad_answer")
            if not found:
                return []
            now = _whole(found[0]["now"])
            rows: list[dict] = []
            for contract in (a.contract for a in NETWORKS[network].assets.values() if a.contract):
                master_raw = _raw(contract)
                for page in range(MAX_PAGES):
                    data = _get("/jetton/transfers", {
                        "jetton_master": contract, "start_utime": now - LOOKUP_WINDOW_S,
                        "end_utime": now + LOOKUP_WINDOW_S, "sort": "asc", "limit": PAGE_LIMIT,
                        "offset": page * PAGE_LIMIT}).get("jetton_transfers")
                    if not isinstance(data, list):
                        raise http.Unavailable(SERVICE, "bad_answer")
                    for row in data:
                        if canonical_hash(row["transaction_hash"]) != tx:
                            continue
                        fields = _row(network, row, master_raw, contract, None)
                        if fields is not None:
                            rows.append(fields)
                    if len(data) < PAGE_LIMIT:
                        break
                else:  # never a possibly incomplete list: status() would read it as dropped
                    raise http.Unavailable(SERVICE, "backlog")
            return _numbered(rows)
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            raise http.Unavailable(SERVICE, "bad_answer") from None

    def status(self, payment) -> Status:
        for t in self.transfers_in_tx(payment.network, payment.tx_hash):
            if t.log_index == payment.log_index:
                return "final" if t.final else "pending"
        return "dropped"
