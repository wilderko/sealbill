from apps.vault.fields import _EncryptedMixin


def encrypted_field_names(model) -> list[str]:
    """Names of the encrypted fields of a model (class or instance), in field order."""
    return [f.name for f in model._meta.get_fields() if isinstance(f, _EncryptedMixin)]


def plaintext_columns(model) -> tuple[str, ...]:
    """The model's concrete columns that are not encrypted, by attname (`org_id`, not `org`).

    Pass them to `.only()` to load or lock a row without touching the vault."""
    encrypted = set(encrypted_field_names(model))
    return tuple(f.attname for f in model._meta.concrete_fields if f.name not in encrypted)
