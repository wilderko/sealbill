"""Wrapping a whole MIME message in encryption: PGP/MIME (RFC 3156) or S/MIME enveloped-data.

Each function takes the finished inner message (headers, text, HTML and attachments) and
returns the encrypted body of the outer one. The caller adds the outer headers. Nothing here
logs, and every keyring or certificate file it writes is temporary and removed after use.
"""

import io
import os
import shutil
import subprocess
import tempfile
from email import encoders, generator
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.serialization import pkcs7
from django.conf import settings

from apps.keys import gpg
from apps.keys.delivery import EncryptionRefused

OPENSSL_TIMEOUT = 60  # seconds; a stuck openssl must not hold the outbox


class EnvelopeError(Exception):
    """gpg or openssl produced no ciphertext for a key that was just validated: a fault here,
    not in the key. Nothing is sent; the outbox tries again later and the key stays in use."""


class _Flattened:
    """`as_bytes(linesep=...)` as Django's SMTP backend calls it (stdlib messages take a policy)."""

    def as_bytes(self, unixfrom=False, linesep="\n"):
        return flatten(self, linesep=linesep, unixfrom=unixfrom)

    def as_string(self, unixfrom=False, linesep="\n"):
        fp = io.StringIO()
        generator.Generator(fp, mangle_from_=False).flatten(self, unixfrom=unixfrom, linesep=linesep)
        return fp.getvalue()


class _Multipart(_Flattened, MIMEMultipart):
    pass


class _Application(_Flattened, MIMEApplication):
    pass


def flatten(message: Message, *, linesep="\r\n", unixfrom=False) -> bytes:
    """The message as bytes in canonical form (CRLF line endings), which is what is encrypted."""
    fp = io.BytesIO()
    generator.BytesGenerator(fp, mangle_from_=False).flatten(message, unixfrom=unixfrom, linesep=linesep)
    return fp.getvalue()


def _part(data: bytes, subtype: str, **params) -> _Application:
    part = _Application(data, subtype, encoders.encode_7or8bit, **params)
    del part["MIME-Version"]  # only the outermost entity carries it
    return part


def pgp_mime(inner: Message, armored_key: str) -> Message:
    """`multipart/encrypted; protocol="application/pgp-encrypted"` holding `inner`, encrypted to
    the one OpenPGP key in `armored_key` (RFC 3156 section 4)."""
    with gpg.throwaway() as g:
        imported = g.import_keys(armored_key)
        gpg.raise_if_import_failed(imported)
        fingerprints = sorted(set(imported.fingerprints or []))
        if len(fingerprints) != 1:
            raise EnvelopeError("the key did not import as one key")
        # The key was validated just now; trust here means "encrypt to it", not "it is someone".
        result = g.encrypt(flatten(inner), fingerprints[0], always_trust=True, armor=True)
        if not result.ok or not result.data:
            raise EnvelopeError("gpg did not encrypt")  # never its status text: it names the key
        armored = result.data.decode("ascii")

    outer = _Multipart("encrypted", protocol="application/pgp-encrypted")
    outer.preamble = "This is an OpenPGP/MIME encrypted message (RFC 4880 and 3156)"
    version = _part(b"Version: 1\r\n", "pgp-encrypted")
    version["Content-Description"] = "PGP/MIME version identification"
    payload = _part(armored.encode("ascii"), "octet-stream", name="encrypted.asc")
    payload["Content-Description"] = "OpenPGP encrypted message"
    payload.add_header("Content-Disposition", "inline", filename="encrypted.asc")
    outer.attach(version)
    outer.attach(payload)
    return outer


def smime(inner: Message, cert_pem: str) -> Message:
    """`application/pkcs7-mime; smime-type=enveloped-data` holding `inner`, encrypted to the
    certificate in `cert_pem`: RSA by `cryptography`, EC by `openssl cms` (ECDH)."""
    data = flatten(inner)
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    public_key = cert.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        der = _rsa_enveloped(cert, data)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        der = _openssl_enveloped(cert_pem, data)
    else:
        raise EncryptionRefused("unsupported_key")
    outer = _Application(der, "pkcs7-mime", encoders.encode_base64,
                         **{"smime-type": "enveloped-data", "name": "smime.p7m"})
    outer.add_header("Content-Disposition", "attachment", filename="smime.p7m")
    return outer


def _rsa_enveloped(cert: x509.Certificate, data: bytes) -> bytes:
    builder = pkcs7.PKCS7EnvelopeBuilder().set_data(data).add_recipient(cert)
    aes256 = getattr(algorithms, "AES256", None)
    if aes256 is not None and hasattr(builder, "set_content_encryption_algorithm"):
        builder = builder.set_content_encryption_algorithm(aes256)  # else the library's default (AES-128)
    return builder.encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])


def _openssl_enveloped(cert_pem: str, data: bytes) -> bytes:
    binary = shutil.which("openssl")
    if not binary:
        raise EncryptionRefused("openssl_missing")
    base = settings.GPG_TMP_DIR or None
    if base:
        os.makedirs(base, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="cms-", suffix=".pem", dir=base)  # 0600
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(cert_pem)
        done = subprocess.run(
            [binary, "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", "-recip", path],
            input=data, capture_output=True, timeout=OPENSSL_TIMEOUT, check=False,
        )
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    if done.returncode != 0 or not done.stdout:
        raise EnvelopeError(f"openssl cms exited with {done.returncode}")
    return done.stdout
