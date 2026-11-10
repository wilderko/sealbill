"""ISO 11649 creditor reference: RF + 2 check digits + up to 21 alphanumerics."""
import re


def _numeric(s: str) -> str:
    return "".join(str(int(c, 36)) for c in s)


def _body(invoice) -> str:
    if invoice.kind == "invoice" and invoice.number:
        body = re.sub(r"[^0-9A-Za-z]", "", invoice.number).upper()
    else:
        body = f"P{invoice.pk}"
    return body[:21]


def payment_reference(invoice) -> str:
    body = _body(invoice)
    check = 98 - int(_numeric(body + "RF00")) % 97
    return f"RF{check:02d}{body}"


def is_valid(ref: str) -> bool:
    ref = (ref or "").replace(" ", "").upper()
    if not re.fullmatch(r"RF\d{2}[0-9A-Z]{1,21}", ref):
        return False
    return int(_numeric(ref[4:] + ref[:4])) % 97 == 1
