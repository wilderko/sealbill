"""How a key becomes a contact's key, and how it stops being one.

Staff and agents are trusted to name the right key, so theirs is active at once. A client
uploads through an invoice link, which can be forwarded, so their key waits as `pending` until
someone answers the letter encrypted to that very key: only its holder can read the link
inside. Whenever a key becomes active the contact's previous active key, and any key that
failed earlier, become `replaced`; a stale failure must not block the contact again once its
successor is revoked.

Whoever reads the client's mailbox and holds a forwarded link can answer that letter too. So when
the contact already has an active key, the upload also sends a notice encrypted to that key, with
the new fingerprint and a cancel link (audit DATA-6). Cancelling refuses the new key; if it was
confirmed meanwhile, the key it pushed out is active again, unless staff have put another key in
place since. The cancel link works once, for CANCEL_VALID_FOR, and only while the key the notice
went to is neither revoked nor failed. A cancel never leaves the contact without the key that
protects their mail: when the old key cannot come back, the new one stays and the cancel is
refused. Staff are told of every cancel and every refusal (`staff.alert`, kind `key_cancel`).

Every change of a key's state is followed, in the same transaction, by the keys mirrored from it
onto the US-resident issuer's copies of the contact (`mirroring.key_changed`).

`inspect` decides what may be kept. When gpg itself could not look (`gpg.GpgFailed`, or an
`OSError` such as no gpg at all) that is KeyCheckUnavailable, "try again": never a verdict on
the key, and nothing is stored. Audit entries name the key by id, never its material.
"""

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.invoices import tokens
from apps.keys import delivery, gpg, mirroring, validation
from apps.keys.models import CANCEL_VALID_FOR, CONFIRM_VALID_FOR, EncryptionKey
from apps.vault.fields import UndecryptableValue

SOURCES = ("staff", "api", "client")
# Earlier keys a newly active one takes the place of.
SUPERSEDED = ("active", "failed")


class KeyCheckUnavailable(Exception):
    """The key could not be checked right now (gpg failed on our side). Nothing was stored."""


class CancelRefused(Exception):
    """A cancel that would leave the contact without the key protecting their mail; nothing changed.

    `code`: "replaced_key_withdrawn" (the key the notice went to was revoked or failed since) or
    "cannot_restore" (the new key is in use, and the old one cannot be made active again)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Refused(Exception):
    """Raised inside the cancel's transaction to roll it back."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def format_fingerprint(fingerprint: str) -> str:
    """A fingerprint in groups of four, the way people compare them: `5872 21D5 F22B ...`."""
    plain = (fingerprint or "").replace(" ", "").upper()
    return " ".join(plain[i:i + 4] for i in range(0, len(plain), 4))


def contact_addresses(contact_id: int) -> list[str]:
    return [a for a in ContactEmail.objects.filter(contact_id=contact_id).values_list("address", flat=True) if a]


def check(data, contact_id: int, *, now=None) -> validation.KeyInfo:
    """`inspect` against the contact's addresses. Raises KeyRejected, or KeyCheckUnavailable
    when gpg could not look."""
    try:
        return validation.inspect(data, contact_addresses(contact_id), now=now)
    except (gpg.GpgFailed, OSError):
        raise KeyCheckUnavailable() from None


