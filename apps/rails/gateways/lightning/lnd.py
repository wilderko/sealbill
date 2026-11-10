"""LND over its REST API with an invoice macaroon (it can create and read invoices, not pay).

Verified against LND's documentation and mocks only. Project 2 tests it on a testnet.
"""

import base64
import binascii
import time

from apps.rails import http
from apps.rails.gateways.lightning.base import FractionalSats

SERVICE = "lightning"


def _bad():
    return http.Unavailable(SERVICE, "bad_answer")


class LndBackend:
    def __init__(self, url: str, macaroon: str):
        self._base = url.rstrip("/")
        self._headers = {"Grpc-Metadata-macaroon": macaroon, "Accept": "application/json"}

    def create_invoice(self, amount_msat, description, expiry_s):
        if amount_msat % 1000:
            raise FractionalSats("whole sats only")
        body = {"value": str(amount_msat // 1000), "memo": description, "expiry": str(expiry_s)}
        out = http.post_json(f"{self._base}/v1/invoices", body,
                             headers=self._headers, service=SERVICE, guard=http.LOCAL_NODE)
        try:
            bolt11 = str(out["payment_request"])
            payment_hash = base64.b64decode(out["r_hash"], validate=True).hex()
            if not bolt11 or not payment_hash:
                raise ValueError("empty")
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise _bad() from None
        return bolt11, payment_hash

    def invoice_state(self, payment_hash):
        out = http.get_json(f"{self._base}/v1/invoice/{payment_hash}",
                            headers=self._headers, service=SERVICE, guard=http.LOCAL_NODE)
        try:
            state = str(out["state"])
            if state == "SETTLED":
                msat = int(out.get("amt_paid_msat") or 0)
                if msat < 0:
                    raise ValueError("negative")
                return "settled", msat
            if state == "CANCELED":
                return "expired", 0
            if state == "OPEN":
                created, expiry = int(out.get("creation_date") or 0), int(out.get("expiry") or 0)
                if created and expiry and created + expiry < time.time():
                    return "expired", 0
                return "open", 0
            return "unknown", 0  # ACCEPTED is a hold
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _bad() from None
