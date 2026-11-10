"""Whether a gateway credential can only receive (audit DATA-3, MONEY-10b).

The system is watch-only (spec §6.9): it makes invoices and reads them, and holds nothing that
can spend. `set_rail_credentials` stores a credential only once one of these checks has shown,
from what the credential itself says, that it cannot do more. Anything a check cannot read is
refused: refusing is always safe. Messages name operations, methods and permissions, never the
secret.

- LND: a macaroon (binary V2, hex) carries its operations in its identifier, a protobuf
  `MacaroonId` {nonce, storageId, ops: [{entity, actions}]} after a version byte. The signature
  covers the identifier, so a macaroon listing fewer operations than it was baked with is one the
  node refuses: what it lists is what it can do. Caveats only narrow it and are not read.
- Core Lightning: a rune is base64url of a 32-byte hash and its restrictions in the clear.
  Restrictions can be appended without the node's secret but never removed, so a clause that
  lets only `method=` values through binds whoever holds the rune.
- BTCPay: the key's permissions as BTCPay lists them for the key itself (fetched by the caller).
"""

import base64
import binascii
import re
import string

LND_ALLOWED = frozenset({"invoices:read", "invoices:write", "info:read"})
LND_REQUIRED = ("invoices:read", "invoices:write")
LND_BAKE = "lncli bakemacaroon invoices:read invoices:write info:read"

CLN_ALLOWED = frozenset({"invoice", "listinvoices", "waitinvoice", "getinfo"})
CLN_REQUIRED = ("invoice", "listinvoices")
CLN_CREATE = """lightning-cli createrune restrictions='[["method=invoice","method=listinvoices"]]'"""

BTCPAY_ALLOWED = frozenset({
    "btcpay.store.canviewinvoices", "btcpay.store.cancreateinvoice",
    "btcpay.store.webhooks.canmodifywebhooks", "btcpay.store.canviewstoresettings",
})
BTCPAY_REQUIRED = ("btcpay.store.canviewinvoices", "btcpay.store.cancreateinvoice")


class NotReceiveOnly(ValueError):
    """The credential can do more than receive, cannot do the job, or cannot be read. Its text is
    for the operator and never holds the secret."""


# ---------------------------------------------------------------------------
# LND
# ---------------------------------------------------------------------------
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2})+$")
_MACAROON_V2 = 2
_ID_VERSION_3 = 3  # macaroon-bakery's Version3: the identifier is this byte, then a MacaroonId
_EOS, _LOCATION, _IDENTIFIER, _VERIFICATION_ID, _SIGNATURE = 0, 1, 2, 4, 6


