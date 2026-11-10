from apps.audit.models import AuditEntry

_PLAIN_TYPES = (int, str, bool, type(None))  # int covers bool; float, Decimal, dates are refused


def check_plain(detail: dict) -> dict:
    """`detail` as a dict of plain facts, or ValueError.

    Every key is a string; every value is an int, str, bool or None, or a list of those.
    Nothing nested, so what is stored in plaintext stays a short list of facts."""
    if not isinstance(detail, dict):
        raise ValueError("the detail must be a dict")
    for key, value in detail.items():
        if not isinstance(key, str):
            raise ValueError(f"detail key {key!r} is not a string")
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, _PLAIN_TYPES):
                raise ValueError(f"detail {key!r} holds a {type(item).__name__}, not a plain value")
    return dict(detail)


def record(actor, action: str, obj, detail: dict | None = None, *, actor_name: str = "") -> AuditEntry:
    """Append an audit entry. `actor` may be None for system actions; `actor_name` then names
    who acted without an account (for example "client" for a claim made on the pay page).

    The detail is encrypted, so this needs the vault unsealed; see `record_system`."""
    actor = actor if getattr(actor, "pk", None) else None
    return AuditEntry.objects.create(
        actor=actor,
        actor_username=(actor.get_username() if actor else actor_name)[:150],
        action=action,
        model_label=obj._meta.label_lower,
        object_pk=str(obj.pk),
        detail=dict(detail or {}),
    )


def record_system(action: str, obj, detail: dict) -> AuditEntry:
    """Append an entry for something the system did on its own; works while the vault is sealed.

    The detail is stored in plaintext (`system_detail`), so callers pass only plain facts: ids,
    amounts, states, network, transaction hash, reason codes. Never a name, an address or free
    text a person typed. `check_plain` refuses anything that is not a flat plain value."""
    detail = check_plain(detail)
    return AuditEntry.objects.create(
        actor=None,
        actor_username="system",
        action=action,
        model_label=obj._meta.label_lower,
        object_pk=str(obj.pk),
        detail=None,
        system_detail=detail,
    )
