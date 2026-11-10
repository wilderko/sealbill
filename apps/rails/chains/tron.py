"""Tron watcher: allow-listed TRC-20 transfers, read from TronGrid.

Reports what the chain shows and nothing else. Every failure to look is `Unavailable`,
never an empty answer; nothing here logs an address, an amount or a hash.

Identity of a transfer is `(tx_hash, log_index)`: the transaction id in lowercase hex without
`0x`, and the ordinal of the transfer among that transaction's transfers to the same
recipient, after a sort by (contract, sender, amount). `scan` and `transfers_in_tx` build it
the same way. Everything TronGrid confirms is final, so `final` is always true.

A run reads at most `MAX_PAGES` pages. One that stops there continues on the next run from the
last row it read (TronGrid's `fingerprint`, kept in the cursor), never from the cursor's overlap
again: more spam rows than one run reads inside that overlap would otherwise make every run read
the same rows and nothing after them (audit MONEY-5). Its health meanwhile is "backlog", never
ok. The rows of the transaction the run stopped in are held in the cursor until the rest of that
transaction is read, so its transfers are numbered together. A continuation TronGrid no longer
accepts is dropped, and that contract is read afresh from the continuation's own last-read time
(`extra.since`), never from the cursor's overlap: the cursor is shared by every contract, and
another contract may have moved it far past where this one had read to.
"""

from __future__ import annotations

import datetime
import re
import time

from django.conf import settings
from django.db import transaction

from apps.rails import health, http
from apps.rails.addresses import tron_hex_to_base58
from apps.rails.chains.base import Status, Transfer, scan_back_to
from apps.rails.models import WatchCursor
from apps.rails.networks import NETWORKS, asset_by_contract

SERVICE = "tron"
PAGE_LIMIT = 200
MAX_PAGES = 25
OVERLAP_MS = 600_000  # re-read on every run: idempotent, and a lagging node cannot hide a transfer
_TX_ID = re.compile(r"^[0-9a-f]{64}$")


def _base() -> str:
    return settings.TRONGRID_API_URL.rstrip("/")


def _headers() -> dict:
    headers = {"Accept": "application/json"}
    key = getattr(settings, "TRONGRID_API_KEY", "")
    if key:
        headers["TRON-PRO-API-KEY"] = key
    return headers


def _get(path: str, params: dict) -> dict:
    try:
        answer = http.get_json(f"{_base()}{path}", params=params, headers=_headers(), service=SERVICE, timeout=20)
    except http.Refused as refused:
        raise http.Unavailable(SERVICE, str(refused.status)) from None
    if not isinstance(answer, dict) or answer.get("success") is False:
        raise http.Unavailable(SERVICE, "bad_answer")
    return answer


def _tx_id(value) -> str:
    if not isinstance(value, str):
        raise http.Unavailable(SERVICE, "bad_answer")
    tx = value.strip().lower().removeprefix("0x")
    if not _TX_ID.match(tx):
        raise http.Unavailable(SERVICE, "bad_answer")
    return tx


def _amount(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise http.Unavailable(SERVICE, "bad_answer")
    return int(value)


def _time(ms) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(ms) / 1000, tz=datetime.timezone.utc)


def _held(row: dict) -> dict:
    """A row read but not reported yet, as the cursor keeps it (JSON)."""
    return {**row, "block_time": int(row["block_time"].timestamp() * 1000)}


def _unheld(row: dict) -> dict:
    return {**row, "block_time": _time(row["block_time"])}


def _refused(exc: http.Unavailable) -> bool:
    """TronGrid understood the request and said no (a 4xx other than 429)."""
    return exc.kind.isdigit() and 400 <= int(exc.kind) < 500 and exc.kind != "429"


class _ContinuationRefused(Exception):
    """TronGrid no longer accepts the fingerprint a capped run kept."""


