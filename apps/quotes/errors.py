QUOTE_ERROR_CODES = (
    "not_payable",          # the invoice is not in a state that takes payments
    "method_not_offered",   # the invoice does not offer this rail, or the account is gone or inactive
    "asset_not_enabled",    # that network or asset is not enabled on the account
    "nothing_due",          # confirmed payments already cover the total
    "rates_unavailable",    # an exchange-rate source could not answer
    "rates_diverge",        # the two sources disagree by more than the bound
    "no_free_amount",       # every unique amount for this base is held in its amount group
    "too_many_quotes",      # the invoice already holds QUOTES_PER_INVOICE quotes not yet released
    "rail_unavailable",     # the gateway is not configured or could not answer
)


class QuoteError(Exception):
    """A quote was refused. `code` is a machine code; the pay page and the API translate it."""

    def __init__(self, code: str):
        if code not in QUOTE_ERROR_CODES:
            raise ValueError(f"unknown quote error code {code!r}")
        super().__init__(code)
        self.code = code
