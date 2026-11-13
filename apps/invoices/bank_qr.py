"""QR codes a banking app reads to fill in a bank transfer: PAY by square and payme (Slovakia), QR
Platba (SPAYD, Czech Republic) and the EPC QR code (GiroCode, SEPA).

Each code carries the account, the amount still due, the currency and the payment reference, so
the client has nothing to type. A code is offered only where its standard fits: EUR to the IBAN of
a SEPA country gets all four, CZK to a Czech IBAN gets QR Platba. Anything else gets none, because
a banking app would refuse such a code or turn it into a SWIFT payment with fees. Bank transfers are still recorded by a person (Sealbill does not read the bank):
the codes only make the transfer easier to get right.

PAY by square follows the bysquare reference implementation (github.com/xseman/bysquare): the
tab-separated fields with a CRC32 in front, raw LZMA (lc 3, lp 0, pb 2, 128 KiB dictionary), a
two-byte header and the payload length, in base32hex without padding. It is written as version
1.1.0, which every Slovak banking app reads; 1.2.0 differs only in the header and in requiring the
beneficiary's name, which these codes always carry. EPC follows EPC069-12 (version 002, UTF-8,
SEPA credit transfer), QR Platba the Czech Banking Association's SPAYD 1.0.

payme is the Slovak Banking Association's Payment Link Standard 2.0: a web address on payme.sk that a
Slovak banking app opens as a payment order, so it works as a link on a phone as well as a QR code. A
phone whose banking app does not take payme links opens the address in the browser, and payme.sk then
shows the details with a PAY by square code: in that case, and only then, payme.sk sees the transfer.
Sealbill itself never calls payme.sk. The link is built the way payme.sk's own generator builds an
invoice: type /p/ (an ordinary SEPA transfer the client can still edit), the variable symbol in the
payment identification as /VS…/SS/KS (the reference when there is no symbol), the reference as the message.
"""
import binascii
import lzma
import re
import struct
import unicodedata
from decimal import Decimal
from urllib.parse import quote_plus, urlencode

from django.utils.translation import gettext_lazy as _

BASE32HEX = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
_LZMA_FILTERS = [{"id": lzma.FILTER_LZMA1, "lc": 3, "lp": 0, "pb": 2, "dict_size": 128 * 1024}]

# The SEPA scheme's countries and territories, by IBAN prefix.
SEPA_COUNTRIES = frozenset(
    "AD AT BE BG CH CY CZ DE DK EE ES FI FR GB GI GR HR HU IE IS IT LI LT LU LV MC MT NL NO PL PT RO SE SI SK "
    "SM VA".split()
    + "AX BL GF GG GP IM JE MF MQ PM RE YT".split())

LABELS = {
    "pay_by_square": _("PAY by square (Slovakia)"),
    "payme": _("payme (Slovakia)"),
    "qr_platba": _("QR Platba (Czech Republic)"),
    "epc": _("EPC QR (SEPA)"),
}


# -- helpers ---------------------------------------------------------------------------------------

def normalize_iban(value) -> str | None:
    """The IBAN without spaces, upper case, or None when `value` is not one (mod 97 included)."""
    iban = re.sub(r"\s+", "", str(value or "")).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return None
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return iban if int(digits) % 97 == 1 else None


def normalize_bic(value) -> str:
    bic = re.sub(r"\s+", "", str(value or "")).upper()
    return bic if re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic) else ""


def deburr(text) -> str:
    """Latin letters without their diacritics (Žltý kôň → Zlty kon), tabs and line breaks as spaces."""
    flat = unicodedata.normalize("NFKD", str(text or ""))
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return re.sub(r"[\t\r\n]+", " ", flat).strip()


def variable_symbol(invoice_number) -> str:
    """The digits of the invoice number, when there are one to ten of them (2026-0007 → 20260007): the
    variable symbol Slovak and Czech payers expect. Empty otherwise."""
    digits = re.sub(r"\D", "", str(invoice_number or "")).lstrip("0") or ""
    return digits if 1 <= len(digits) <= 10 else ""


def _plain(amount: Decimal) -> str:
    """100 → "100", 123.450 → "123.45": the bysquare reference library's amount format."""
    text = format(amount.normalize(), "f")
    return text