def _numbered(rows: list[dict]) -> list[Transfer]:
    """Give each row its log_index: the ordinal among the transaction's rows to the same
    recipient, after the deterministic sort. `rows` hold the Transfer fields except log_index."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["tx_hash"], row["to_address"]), []).append(row)
    out: list[Transfer] = []
    for group in groups.values():
        group.sort(key=lambda r: (r["contract"], r["from_address"], r["amount_base_units"]))
        out += [Transfer(log_index=i, **row) for i, row in enumerate(group)]
    return out


class TronAdapter:
    def scan(self, account, network: str) -> list[Transfer]:
        net = NETWORKS[network]
        address = account.address
        enabled = set(account.assets or [])
        contracts = [a.contract for s, a in net.assets.items() if a.contract and s in enabled]
        name = f"scan:tron:{network}:{account.pk}"
        stream = f"tron-trc20:{account.pk}"
        cursor = WatchCursor.objects.filter(network=network, address=address, stream=stream).first()
        kept = (cursor.extra or {}) if cursor is not None else {}
        stored, stored_since = dict(kept.get("resume") or {}), dict(kept.get("since") or {})
        resume, since_of = dict(stored), dict(stored_since)
        now_ms = int(time.time() * 1000)
        capped = False
        try:
            since = scan_back_to(account, network) if cursor is None else None
            if cursor is None and since is None:  # nothing anyone may be paying: start now
                position, found = now_ms, []
            else:
                if cursor is None:  # a client may have paid before this first look: read from their quote
                    position = start = int(since.timestamp() * 1000)
                else:
                    position = int(cursor.position)
                    start = position - OVERLAP_MS
                rows, newest = [], position
                for contract in contracts:
                    begin = min(start, int(since_of.get(contract, start)))
                    try:
                        got, seen, more = self._read(network, address, contract, begin, resume.get(contract))
                    except _ContinuationRefused:
                        # Drop only that continuation, and nothing read this run; that contract is
                        # read afresh from where the continuation had read to.
                        cont = stored[contract]
                        since_left = {**stored_since,
                                      contract: int(cont.get("last_ms", cont["params"]["min_timestamp"]))}
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

    def _read(self, network: str, address: str, contract: str, min_ms: int, resume: dict | None):
        """Rows from `min_ms` on, or where a capped run stopped (`resume`). Returns the rows to
        report, the newest timestamp read, and what the next run continues from (None when the
        listing ended)."""
        if resume:
            params = {**resume["params"], "fingerprint": resume["fingerprint"]}
            rows = [_unheld(r) for r in resume.get("held") or []]
        else:
            params = {"only_confirmed": "true", "only_to": "true", "contract_address": contract,
                      "min_timestamp": max(min_ms, 0), "limit": PAGE_LIMIT, "order_by": "block_timestamp,asc"}
            rows = []
        newest, last_tx = 0, None
        for number in range(MAX_PAGES):
            try:
                answer = _get(f"/v1/accounts/{address}/transactions/trc20", params)
            except http.Unavailable as exc:
                if resume and number == 0 and _refused(exc):
                    raise _ContinuationRefused() from None
                raise
            data = answer.get("data") or []
            if not isinstance(data, list):
                raise http.Unavailable(SERVICE, "bad_answer")
            for row in data:
                stamp = int(row["block_timestamp"])
                newest = max(newest, stamp)
                last_tx = str(row.get("transaction_id", "")).strip().lower().removeprefix("0x")
                info = row.get("token_info") or {}
                # only_to and contract_address are filters, not guarantees: check both ourselves
                if row.get("to") != address or info.get("address") != contract or row.get("type", "Transfer") != "Transfer":
                    continue
                if asset_by_contract(network, contract) is None:
                    continue
                amount = _amount(row.get("value"))
                if amount <= 0:
                    continue
                rows.append(dict(
                    network=network, contract=contract, to_address=address, from_address=str(row.get("from", "")),
                    amount_base_units=amount, tx_hash=_tx_id(row.get("transaction_id")), block_number=None,
                    block_time=_time(stamp), final=True))
            fingerprint = (answer.get("meta") or {}).get("fingerprint")
            if not fingerprint or not data:
                return rows, newest, None
            params = {**params, "fingerprint": fingerprint}
        # Capped: the next run continues after the last row read. Its transaction may go on there,
        # so its rows wait in the cursor and are numbered with the rest of it.
        held = [r for r in rows if r["tx_hash"] == last_tx]
        rows = [r for r in rows if r["tx_hash"] != last_tx]
        base = {k: v for k, v in params.items() if k != "fingerprint"}
        return rows, newest, {"params": base, "fingerprint": fingerprint, "last_ms": newest,
                              "held": [_held(r) for r in held]}

    def _events(self, network: str, tx_hash: str, *, confirmed: bool = True) -> list[Transfer]:
        tx = tx_hash.strip().lower().removeprefix("0x")
        if not _TX_ID.match(tx):
            return []
        try:
            answer = _get(f"/v1/transactions/{tx}/events", {"only_confirmed": "true" if confirmed else "false"})
            rows = []
            for event in answer.get("data") or []:
                contract = event.get("contract_address")
                if event.get("event_name") != "Transfer" or asset_by_contract(network, str(contract)) is None:
                    continue
                result = event["result"]
                amount = _amount(result["value"])
                if amount <= 0:
                    continue
                rows.append(dict(
                    network=network, contract=contract, to_address=tron_hex_to_base58(result["to"]),
                    from_address=tron_hex_to_base58(result["from"]), amount_base_units=amount, tx_hash=tx,
                    block_number=event.get("block_number"), block_time=_time(event["block_timestamp"]), final=confirmed))
            return _numbered(rows)
        except (TypeError, ValueError, KeyError, AttributeError, OSError, OverflowError):
            raise http.Unavailable(SERVICE, "bad_answer") from None

    def transfers_in_tx(self, network: str, tx_hash: str) -> list[Transfer]:
        return self._events(network, tx_hash)

    def status(self, payment) -> Status:
        for t in self._events(payment.network, payment.tx_hash):
            if t.log_index == payment.log_index:
                return "final"
        return "pending"  # unconfirmed or not indexed yet: Tron never reports a transfer before it is final
