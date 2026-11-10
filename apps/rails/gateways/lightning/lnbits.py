"""LNbits with an invoice/read key (it can create invoices and read payments, not pay).

Verified against LNbits' documentation and mocks only. Project 2 tests it on a testnet.
"""

from apps.rails import http
from apps.rails.gateways.lightning.base import FractionalSats

SERVICE = "lightning"


def _bad():
    return http.Unavailable(SERVICE, "bad_answer")


class LnbitsBackend:
    def __init__(self, url: str, invoice_key: str):
        self._base = url.rstrip("/")
        self._headers = {"X-Api-Key": invoice_key, "Accept": "application/json"}

    def create_invoice(self, amount_msat, description, expiry_s):
        if amount_msat % 1000:
            raise FractionalSats("whole sats only")
        body = {"out": False, "amount": amount_msat // 1000, "memo": description, "expiry": expiry_s}
        out = http.post_json(f"{self._base}/api/v1/payments", body,
                             headers=self._headers, service=SERVICE, guard=http.STAFF_URL)
        try:
            bolt11 = str(out.get("payment_request") or out["bolt11"])
            payment_hash = str(out["payment_hash"])
            if not bolt11 or not payment_hash:
                raise ValueError("empty")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _bad() from None
        return bolt11, payment_hash

    def invoice_state(self, payment_hash):
        out = http.get_json(f"{self._base}/api/v1/payments/{payment_hash}",
                            headers=self._headers, service=SERVICE, guard=http.STAFF_URL)
        try:
            paid = out["paid"]
            if not isinstance(paid, bool):
                raise ValueError("paid")
            if not paid:
                return "open", 0  # LNbits does not report expiry; the quote's own clock does
            msat = out["details"]["amount"]
            if isinstance(msat, bool) or not isinstance(msat, int) or msat < 0:
                raise ValueError("amount")
            return "settled", msat
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _bad() from None