def add_key(contact, data, *, source: str, actor=None) -> EncryptionKey:
    """Validate `data` and keep it as a key of `contact`.

    `staff` and `api`: active at once. `client`: pending, with a confirmation token (32 random
    bytes; only its sha256 is stored, the link works for CONFIRM_VALID_FOR) and a `key_confirm`
    letter queued, encrypted to this key. When the contact has an active key, a `key_replacement`
    notice is queued too, encrypted to that key, with a cancel token stored the same way. A client
    uploading a key that is already pending for the contact, with a link still working, gets that
    row back: no second row, no second letter.
    Raises KeyRejected or KeyCheckUnavailable; then nothing is stored and nothing is queued.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown key source {source!r}")
    info = check(data, contact.pk)
    actor = actor if getattr(actor, "pk", None) else None
    now = timezone.now()
    with transaction.atomic():
        _lock_contact(contact.pk)
        fields = {"contact": contact, "kind": info.kind, "material": info.material, "fingerprint": info.fingerprint,
                  "expires_at": info.expires_at, "source": source, "created_by": actor}
        replaced = []
        if source == "client":
            twin = _pending_twin(contact.pk, info, now)
            if twin is not None:
                return twin  # its letter is on its way already; nothing changed, nothing to audit
            plain, digest = tokens.new_token()
            current = EncryptionKey.objects.filter(contact_id=contact.pk, state="active").only("pk").first()
            cancel_plain, cancel_digest = tokens.new_token() if current is not None else (None, None)
            key = EncryptionKey.objects.create(**fields, state="pending", confirm_token_hash=digest,
                                               confirm_sent_at=now, replaces=current,
                                               cancel_token_hash=cancel_digest)
            _queue_confirmation(contact, key, plain)
            if current is not None:
                _queue_replacement_notice(contact, key, current, cancel_plain)
        else:
            replaced = _supersede(contact.pk)
            key = EncryptionKey.objects.create(**fields, state="active")
        audit.record(actor, "key.add", key,
                     {"kind": key.kind, "source": source, "state": key.state, "contact": contact.pk,
                      "replaced": replaced},
                     actor_name="client" if source == "client" else "")
        if key.state == "active":
            mirroring.key_changed(contact.pk, actor=actor)
    return key


def find_by_token(token: str) -> EncryptionKey | None:
    """The key a confirmation token was issued for, in whatever state it is now, or None."""
    if not token:
        return None
    return (EncryptionKey.objects.select_related("contact__org")
            .filter(confirm_token_hash=tokens.hash_token(token)).first())


def confirmation_problem(key: EncryptionKey | None, *, now=None) -> str | None:
    """Why this confirmation cannot happen ("unknown", "used", "expired"), or None. Reads only."""
    if key is None:
        return "unknown"
    if key.state != "pending":
        return "used"
    now = now or timezone.now()
    if key.confirm_sent_at is None or now >= key.confirm_sent_at + CONFIRM_VALID_FOR:
        return "expired"
    return None


def confirm_key(token: str, *, now=None) -> EncryptionKey:
    """A person pressed Confirm: the pending key becomes active, replacing the contact's
    earlier active (and failed) keys.

    Raises KeyError("unknown"|"used"|"expired"). The key is checked again first, against the
    contact's addresses as they are now: KeyRejected when it no longer works (it stays pending,
    it is never activated), KeyCheckUnavailable when gpg could not look.
    """
    now = now or timezone.now()
    key = find_by_token(token)
    problem = confirmation_problem(key, now=now)
    if problem:
        raise KeyError(problem)
    info = check(key.material, key.contact_id, now=now)
    if (info.kind, info.fingerprint) != (key.kind, key.fingerprint):
        raise validation.KeyRejected("not_a_key")  # not the key that was uploaded: never activate it
    with transaction.atomic():
        _lock_contact(key.contact_id)
        replaced = _supersede(key.contact_id, exclude=key.pk)
        changed = EncryptionKey.objects.filter(pk=key.pk, state="pending").update(state="active", confirmed_at=now)
        if not changed:
            raise KeyError("used")  # confirmed (or revoked) a moment ago: the transaction rolls back
        audit.record(None, "key.confirm", key, {"contact": key.contact_id, "replaced": replaced},
                     actor_name="client")
        mirroring.key_changed(key.contact_id)
    key.state, key.confirmed_at = "active", now
    return key


def find_by_cancel_token(token: str) -> EncryptionKey | None:
    """The uploaded key a replacement notice's cancel token was issued for, or None."""
    if not token:
        return None
    return (EncryptionKey.objects.select_related("contact__org")
            .filter(cancel_token_hash=tokens.hash_token(token)).first())


def cancel_problem(key: EncryptionKey | None, *, now=None) -> str | None:
    """Why this cancel link cannot be used ("unknown", "used", "expired", "withdrawn"), or None.
    "withdrawn": the key the notice went to was revoked or failed since (or is gone), so nothing can
    be handed back to it. Reads only."""
    if key is None:
        return "unknown"
    if key.cancelled_at is not None:
        return "used"
    now = now or timezone.now()
    if key.confirm_sent_at is None or now >= key.confirm_sent_at + CANCEL_VALID_FOR:
        return "expired"
    state = (EncryptionKey.objects.filter(pk=key.replaces_id).values_list("state", flat=True).first()
             if key.replaces_id is not None else None)
    if state is None or state in delivery.WITHDRAWN_STATES:
        return "withdrawn"
    return None


