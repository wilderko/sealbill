class InvoiceError(Exception):
    """A lifecycle refusal (editing a sent invoice, sending twice, ...)."""