def _base32hex(data: bytes) -> str:
    bits = "".join(f"{b:08b}" for b in data)
    bits += "0" * (-len(bits) % 5)
    return "".join(BASE32HEX[int(bits[i:i + 5], 2)] for i in range(0, len(bits), 5))


def _unbase32hex(text: str) -> bytes:
    bits = "".join(f"{BASE32HEX.index(c):05b}" for c in text.rstrip("=").upper())
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - len(bits) % 8, 8))


# -- PAY by square -----------------------------------------------------------------------------------

def encode_pay_by_square(payload: str, *, version: int = 1) -> str:
    """The QR text for a serialized PAY by square document (tab-separated fields)."""
    raw = payload.encode("utf-8")
    checked = struct.pack("<I", binascii.crc32(raw) & 0xFFFFFFFF) + raw
    if len(checked) >= 65536:
        raise ValueError("a PAY by square document must stay under 64 KiB")
    body = lzma.compress(checked, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
    header = bytes([(0x0 << 4) | (version & 0x0F), (0x0 << 4) | 0x0])  # PAY, version; payment order, reserved
    return _base32hex(header + struct.pack("<H", len(checked)) + body)


def decode_pay_by_square(text: str) -> tuple[int, str]:
    """(version, payload) of a PAY by square QR text; ValueError when it does not check out."""
    data = _unbase32hex(text)
    if len(data) < 4 or data[0] >> 4 != 0:
        raise ValueError("not a PAY by square document")
    version, length = data[0] & 0x0F, struct.unpack("<H", data[2:4])[0]
    checked = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS).decompress(data[4:])[:length]
    if len(checked) != length:
        raise ValueError("truncated PAY by square document")
    crc, raw = struct.unpack("<I", checked[:4])[0], checked[4:]
    if binascii.crc32(raw) & 0xFFFFFFFF != crc:
        raise ValueError("PAY by square checksum mismatch")
    return version, raw.decode("utf-8")


def pay_by_square(*, iban: str, bic: str, amount: Decimal, currency: str, note: str, variable_symbol: str,
                  beneficiary_name: str) -> str:
    """One payment order to one account, as PAY by square 1.1.0."""
    fields = [
        "",                       # invoice id
        "1",                      # payments
        "1",                      # payment order
        _plain(amount), currency.upper(),
        "",                       # due date: the client pays when they pay
        variable_symbol, "", "",  # variable, constant, specific symbol
        "",                       # originator's reference information
        deburr(note)[:140],
        "1", iban, bic,           # one bank account
        "0", "0",                 # no standing order, no direct debit
        deburr(beneficiary_name)[:70], "", "",  # beneficiary name, street, city
    ]
    return encode_pay_by_square("\t".join(fields), version=1)


# -- payme (Payment Link Standard 2.0) ---------------------------------------------------------------

PAYME_BASE = "https://payme.sk/2/p/PME"   # version 2, person-to-person type (invoices too), payme.sk scheme
_PAYME_DROP = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")  # Annex A, the recommended characters


def _payme_text(text, limit: int) -> str:
    """`text` in the standard's recommended characters (Annex A), diacritics off, at most `limit` long."""
    return re.sub(r" {2,}", " ", _PAYME_DROP.sub("", deburr(text))).strip()[:limit].strip()


def payme(*, iban: str, amount: Decimal, message: str, variable_symbol: str, beneficiary_name: str,
          payment_id: str = "") -> str | None:
    """The payme link for one EUR transfer, or None when the standard does not allow it (no creditor's
    name, an amount over nine characters). The payment identification is the variable symbol as
    /VS…/SS/KS, or else `payment_id` when it keeps to the identification's rules (at most 35 characters,
    no slash at either end, no double slash)."""
    name = _payme_text(beneficiary_name, 70)
    am = f"{amount:.2f}"
    if not name or amount <= 0 or len(am) > 9:
        return None
    msg = _payme_text(message, 140)
    if variable_symbol:
        pi = f"/VS{variable_symbol}/SS/KS"
    else:
        pi = _payme_text(payment_id, 999)
        if len(pi) > 35 or pi.startswith("/") or pi.endswith("/") or "//" in pi:
            pi = ""
    query = [("IBAN", iban), ("AM", am), ("CC", "EUR")]
    query += [("PI", pi)] if pi else []
    query += [("MSG", msg)] if msg else []
    query.append(("CN", name))
    return PAYME_BASE + "?" + urlencode(query, quote_via=quote_plus)