def _restorable(old: dict) -> bool:
    """May the key a cancel hands back become active again? Only one the new key pushed out
    (`replaced`) that never failed (a failed key keeps its failure when superseded), and never a
    mirror: the mirroring rules decide those (`mirroring.key_changed`)."""
    return (old["state"] == "replaced" and old["source"] != mirroring.MIRROR
            and not old["failed_while"] and not old["last_error_kind"])


def cancel_replacement(token: str, *, now=None) -> tuple[EncryptionKey, int | None]:
    """The holder of the key that was active pressed Cancel: the uploaded key is refused (revoked,
    whatever state it reached) and never used. If it was in use (active, or failed while active, which
    holds the contact's mail back), the key it replaced is made active again, unless another key is
    active by now (staff added it; it stays). Returns (the uploaded key, the restored key's id or None).

    A cancel never leaves the contact's mail unprotected. If the new key is in use and nothing would
    protect the contact without it, nothing changes and CancelRefused("cannot_restore") is raised;
    CancelRefused("replaced_key_withdrawn") when the key the notice went to was revoked or failed.
    Raises KeyError("unknown"|"used"|"expired"). Every cancel and every refusal is audited and raises
    one `staff.alert` (ids only)."""
    now = now or timezone.now()
    key = find_by_cancel_token(token)
    problem = cancel_problem(key, now=now)
    if problem == "withdrawn":
        _refuse(key, "replaced_key_withdrawn")
    if problem:
        raise KeyError(problem)
    try:
        with transaction.atomic():
            _lock_contact(key.contact_id)
            fresh = (EncryptionKey.objects.select_for_update().filter(pk=key.pk)
                     .values("state", "failed_while", "replaces_id", "cancelled_at").first())
            if fresh is None or fresh["cancelled_at"] is not None:
                raise KeyError("used")  # cancelled a moment ago, by the other click of a double click
            old = (EncryptionKey.objects.select_for_update()
                   .filter(pk=fresh["replaces_id"], contact_id=key.contact_id)
                   .values("state", "source", "failed_while", "last_error_kind").first())
            if old is None or old["state"] in delivery.WITHDRAWN_STATES:
                raise _Refused("replaced_key_withdrawn")
            was = fresh["state"]
            in_use = was == "active" or (was == "failed" and fresh["failed_while"] == "active")
            EncryptionKey.objects.filter(pk=key.pk).update(state="revoked", cancelled_at=now)
            active = EncryptionKey.objects.filter(contact_id=key.contact_id, state="active")
            restored = None
            if in_use and not active.exists() and _restorable(old):
                EncryptionKey.objects.filter(pk=fresh["replaces_id"], state="replaced").update(state="active")
                restored = fresh["replaces_id"]
            mirroring.key_changed(key.contact_id)
            if in_use and not active.exists():
                raise _Refused("cannot_restore")  # rolls back the revocation and the mirrors
            audit.record(None, "key.cancel", key, {"contact": key.contact_id, "was": was, "restored": restored},
                         actor_name="client")
    except _Refused as exc:
        _refuse(key, exc.code)
    _alert_staff(key, "cancelled")
    key.state, key.cancelled_at = "revoked", now
    return key, restored


def _refuse(key: EncryptionKey, code: str):
    audit.record(None, "key.cancel_refused", key, {"contact": key.contact_id, "reason": code}, actor_name="client")
    _alert_staff(key, code)
    raise CancelRefused(code)


def _alert_staff(key: EncryptionKey, reason: str) -> None:
    """One `staff.alert` for the contact's organization: the uploaded key's id and what happened,
    never the contact, the address or the fingerprint."""
    from apps.webhooks import events

    events.staff_alert({"kind": "key_cancel", "key_id": key.pk, "reason": reason}, org=key.contact.org_id)


