#!/usr/bin/env python3
"""The HMAC-SHA256 of a backup archive (encrypt-then-MAC), so that a restore can tell a changed archive
before it decrypts anything: `openssl enc -aes-256-cbc` carries no integrity of its own (audit 2026-11-11).

    archive-mac.py sign   <archive> <passphrase file>              prints the MAC (hex)
    archive-mac.py verify <archive> <passphrase file> <mac file>   exits 0 when it matches, 1 when not

The key is HMAC-SHA256(first line of the passphrase file, a fixed label): the passphrase the archive is
encrypted with (openssl's `-pass file:` reads that same first line), kept apart from the encryption key
by the label. Standard library only; nothing secret goes on a command line."""
import hashlib
import hmac
import sys

LABEL = b"pay-invoicing backup archive mac v1"


def _key(passphrase_file: str) -> bytes:
    with open(passphrase_file, "rb") as fh:
        line = fh.readline().rstrip(b"\r\n")
    if not line:
        sys.exit("the passphrase file is empty")
    return hmac.new(line, LABEL, hashlib.sha256).digest()


def _mac(archive: str, key: bytes) -> str:
    h = hmac.new(key, digestmod=hashlib.sha256)
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "sign":
        print(_mac(argv[1], _key(argv[2])))
        return 0
    if len(argv) == 4 and argv[0] == "verify":
        with open(argv[3], encoding="ascii", errors="replace") as fh:
            expected = fh.read().strip()
        return 0 if hmac.compare_digest(_mac(argv[1], _key(argv[2])), expected) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
