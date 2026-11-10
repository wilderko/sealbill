"""Core Lightning over clnrest with a restricted rune (invoice and listinvoices only).

Verified against Core Lightning's documentation and mocks only. Project 2 tests it on a testnet.
"""

import re
import uuid

from apps.rails import http
from apps.rails.gateways.lightning.base import FractionalSats

SERVICE = "lightning"
_MSAT = re.compile(r"^(\d+)(msat)?$")


def _bad():
    return http.Unavailable(SERVICE, "bad_answer")


def _msat(value) -> int:
    if isinstance(value, bool):
        raise ValueError("bool")
    if isinstance(value, int):
        return value
    m = _MSAT.match(str(value))
    if not m:
        raise ValueError("not msat")
    return int(m.group(1))


class ClnBackend:
    def __init__(self, url: str, rune: str):
        self._base = url.rstrip("/")
        self._headers = {"Rune": rune, "Accept": "application/json"}

    def create_invoice(self, amount_msat, description, expiry_s):
        if amount_msat % 1000:
            raise FractionalSats("whole sats only")
        # The label must be unique per invoice and the quote is not saved yet, so it has no pk.
        body = {"amount_msat": amount_msat, "label": f"q{uuid.uuid4().hex}",
                "description": description, "expiry": expiry_s}
        out = http.post_json(f"{self._base}/v1/invoice", body,
                             headers=self._headers, service=SERVICE, guard=http.LOCAL_NODE)
        try:
            bolt11, payment_hash = str(out["bolt11"]), str(out["payment_hash"])
            if not bolt11 or not payment_hash:
                raise ValueError("empty")
        except (KeyError, TypeError, ValueError):
            raise _bad() from None
        return bolt11, payment_hash

    def invoice_state(self, payment_hash):
        out = http.post_json(f"{self._base}/v1/listinvoices", {"payment_hash": payment_hash},
                             headers=self._headers, service=SERVICE, guard=http.LOCAL_NODE)
        try:
            rows = out["invoices"]
            if not isinstance(rows, list):
                raise ValueError("shape")
            if not rows:
                return "unknown", 0
            row = rows[0]
            status = str(row["status"])
            if status == "paid":
                msat = _msat(row.get("amount_received_msat") or row["amount_msat"])
                if msat < 0:
                    raise ValueError("negative")
                return "settled", msat
            if status == "expired":
                return "expired", 0
            if status == "unpaid":
                return "open", 0
            return "unknown", 0
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _bad() from None
