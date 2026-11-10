"""Deciding whether an uploaded key may be kept: it must be one public key that can
encrypt, is neither expired nor revoked, and names one of the contact's addresses.

Anything that holds a private key is refused outright, PKCS#12 included. Nothing here
logs: what is being inspected is the client's key, and a rejection carries only a code.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.utils import timezone

from apps.keys import gpg

MAX_BYTES = 64 * 1024

CODES = (
    "too_large",
    "pkcs12_refused",
    "secret_key",
    "not_a_key",
    "several_keys",
    "cannot_encrypt",
    "expired",
    "not_yet_valid",
    "revoked",
    "email_mismatch",
    "unsupported_key",
)

# RSA below this is refused, for S/MIME and for PGP encryption keys alike.
MIN_RSA_BITS = 2048
# Curves `openssl cms` encrypts to with ECDH (the S/MIME EC path of the envelope).
SMIME_CURVES = {"secp256r1", "secp384r1", "secp521r1", "brainpoolP256r1", "brainpoolP384r1", "brainpoolP512r1"}
# OpenPGP public-key algorithm ids whose strength is their bit length: RSA (1, 2) and Elgamal (16, 20).
PGP_LENGTH_ALGOS = {"1", "2", "16", "20"}

_PRIVATE_PEM = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PGP_SECRET = re.compile(rb"-----BEGIN PGP (?:PRIVATE|SECRET) KEY BLOCK-----")
_PGP_PUBLIC = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"
_PEM_CERT = re.compile(rb"-----BEGIN (?:X509 )?CERTIFICATE-----")
# The last <...> of a user id, or a user id that is a bare address.
_UID_ADDRESS = re.compile(r"<([^<>\s]+)>\s*$")


class KeyRejected(Exception):
    """The key cannot be kept. `code` is one of CODES and is all a rejection says."""

    def __init__(self, code: str):
        if code not in CODES:
            raise ValueError(f"unknown rejection code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class KeyInfo:
    kind: str  # "pgp" | "smime"
    fingerprint: str  # uppercase hex, no spaces: the OpenPGP fingerprint, or SHA-256 of the certificate DER
    expires_at: datetime | None  # when encryption to it stops working; None: never
    material: str  # normalized: an armored public key, or one PEM certificate


def inspect(data: bytes | str, emails: list[str], *, now: datetime | None = None) -> KeyInfo:
    """Work out what `data` is and whether it may be kept for a contact with `emails`.

    Raises KeyRejected with a code; returns the normalized key otherwise. Raises gpg.GpgFailed
    (or OSError, when there is no gpg) when it could not look: that is not a rejection.
    """
    raw = _as_bytes(data)
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise ValueError("now must be timezone-aware")
    wanted = {e.strip().lower() for e in emails if e and e.strip()}

    # Private keys first, so a certificate with its key pasted below it is refused for the key.
    if _PGP_SECRET.search(raw) or _PRIVATE_PEM.search(raw):
        raise KeyRejected("secret_key")
    if _is_pkcs12(raw):
        raise KeyRejected("pkcs12_refused")

    has_pgp_armor = _PGP_PUBLIC in raw
    cert_blocks = len(_PEM_CERT.findall(raw))
    if has_pgp_armor and cert_blocks:
        raise KeyRejected("several_keys")
    if has_pgp_armor:
        return _inspect_pgp(raw, wanted, now)
    if cert_blocks:
        if cert_blocks > 1:
            raise KeyRejected("several_keys")
        return _inspect_smime(_load_pem_cert(raw), wanted, now)

    tag = _openpgp_first_packet_tag(raw)
    if tag in (5, 7):  # secret key / secret subkey packet
        raise KeyRejected("secret_key")
    if tag == 6:  # public key packet
        return _inspect_pgp(raw, wanted, now)

    try:
        cert = x509.load_der_x509_certificate(raw)
    except ValueError:
        cert = None
    if cert is not None:
        return _inspect_smime(cert, wanted, now)
    if _is_der_private_key(raw):
        raise KeyRejected("secret_key")
    raise KeyRejected("not_a_key")


# --- input -----------------------------------------------------------------------------------


def _as_bytes(data: bytes | str) -> bytes:
    if isinstance(data, str):
        if len(data) > MAX_BYTES:
            raise KeyRejected("too_large")
        try:
            data = data.encode("utf-8")
        except UnicodeEncodeError:
            raise KeyRejected("not_a_key") from None
    elif not isinstance(data, bytes | bytearray | memoryview):
        raise KeyRejected("not_a_key")
    raw = bytes(data)
    if len(raw) > MAX_BYTES:
        raise KeyRejected("too_large")
    return raw


def _der_header(raw: bytes, pos: int) -> tuple[int, int, int] | None:
    """(tag, content start, content length) of the DER element at `pos`, or None."""
    if pos + 2 > len(raw):
        return None
    tag, first = raw[pos], raw[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if not 1 <= n <= 4 or pos + n > len(raw):
            return None
        length = int.from_bytes(raw[pos:pos + n], "big")
        pos += n
    if pos + length > len(raw):  # the content must be there, or this is not DER
        return None
    return tag, pos, length


def _looks_like_pfx(raw: bytes) -> bool:
    """PFX ::= SEQUENCE { version INTEGER (3), authSafe ContentInfo (SEQUENCE { OID, ... }) }.

    Matched on structure alone, so a PKCS#12 file is recognised whatever its password.
    A certificate's outer SEQUENCE starts with another SEQUENCE, a private key's with
    version 0 or 1, so neither can look like this."""
    outer = _der_header(raw, 0)
    if not outer or outer[0] != 0x30:
        return False
    version = _der_header(raw, outer[1])
    if not version or version[0] != 0x02 or version[2] != 1 or raw[version[1]] != 3:
        return False
    auth_safe = _der_header(raw, version[1] + 1)
    if not auth_safe or auth_safe[0] != 0x30:
        return False
    content_type = _der_header(raw, auth_safe[1])
    return bool(content_type and content_type[0] == 0x06)


def _is_pkcs12(raw: bytes) -> bool:
    if _looks_like_pfx(raw):
        return True
    for password in (None, b""):  # no password, and the empty one some tools write
        try:
            pkcs12.load_key_and_certificates(raw, password)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            continue
        return True
    return False


def _is_der_private_key(raw: bytes) -> bool:
    try:
        serialization.load_der_private_key(raw, password=None)
    except TypeError:  # an encrypted private key: still a private key
        return True
    except (ValueError, UnsupportedAlgorithm):
        return False
    return True


def _openpgp_first_packet_tag(raw: bytes) -> int | None:
    if not raw or not raw[0] & 0x80:
        return None
    if raw[0] & 0x40:  # new packet format
        return raw[0] & 0x3F
    return (raw[0] >> 2) & 0x0F


def _matches(address: str, wanted: set[str]) -> bool:
    return address.strip().lower() in wanted


# --- OpenPGP ---------------------------------------------------------------------------------


def _epoch(value: str) -> int | None:
    return int(value) if value and value.isdigit() else None


def _inspect_pgp(raw: bytes, wanted: set[str], now: datetime) -> KeyInfo:
    now_ts = int(now.timestamp())
    with gpg.throwaway() as g:
        imported = g.import_keys(raw)
        gpg.raise_if_import_failed(imported)  # gpg's own failure is not "not a key"
        if getattr(imported, "sec_read", 0):
            raise KeyRejected("secret_key")
        keys = g.list_keys()
        if not keys:
            raise KeyRejected("not_a_key")
        if len(keys) > 1:
            raise KeyRejected("several_keys")
        key = keys[0]
        fingerprint = (key.get("fingerprint") or "").upper()
        if not fingerprint:
            raise KeyRejected("not_a_key")

        primary_expires = _epoch(key.get("expires", ""))
        if key.get("trust") == "r":
            raise KeyRejected("revoked")
        if key.get("trust") == "i":
            raise KeyRejected("cannot_encrypt")
        created = _epoch(key.get("date", ""))
        if created is not None and created > now_ts:
            raise KeyRejected("not_yet_valid")
        if primary_expires is not None and primary_expires <= now_ts:
            raise KeyRejected("expired")

        expires_at = _pgp_encryption_expiry(key, primary_expires, now_ts)

        uids = [uid for uid in key.get("uids", [])
                if keys.uid_map.get(uid, {}).get("trust") not in ("r", "i")]
        if not any(_matches(addr, wanted) for addr in map(_uid_address, uids) if addr):
            raise KeyRejected("email_mismatch")

        material = g.export_keys(fingerprint, minimal=True)
        if not material or _PGP_PUBLIC.decode() not in material:
            raise KeyRejected("not_a_key")
    return KeyInfo(kind="pgp", fingerprint=fingerprint, expires_at=expires_at, material=material)


def _pgp_encryption_expiry(key: dict, primary_expires: int | None, now_ts: int) -> datetime | None:
    """When the last usable encryption key (primary or subkey) stops working.

    Raises KeyRejected when there is none: `cannot_encrypt` when nothing on the key can
    encrypt, otherwise the reason the newest encryption-capable one cannot be used."""
    candidates = []
    if "e" in (key.get("cap") or ""):  # the primary's own (lowercase) capabilities
        candidates.append(key)
    candidates += [info for info in (key.get("subkey_info") or {}).values() if "e" in (info.get("cap") or "")]
    if not candidates:
        raise KeyRejected("cannot_encrypt")

    usable, refused = [], []
    for cand in candidates:
        created = _epoch(cand.get("date", "")) or 0
        expires = _epoch(cand.get("expires", ""))
        if cand.get("trust") == "r":
            reason = "revoked"
        elif cand.get("trust") == "i":
            reason = "cannot_encrypt"
        elif created > now_ts:
            reason = "not_yet_valid"
        elif expires is not None and expires <= now_ts:
            reason = "expired"
        elif cand.get("algo") in PGP_LENGTH_ALGOS and int(cand.get("length") or 0) < MIN_RSA_BITS:
            reason = "unsupported_key"
        else:
            ends = [t for t in (expires, primary_expires) if t is not None]
            usable.append(min(ends) if ends else None)
            continue
        refused.append((created, reason))
    if not usable:
        raise KeyRejected(max(refused)[1])
    if None in usable:
        return None
    return datetime.fromtimestamp(max(usable), tz=UTC)


def _uid_address(uid: str) -> str | None:
    """The address in a user id as python-gnupg lists it. It decodes gpg's output as
    Latin-1 while gpg prints user ids in UTF-8, so the text is turned back first."""
    try:
        uid = uid.encode("latin-1").decode("utf-8")
    except UnicodeError:
        pass  # not UTF-8 underneath: keep what was listed
    match = _UID_ADDRESS.search(uid)
    if match:
        return match.group(1)
    bare = uid.strip()
    return bare if "@" in bare and not any(c.isspace() for c in bare) else None


# --- S/MIME ----------------------------------------------------------------------------------


def _load_pem_cert(raw: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(raw)
    except ValueError:
        raise KeyRejected("not_a_key") from None


def _inspect_smime(cert: x509.Certificate, wanted: set[str], now: datetime) -> KeyInfo:
    if now < cert.not_valid_before_utc:
        raise KeyRejected("not_yet_valid")
    if now > cert.not_valid_after_utc:
        raise KeyRejected("expired")
    try:
        return _check_certificate(cert, wanted)
    except (ValueError, UnsupportedAlgorithm, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
        # A malformed key, extension or name only surfaces when it is first read.
        raise KeyRejected("not_a_key") from None


def _check_certificate(cert: x509.Certificate, wanted: set[str]) -> KeyInfo:
    public_key = cert.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < MIN_RSA_BITS:
            raise KeyRejected("unsupported_key")
        needed = "key_encipherment"
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        if public_key.curve.name not in SMIME_CURVES:
            raise KeyRejected("unsupported_key")
        needed = "key_agreement"
    else:
        raise KeyRejected("unsupported_key")

    extensions = cert.extensions
    try:
        usage = extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        usage = None  # no KeyUsage: every use is allowed (RFC 5280 4.2.1.3)
    if usage is not None and not getattr(usage, needed):
        raise KeyRejected("cannot_encrypt")
    try:
        purposes = extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        purposes = None
    # RFC 8550 4.4.4: either emailProtection or anyExtendedKeyUsage permits e-mail protection.
    if purposes is not None and not {ExtendedKeyUsageOID.EMAIL_PROTECTION,
                                     ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE} & set(purposes):
        raise KeyRejected("cannot_encrypt")

    addresses = [attr.value for attr in cert.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS)]
    try:
        san = extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        san = None
    if san is not None:
        addresses += san.get_values_for_type(x509.RFC822Name)
    if not any(isinstance(a, str) and _matches(a, wanted) for a in addresses):
        raise KeyRejected("email_mismatch")

    return KeyInfo(
        kind="smime",
        fingerprint=cert.fingerprint(hashes.SHA256()).hex().upper(),
        expires_at=cert.not_valid_after_utc,
        material=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )
