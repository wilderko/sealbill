"""Throw-away GnuPG keyrings. Nothing here ever opens a user's own keyring."""

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

import gnupg
from django.conf import settings

# python-gnupg logs every status line gpg prints (key ids, fingerprints, user ids) at DEBUG,
# and a warning for each non-zero exit, which the caller reads off the result anyway.
# Neither belongs in our logs.
logging.getLogger("gnupg").setLevel(logging.ERROR)


class GpgFailed(Exception):
    """gpg could not do its work (a keyring it cannot write, a full tmpfs, a crash). That is
    "could not look", never a verdict on the key; the message never carries gpg's output."""


def raise_if_import_failed(result) -> None:
    """Raise GpgFailed when an import failed on gpg's side rather than for the data.

    Data gpg cannot read (garbage, a truncated packet) ends with IMPORT_RES and no ERROR status;
    a keyring that cannot be written prints ERROR statuses, and a gpg that died reports nothing."""
    statuses = {line.split()[1] for line in (result.stderr or "").splitlines()
                if line.startswith("[GNUPG:] ") and len(line.split()) > 1}
    if "ERROR" in statuses or (getattr(result, "returncode", None) and "IMPORT_RES" not in statuses):
        raise GpgFailed("gpg could not import")


@contextmanager
def throwaway(*, agent: bool = False):
    """Yield a `gnupg.GPG` whose home is a fresh 0700 directory under `GPG_TMP_DIR`
    (the system temp directory when unset), removed on the way out whatever happens.

    Importing, listing and encrypting to public keys need no gpg-agent, so none is started
    unless `agent=True` (only decrypting with a secret key needs one, which tests do). Any
    daemon that did start for this home is stopped before the directory is removed.
    """
    base = settings.GPG_TMP_DIR or None
    if base:
        os.makedirs(base, mode=0o700, exist_ok=True)
    home = tempfile.mkdtemp(prefix="gpg-", dir=base)
    try:
        os.chmod(home, 0o700)
        options = [] if agent else ["--no-autostart"]
        yield gnupg.GPG(gnupghome=home, gpgbinary=settings.GPG_BINARY, options=options)
    finally:
        _stop_daemons(home)
        shutil.rmtree(home, ignore_errors=True)


def _gpgconf() -> str | None:
    gpg_path = shutil.which(settings.GPG_BINARY)
    if gpg_path:
        sibling = os.path.join(os.path.dirname(gpg_path), "gpgconf")
        if os.access(sibling, os.X_OK):
            return sibling
    return shutil.which("gpgconf")


def _stop_daemons(home: str) -> None:
    """Stop any gpg-agent or keyboxd serving `home` and remove its socket directory
    (under /run/user/<uid>/gnupg when that exists, else inside `home`)."""
    gpgconf = _gpgconf()
    if not gpgconf:
        return
    for args in (["--kill", "all"], ["--remove-socketdir"]):
        try:
            subprocess.run([gpgconf, "--homedir", home, *args], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
