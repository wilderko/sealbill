import hashlib
import secrets


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    plain = secrets.token_urlsafe(32)
    return plain, hash_token(plain)


def resolve(plain: str, which: str):
    """Find the invoice a view ('view') or pay ('pay') token belongs to, or None."""
    from apps.invoices.models import Invoice

    if which not in ("view", "pay") or not plain:
        return None
    return Invoice.objects.filter(**{f"{which}_token_hash": hash_token(plain)}).first()