def _uvarint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _section(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """One field of a V2 macaroon: (type, payload, next position). End-of-section has no payload."""
    kind, pos = _uvarint(data, pos)
    if kind == _EOS:
        return kind, b"", pos
    length, pos = _uvarint(data, pos)
    end = pos + length
    if end > len(data):
        raise ValueError("truncated field")
    return kind, data[pos:end], end


def _macaroon_identifier(raw: bytes) -> bytes:
    """The identifier of a binary V2 macaroon, after checking the whole layout (go-macaroon's
    `appendBinaryV2`): [location] identifier EOS, caveats ([location] identifier [vid] EOS)*, EOS,
    a 32-byte signature, nothing after it."""
    if not raw or raw[0] != _MACAROON_V2:
        raise ValueError("not a version 2 macaroon")
    kind, payload, pos = _section(raw, 1)
    if kind == _LOCATION:
        kind, payload, pos = _section(raw, pos)
    if kind != _IDENTIFIER or not payload:
        raise ValueError("no identifier")
    identifier = payload
    kind, _, pos = _section(raw, pos)
    if kind != _EOS:
        raise ValueError("identifier not closed")
    while True:
        kind, payload, pos = _section(raw, pos)
        if kind == _EOS:
            break  # the end of the caveats
        if kind == _LOCATION:
            kind, payload, pos = _section(raw, pos)
        if kind != _IDENTIFIER:
            raise ValueError("caveat without an identifier")
        kind, payload, pos = _section(raw, pos)
        if kind == _VERIFICATION_ID:
            kind, payload, pos = _section(raw, pos)
        if kind != _EOS:
            raise ValueError("caveat not closed")
    kind, signature, pos = _section(raw, pos)
    if kind != _SIGNATURE or len(signature) != 32 or pos != len(raw):
        raise ValueError("no signature")
    return identifier


def _length_delimited(data: bytes) -> list[tuple[int, bytes]]:
    """(field number, payload) of a protobuf message whose fields are all length-delimited, as
    MacaroonId's and Op's are. Any other field shape means it is not what LND writes."""
    fields, pos = [], 0
    while pos < len(data):
        key, pos = _uvarint(data, pos)
        number, wire = key >> 3, key & 7
        if wire != 2 or number == 0:
            raise ValueError("unexpected field")
        length, pos = _uvarint(data, pos)
        end = pos + length
        if end > len(data):
            raise ValueError("truncated field")
        fields.append((number, data[pos:end]))
        pos = end
    return fields


def lnd_operations(macaroon_hex: str) -> set[str]:
    """The operations ("entity:action") an LND macaroon grants, read offline. NotReceiveOnly when
    it is not a macaroon this can read (hex of a binary V2 macaroon with a Version3 identifier)."""
    unreadable = NotReceiveOnly(
        "this is not an LND macaroon that can be read here (the hex that `lncli bakemacaroon` prints): "
        "its operations cannot be checked, so nothing was stored")
    if not isinstance(macaroon_hex, str) or not _HEX.match(macaroon_hex):
        raise unreadable
    try:
        identifier = _macaroon_identifier(binascii.unhexlify(macaroon_hex))
        if identifier[0] != _ID_VERSION_3:
            raise ValueError("identifier version")
        ops = set()
        for number, payload in _length_delimited(identifier[1:]):
            if number in (1, 2):  # nonce, storageId
                continue
            if number != 3:
                raise ValueError("unknown MacaroonId field")
            entity, actions = None, []
            for op_number, op_payload in _length_delimited(payload):
                text = op_payload.decode("utf-8")
                if op_number == 1 and entity is None:
                    entity = text
                elif op_number == 2:
                    actions.append(text)
                else:
                    raise ValueError("unknown Op field")
            if not entity or not actions:
                raise ValueError("empty op")
            ops.update(f"{entity}:{action}" for action in actions)
    except (ValueError, IndexError, UnicodeDecodeError, binascii.Error):
        raise unreadable from None
    return ops


def check_lnd(macaroon_hex: str) -> None:
    ops = lnd_operations(macaroon_hex)
    broad = sorted(ops - LND_ALLOWED)
    if broad:
        raise NotReceiveOnly(f"this macaroon can do more than make and read invoices ({', '.join(broad)}): "
                             f"bake one with `{LND_BAKE}` and paste its hex")
    missing = [op for op in LND_REQUIRED if op not in ops]
    if missing:
        raise NotReceiveOnly(f"this macaroon lacks {', '.join(missing)}: bake one with `{LND_BAKE}` "
                             "and paste its hex")


# ---------------------------------------------------------------------------
# Core Lightning
# ---------------------------------------------------------------------------
_RUNE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_CONDITIONS = frozenset("!=/^$~<>{}#")
_HASH_BYTES = 32


def _restrictions(text: str) -> list[list[tuple[str, str, str]]]:
    """A rune's restrictions as clauses (all must pass) of alternatives (any may pass), each
    (field, condition, value). The grammar of Core Lightning's runes: a field runs up to the first
    punctuation, which is the condition; a value runs to an unescaped `|` (next alternative) or
    `&` (next clause), and `\\` takes the next character as it is. Only the first clause may have
    an empty field: it is the rune's unique id."""
    clauses, pos = [], 0
    while pos < len(text):
        clause = []
        while True:
            start = pos
            while pos < len(text) and text[pos] not in string.punctuation:
                pos += 1
            if pos >= len(text) or text[pos] not in _CONDITIONS:
                raise ValueError("no condition")
            field, condition = text[start:pos], text[pos]
            if not field and clauses:
                raise ValueError("an empty field outside the unique id")
            pos += 1
            value = []
            while pos < len(text) and text[pos] not in "|&":
                if text[pos] == "\\":
                    pos += 1
                    if pos >= len(text):
                        raise ValueError("dangling escape")
                value.append(text[pos])
                pos += 1
            clause.append((field, condition, "".join(value)))
            if pos < len(text) and text[pos] == "|":
                pos += 1
                continue
            break
        clauses.append(clause)
        if pos < len(text):  # an `&`
            pos += 1
            if pos >= len(text):
                raise ValueError("empty clause")
    return clauses


def cln_methods(rune: str) -> frozenset[str] | None:
    """The methods a Core Lightning rune can call, or None when its restrictions do not limit them
    to a list. A clause limits them only if every alternative in it is `method=<name>`; several
    such clauses must all pass, so the rune can call what they have in common. NotReceiveOnly when
    it is not a rune that can be read."""
    try:
        if not isinstance(rune, str) or not _RUNE.match(rune):
            raise ValueError("not base64url")
        raw = base64.urlsafe_b64decode(rune + "=" * (-len(rune) % 4))
        if len(raw) < _HASH_BYTES:
            raise ValueError("too short")
        clauses = _restrictions(raw[_HASH_BYTES:].decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise NotReceiveOnly("this is not a Core Lightning rune that can be read here: its restrictions "
                             "cannot be checked, so nothing was stored") from None
    methods = None
    for clause in clauses:
        if all(field == "method" and condition == "=" for field, condition, _ in clause):
            named = frozenset(value for _, _, value in clause)
            methods = named if methods is None else methods & named
    return methods


def check_cln(rune: str) -> None:
    allowed = ", ".join(sorted(CLN_ALLOWED))
    methods = cln_methods(rune)
    if methods is None:
        raise NotReceiveOnly(f"this rune does not limit the methods it may call to {allowed}: create one "
                             f"with `{CLN_CREATE}`")
    broad = sorted(methods - CLN_ALLOWED)
    if broad:
        raise NotReceiveOnly(f"this rune can call more than {allowed} ({', '.join(broad)}): create one "
                             f"with `{CLN_CREATE}`")
    missing = [m for m in CLN_REQUIRED if m not in methods]
    if missing:
        raise NotReceiveOnly(f"this rune lacks {', '.join(missing)}: create one with `{CLN_CREATE}`")


# ---------------------------------------------------------------------------
# BTCPay
# ---------------------------------------------------------------------------
def check_btcpay(permissions, store_id: str) -> None:
    """`permissions` as BTCPay lists them for the key (`GET /api/v1/api-keys/current`): a name,
    optionally scoped to one store (`btcpay.store.canviewinvoices:<store id>`)."""
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise NotReceiveOnly("BTCPay's answer about this key could not be read, so nothing was stored")
    names = [p.partition(":")[0] for p in permissions]
    wanted = ", ".join(sorted(BTCPAY_ALLOWED))
    broad = sorted({name for name in names if name not in BTCPAY_ALLOWED})
    if broad:
        raise NotReceiveOnly(f"this API key can do more than create and read invoices ({', '.join(broad)}): "
                             f"create a key for the store with only {wanted}")
    here = {name for name, _, store in (p.partition(":") for p in permissions) if store in ("", store_id)}
    missing = [name for name in BTCPAY_REQUIRED if name not in here]
    if missing:
        raise NotReceiveOnly(f"this API key lacks {', '.join(missing)} for this store: create a key for the "
                             f"store with only {wanted}")
