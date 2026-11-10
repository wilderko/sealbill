"""Model fields whose values are Fernet tokens in the database.

Reading while the vault is sealed raises VaultSealed. A token that cannot be
opened raises UndecryptableValue; the ciphertext is never handed back.
"""

import hashlib
import hmac
import json
import logging

from cryptography.fernet import InvalidToken
from django.core import checks, validators
from django.db import NotSupportedError, models
from django.db.models import NOT_PROVIDED

from apps.vault.vault import vault

logger = logging.getLogger(__name__)


class UndecryptableValue(Exception):
    """A stored token could not be decrypted with the current key."""


class _EncryptedMixin:
    def get_lookup(self, lookup_name):
        # Each encryption uses a fresh IV, so equality can never match.
        if lookup_name != "isnull":
            raise NotSupportedError(f"encrypted field {self.name}: look it up through a blind index")
        return super().get_lookup(lookup_name)

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        if self.has_default() and self.default is not None:
            errors.append(
                checks.Error(
                    "Encrypted fields must not have a default; use null=True.",
                    obj=self,
                    id="vault.E002",
                )
            )
        if self.unique or self.db_index:
            errors.append(
                checks.Error(
                    "Encrypted fields cannot be unique or indexed; use a blind index.",
                    obj=self,
                    id="vault.E003",
                )
            )
        return errors

    def _serialize(self, value) -> str:
        return value if isinstance(value, str) else str(value)

    def _deserialize(self, text: str):
        return text

    def get_prep_value(self, value):
        if value is None:
            return None
        return vault.fernet().encrypt(self._serialize(value).encode("utf-8")).decode("ascii")

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        fernet = vault.fernet()  # VaultSealed propagates
        try:
            text = fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            model = getattr(self, "model", None)
            logger.error(
                "Cannot decrypt %s.%s: stored value does not open with the current key",
                model.__name__ if model else "?",
                getattr(self, "name", "?"),
            )
            raise UndecryptableValue(f"{self.name}: value cannot be decrypted") from None
        return self._deserialize(text)


class EncryptedTextField(_EncryptedMixin, models.TextField):
    pass


class EncryptedCharField(_EncryptedMixin, models.TextField):
    """Stored in a text column (ciphertext is longer); max_length checks the plaintext only."""

    def __init__(self, *args, max_length=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_length = max_length
        if max_length is not None:
            self.validators.append(validators.MaxLengthValidator(max_length))

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        if self.max_length is None:
            errors.append(checks.Error("EncryptedCharField needs max_length.", obj=self, id="vault.E001"))
        return errors

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["max_length"] = self.max_length
        return name, path, args, kwargs

    def formfield(self, **kwargs):
        from django import forms

        return super().formfield(**{"max_length": self.max_length, "widget": forms.TextInput, **kwargs})


class EncryptedJSONField(_EncryptedMixin, models.TextField):
    def _serialize(self, value) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _deserialize(self, text: str):
        return json.loads(text)

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        # JSON null is stored as NULL, like every other encrypted field.
        return super().get_prep_value(value)

    def formfield(self, **kwargs):
        from django import forms

        return super().formfield(**{"form_class": forms.JSONField, **kwargs})


class BlindIndexField(models.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 64)
        kwargs.setdefault("db_index", True)
        super().__init__(*args, **kwargs)


def blind_index(value: str) -> str:
    key = vault.derive(b"blind-index")
    return hmac.new(key, value.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
