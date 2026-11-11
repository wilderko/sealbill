"""Password-unlocked vault: PBKDF2 -> Fernet key, kept in RAM and in a tmpfs file."""
import base64
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings

logger = logging.getLogger(__name__)

SENTINEL = b"liberation-pay-vault"
ITERATIONS = 600_000
# Every encrypted column and the blind-index key hang on this one password, and vault.json (salt and
# sentinel) is in every backup: a short one is guessed offline (audit DATA-5, WEB-7).
MIN_PASSWORD_LENGTH = 16


class VaultSealed(Exception):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


class Vault:
    """The key, once unsealed, stays tied to the vault.json it opened.

    A restore can put another installation's vault.json in place under a running process (audit
    DATA-2). Kept unchecked, the old key failed every restored letter for good and encrypted every
    new row with a key the restored vault cannot derive. So each use costs one `stat` of vault.json:
    when its inode or mtime changed, the key is tried against the new sentinel, and dropped from
    this process (sealed) if it no longer opens it. The key file is left alone: another worker may
    have just unsealed under the new vault.json and written its key there, which is then picked up.
    A vault.json that cannot be read is no verdict, and the key stays until a readable one appears.

    Each use also costs one `stat` of the key file: sealing removes it (`seal`, `manage.py seal_vault`),
    and every process holding the key then drops it on its next use.
    """

    def __init__(self):
        self._key = None  # urlsafe-b64 Fernet key
        self._lock = threading.Lock()
        self._seen = None
        self._config_seen = None  # vault.json's identity when the key was taken

    # -- paths are read at call time so settings overrides apply
    @staticmethod
    def _config_path() -> Path:
        return Path(settings.VAULT_CONFIG_PATH)

    @staticmethod
    def _key_path() -> Path:
        return Path(settings.VAULT_KEY_PATH)

    def _read_config(self):
        return json.loads(self._config_path().read_text())

    def _config_identity(self):
        """(path, inode, mtime, size) of vault.json, or None when it cannot be looked at. The size
        tells a file caught half-written from the finished one when both carry the same mtime."""
        path = self._config_path()
        try:
            st = path.stat()
        except OSError:
            return None
        return (str(path), st.st_ino, st.st_mtime_ns, st.st_size)

    def _check_config(self) -> None:
        """Called with the lock held and a key in hand: drop the key if vault.json was replaced by
        one it does not open. One that cannot be read (gone, unreadable, not JSON) is "could not
        look", never "another vault": the key stays, and the next change of the file is checked."""
        ident = self._config_identity()
        if ident == self._config_seen:
            return
        self._config_seen = ident  # settled either way below: the same file is not read again
        try:
            sentinel = self._read_config()["sentinel"].encode()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Vault config %s cannot be read; keeping the key until it can", self._config_path())
            return
        try:
            opens = Fernet(self._key).decrypt(sentinel) == SENTINEL
        except InvalidToken:
            opens = False
        if opens:
            return  # the same vault restored, or vault.json merely touched
        logger.error("Vault config %s was replaced and the key does not open it; sealed until it is "
                     "unsealed with that vault's password", ident[0])
        self._key = None
        self._config_seen = None

    def _key_opens_sentinel(self, key: bytes) -> bool:
        try:
            cfg = self._read_config()
            return Fernet(key).decrypt(cfg["sentinel"].encode()) == SENTINEL
        except (InvalidToken, OSError, ValueError, KeyError, TypeError, AttributeError):  # a wrong shape too
            return False

    def _load_from_file(self):
        """While sealed, (re)read the key file whenever its identity changes, so a
        key written by another worker or by unseal_vault is picked up."""
        try:
            st = self._key_path().stat()
        except OSError:
            self._seen = None
            return
        ident = (str(self._key_path()), st.st_ino, st.st_mtime_ns, st.st_size)
        if self._seen == ident:
            return  # already tried this exact file (logged once if bad)
        self._seen = ident
        try:
            key = self._key_path().read_bytes().strip()
        except OSError:
            self._seen = None
            return
        config = self._config_identity()
        if self._key_opens_sentinel(key):
            self._key = key
            self._config_seen = config
        else:
            logger.error("Vault key file %s does not open the sentinel; staying sealed", ident[0])

    def _check_key_file(self) -> None:
        """Called with the lock held and a key in hand: drop the key once the key file is gone. Sealing
        removes that file, so every process that took the key from it seals on its next use, not only
        the one that sealed (audit 2026-11-11: the web workers and the worker kept the key in memory,
        and sealing in an incident stopped nothing). A file that cannot be looked at is no verdict."""
        try:
            os.stat(self._key_path())
        except FileNotFoundError:
            logger.warning("Vault key file is gone; this process is sealed too")
            self._key = None
            self._seen = None
            self._config_seen = None
        except OSError:
            pass

    def _current_key(self):
        with self._lock:
            if self._key is not None:
                self._check_config()
            if self._key is not None:
                self._check_key_file()
            if self._key is None:
                self._load_from_file()
            return self._key

    def is_unsealed(self) -> bool:
        return self._current_key() is not None

    def init(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"The vault password needs at least {MIN_PASSWORD_LENGTH} characters: "
                             "use a generated passphrase.")
        path = self._config_path()
        if path.exists():
            raise FileExistsError(f"Vault already initialised: {path}")
        salt = os.urandom(16)
        key = _derive_key(password, salt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "salt": base64.b64encode(salt).decode(),
            "sentinel": Fernet(key).encrypt(SENTINEL).decode(),
        }))
        os.chmod(path, 0o600)

    def unseal(self, password: str) -> bool:
        config = self._config_identity()  # before reading: a later change is checked on next use
        try:
            cfg = self._read_config()
            salt = base64.b64decode(cfg["salt"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):  # a wrong shape stays sealed
            return False
        key = _derive_key(password, salt)
        if not self._key_opens_sentinel(key):
            return False
        key_path = self._key_path()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = key_path.with_name(f".{key_path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, key_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self._lock:
            self._key = key
            self._seen = None
            self._config_seen = config
        return True

    def seal(self) -> None:
        with self._lock:
            self._key = None
            self._seen = None
            self._config_seen = None
        try:
            self._key_path().unlink()
        except FileNotFoundError:
            pass

    def _require_key(self) -> bytes:
        key = self._current_key()  # read once: another thread may seal between a check and a read
        if key is None:
            raise VaultSealed("The vault is sealed")
        return key

    def fernet(self) -> Fernet:
        return Fernet(self._require_key())

    def derive(self, purpose: bytes) -> bytes:
        raw = base64.urlsafe_b64decode(self._require_key())
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(raw)


vault = Vault()