# -- EPC QR (GiroCode) -------------------------------------------------------------------------------

def epc_qr(*, iban: str, bic: str, amount: Decimal, beneficiary_name: str, reference: str) -> str | None:
    """The EPC069-12 text, or None when the standard does not allow it (no name, amount out of range)."""
    from apps.invoices.reference import is_valid

    name = (beneficiary_name or "").strip()[:70]
    if not name or not Decimal("0.01") <= amount <= Decimal("999999999.99"):
        return None
    structured = reference if is_valid(reference) else ""
    lines = ["BCD", "002", "1", "SCT", bic, name, iban, f"EUR{amount:.2f}", "", structured,
             "" if structured else (reference or "")[:140]]
    return "\n".join(lines).rstrip("\n")


# -- QR Platba (SPAYD) -------------------------------------------------------------------------------

def _spayd_value(text: str) -> str:
    return deburr(text).replace("%", "%25").replace("*", "%2A")


def spayd(*, iban: str, bic: str, amount: Decimal, currency: str, message: str, variable_symbol: str,
          beneficiary_name: str) -> str | None:
    """The SPAYD 1.0 text, or None when the amount does not fit its field (at most 10 characters)."""
    am = f"{amount:.2f}"
    if amount <= 0 or len(am) > 10:
        return None
    parts = ["SPD", "1.0", f"ACC:{iban}" + (f"+{bic}" if bic else ""), f"AM:{am}", f"CC:{currency.upper()}"]
    if message:
        parts.append("MSG:" + _spayd_value(message)[:60])
    if variable_symbol:
        parts.append("X-VS:" + variable_symbol)
    if beneficiary_name:
        parts.append("RN:" + _spayd_value(beneficiary_name)[:35])
    return "*".join(parts)


# -- what a bank method gets -------------------------------------------------------------------------

def codes_for(method: dict, *, amount_minor: int, currency: str, reference: str, invoice_number: str) -> list[dict]:
    """The QR codes for one bank method (a snapshot dict), for `amount_minor` still due in `currency`.

    Each is {"standard", "label", "text", "image", "link"}, where `link` is the text again when the code is
    a web address a phone can open (payme) and empty otherwise; an empty list when none fits."""
    from apps.invoices.currencies import minor_units
    from apps.portal.qr import qr_data_uri

    iban = normalize_iban(method.get("account_number"))
    currency = (currency or "").upper()
    if iban is None or amount_minor <= 0:
        return []
    if not ((currency == "EUR" and iban[:2] in SEPA_COUNTRIES) or (currency == "CZK" and iban[:2] == "CZ")):
        return []
    amount = Decimal(int(amount_minor)).scaleb(-minor_units(currency))
    bic = normalize_bic(method.get("bank_swift"))
    name = (method.get("beneficiary_name") or "").strip()
    vs = variable_symbol(invoice_number)
    texts = []
    if currency == "EUR":
        texts.append(("pay_by_square", pay_by_square(iban=iban, bic=bic, amount=amount, currency=currency,
                                                     note=reference, variable_symbol=vs, beneficiary_name=name)))
        texts.append(("payme", payme(iban=iban, amount=amount, message=reference, variable_symbol=vs,
                                     beneficiary_name=name, payment_id=reference)))
    texts.append(("qr_platba", spayd(iban=iban, bic=bic, amount=amount, currency=currency, message=reference,
                                     variable_symbol=vs, beneficiary_name=name)))
    if currency == "EUR":
        texts.append(("epc", epc_qr(iban=iban, bic=bic, amount=amount, beneficiary_name=name, reference=reference)))
    return [{"standard": key, "label": LABELS[key], "text": text, "image": qr_data_uri(text),
             "link": text if key == "payme" else ""}
            for key, text in texts if text]
