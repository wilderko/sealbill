"""ISO 4217 minor-unit exponents for invoice currencies (fiat plus BTC)."""

_EXPONENTS = {
    "EUR": 2, "USD": 2, "CZK": 2, "GBP": 2, "CHF": 2, "PLN": 2, "HUF": 2,
    "GEL": 2, "KGS": 2, "KRW": 0, "JPY": 0, "RUB": 2, "UAH": 2, "SEK": 2,
    "NOK": 2, "DKK": 2, "CAD": 2, "AUD": 2, "PYG": 0, "BRL": 2, "MXN": 2,
    "AED": 2, "BTC": 8,
}

# Currencies a carried-over invoice may be in (Request Finance's crypto invoices, owner 2026-11-09).
# For history only: they can be imported and shown, never issued, linked or chosen in a setting.
# Exponents keep an amount inside a bigint and are exact for what RF issued (the exporter refuses
# an amount that does not divide).
MIGRATED_ONLY = {
    "USDT": 6, "USDC": 6, "DAI": 2, "TUSD": 2, "ETH": 8, "WETH": 8, "WBTC": 8,
    "TRX": 6, "BNB": 8, "MATIC": 8, "SOL": 9,
}

CURRENCIES = tuple(sorted(_EXPONENTS))  # the currencies anything new may use


def minor_units(code: str) -> int:
    code = (code or "").upper()
    try:
        return _EXPONENTS[code] if code in _EXPONENTS else MIGRATED_ONLY[code]
    except KeyError:
        raise ValueError(f"unknown invoice currency: {code!r}")


def check_issuable(code: str) -> None:
    """ValueError unless something new may be issued in `code` (a carried-over-only currency may not)."""
    minor_units(code)
    if (code or "").upper() not in _EXPONENTS:
        raise ValueError(f"{code!r} is kept for carried-over invoices only")
