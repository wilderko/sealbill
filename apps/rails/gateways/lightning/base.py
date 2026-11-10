"""The seam between the Lightning gateway and a node.

A backend does two things: it makes an invoice and it says what became of one. It never pays
anything: no backend holds a spending credential, and none has a method that sends money.
"""

from typing import Literal, Protocol

InvoiceState = Literal["open", "settled", "expired", "unknown"]


class FractionalSats(ValueError):
    """An amount that is not a whole number of satoshis. Some nodes refuse it; none is asked."""


class LightningBackend(Protocol):
    def create_invoice(self, amount_msat: int, description: str, expiry_s: int) -> tuple[str, str]:
        """Returns `(bolt11, payment_hash)`. Raises `http.Unavailable` / `http.Refused`."""

    def invoice_state(self, payment_hash: str) -> tuple[InvoiceState, int]:
        """Returns the state and the msat that settled it (0 unless settled)."""