def revoke_key(key: EncryptionKey, actor) -> EncryptionKey:
    """Withdraw `key`: it is never used again, its confirmation link stops working, and a failed
    key no longer blocks plaintext (the contact then has no usable key). Revoking twice is harmless."""
    with transaction.atomic():
        _lock_contact(key.contact_id)  # the contact's row before key rows, as everywhere
        was = (EncryptionKey.objects.select_for_update().filter(pk=key.pk).values_list("state", flat=True).first())
        if was is not None and was != "revoked":
            EncryptionKey.objects.filter(pk=key.pk, state=was).update(state="revoked")
            audit.record(actor, "key.revoke", key, {"was": was, "contact": key.contact_id})
            mirroring.key_changed(key.contact_id, actor=actor)
    key.state = "revoked"
    return key


def _lock_contact(contact_id: int) -> None:
    """Serialise key changes of one contact (PostgreSQL; SQLite serialises writers anyway)."""
    Contact.objects.select_for_update().filter(pk=contact_id).only("pk").first()


def _pending_twin(contact_id: int, info: validation.KeyInfo, now) -> EncryptionKey | None:
    """The contact's pending key with these kind and fingerprint whose link still works, or None.
    The fingerprint is encrypted, so the (few) pending rows are compared one by one."""
    candidates = EncryptionKey.objects.filter(contact_id=contact_id, state="pending", kind=info.kind,
                                              confirm_sent_at__gt=now - CONFIRM_VALID_FOR)
    for pk in candidates.order_by("-pk").values_list("pk", flat=True):
        try:
            key = EncryptionKey.objects.get(pk=pk)
        except UndecryptableValue:
            continue  # cannot tell: a fresh row is the safe answer
        if key.fingerprint == info.fingerprint:
            return key
    return None


def _supersede(contact_id: int, *, exclude: int | None = None) -> list[int]:
    """Move the contact's active and failed keys to `replaced`; returns their ids.

    A failed mirror stays failed: it follows the original's key, and only a change of that key
    lifts it (apps.keys.mirroring). The new key, being active, wins over it meanwhile."""
    earlier = (EncryptionKey.objects.filter(contact_id=contact_id, state__in=SUPERSEDED)
               .exclude(source=mirroring.MIRROR, state="failed"))
    if exclude is not None:
        earlier = earlier.exclude(pk=exclude)
    pks = sorted(earlier.values_list("pk", flat=True))
    EncryptionKey.objects.filter(pk__in=pks).update(state="replaced")
    return pks


def _queue_confirmation(contact, key: EncryptionKey, plain_token: str) -> None:
    """The `key_confirm` letter: to the contact's primary (else first) address, in their language,
    encrypted to `key` by the outbox. Its link carries the only copy of the token."""
    from django.conf import settings

    from apps.core.i18n import activate_for
    from apps.notify import mail
    from apps.notify import services as notify

    row = contact.emails.filter(primary=True).first() or contact.emails.first()
    language = activate_for(contact)
    notify.enqueue(
        "key_confirm", to=[row.address], language=language, contact=contact, key=key,
        context={"org": mail.org_name(contact.org), "name": mail.contact_name(contact),
                 "fingerprint": format_fingerprint(key.fingerprint),
                 "confirm_url": f"{settings.SITE_URL.rstrip('/')}/k/{plain_token}/"},
    )


def _queue_replacement_notice(contact, key: EncryptionKey, current: EncryptionKey, plain_token: str) -> None:
    """The `key_replacement` notice: to the same address and in the same language as the confirmation,
    encrypted by the outbox to `current`, the key that was active at the upload, or to nobody. It names
    the uploaded key's fingerprint, and its cancel link carries the only copy of the token."""
    from django.conf import settings

    from apps.core.i18n import activate_for
    from apps.notify import mail
    from apps.notify import services as notify

    row = contact.emails.filter(primary=True).first() or contact.emails.first()
    notify.enqueue(
        "key_replacement", to=[row.address], language=activate_for(contact), contact=contact, key=current,
        plain_context={"key": key.pk},
        context={"org": mail.org_name(contact.org), "name": mail.contact_name(contact),
                 "fingerprint": format_fingerprint(key.fingerprint),
                 "cancel_url": f"{settings.SITE_URL.rstrip('/')}/k/{plain_token}/cancel/"},
    )
