"""Encrypting a letter to a contact's key at the moment it is sent, or refusing to send it.

The rule is "refuse rather than downgrade": a contact with an active key never gets a
plaintext e-mail. `protect` checks the key again at send time, with the contact's addresses
as they are now; when it no longer works (expired, revoked, no longer naming the contact,
unusable) it raises EncryptionRefused, the outbox fails the message instead of sending it,
and the key is marked `failed`. A key that failed while active then keeps plaintext from
going to that contact until a person acts: a working key becomes active, or the failed one
is revoked.

A refusal is final for the message, and only what stored, once-accepted material can newly
be counts as one (SEND_TIME_VERDICTS). Anything that is our side's failure is retried like an
SMTP failure and leaves the key as it is: gpg unable to import (`gpg.GpgFailed`), gpg or
openssl producing no ciphertext, or stored material suddenly reading as "not a key" or
"several keys" (`envelope.EnvelopeError`). "Could not look" is never "the key is bad".

Nothing here logs. A refusal carries a code, never key material or an address.
"""

from django.core.mail import EmailMessage, EmailMultiAlternatives

from apps.keys import validation
from apps.keys.models import EncryptionKey

CODES = validation.CODES + (
    "fingerprint_mismatch",  # the stored material is not the key whose fingerprint was shown
    "openssl_missing",  # an EC certificate, and no openssl binary to encrypt with
    "key_failed",  # the contact's key failed earlier: nothing goes in plaintext meanwhile
    "key_missing",  # a letter that must go to one key (a key confirmation) whose key no longer exists
    "key_withdrawn",  # a letter that must go to one key, and that key was revoked or failed since
)
# Refusals that say nothing about the key itself: it stays as it was.
NOT_THE_KEYS_FAULT = frozenset({"openssl_missing", "key_failed", "key_missing", "key_withdrawn"})
# A key in one of these states is never encrypted to again, whatever letter names it.
WITHDRAWN_STATES = frozenset({"revoked", "failed"})
# What material that was accepted once can newly be at send time: time passed, the key was
# revoked, the contact's address changed. Any other rejection of stored material ("not a key",
# "several keys", ...) cannot be the key's doing, so it is retried, never blamed on the key.
SEND_TIME_VERDICTS = frozenset({"expired", "revoked", "email_mismatch", "not_yet_valid", "cannot_encrypt",
                                "unsupported_key"})
# Copied from the inner message to the outer one: what delivery and replies need. The real
# Subject stays inside; outside there is only the generic one.
OUTER_HEADERS = ("From", "To", "Cc", "Reply-To", "Date", "Message-ID")


class EncryptionRefused(Exception):
    """The letter must not be sent: it would have to go unencrypted. `code` is all it says."""

    def __init__(self, code: str):
        if code not in CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        super().__init__(code)
        self.code = code

    @property
    def blames_key(self) -> bool:
        return self.code not in NOT_THE_KEYS_FAULT


class EncryptedEmail(EmailMessage):
    """An e-mail whose MIME structure `protect` built and encrypted. `message()` returns that
    envelope, so whichever backend sends it sends the ciphertext and nothing else."""

    def __init__(self, envelope, plain: EmailMessage, subject: str):
        super().__init__(subject=subject, body="", from_email=plain.from_email, to=plain.to, cc=plain.cc,
                         bcc=plain.bcc, reply_to=plain.reply_to, connection=plain.connection)
        self._envelope = envelope

    def message(self, *args, **kwargs):
        return self._envelope


def key_for_contact(contact_id: int) -> EncryptionKey | None:
    """The key letters to this contact are encrypted to, or None when they go in plaintext.

    Raises EncryptionRefused("key_failed") while the contact has no active key but one that
    failed while active: the next letter must not be the plaintext one."""
    keys = EncryptionKey.objects.filter(contact_id=contact_id)
    active = keys.filter(state="active").first()
    if active is not None:
        return active
    # Fail closed: anything but a key known to have failed while still pending blocks.
    if keys.filter(state="failed").exclude(failed_while="pending").exists():
        raise EncryptionRefused("key_failed")
    return None


def protect(mail: EmailMultiAlternatives, key: EncryptionKey, *, outer_subject: str, now=None) -> EmailMessage:
    """`mail`, every body and attachment and its real Subject, encrypted to `key`.

    The key is validated again first, against the contact's e-mail addresses as they are now.
    The outer message carries only From, To, Cc, Reply-To, Date, Message-ID and `outer_subject`.
    """
    from apps.contacts.models import ContactEmail
    from apps.keys import envelope

    emails = [a for a in ContactEmail.objects.filter(contact_id=key.contact_id).values_list("address", flat=True) if a]
    try:
        info = validation.inspect(key.material, emails, now=now)
    except validation.KeyRejected as exc:
        if exc.code in SEND_TIME_VERDICTS:
            raise EncryptionRefused(exc.code) from None
        raise envelope.EnvelopeError(f"stored key material read as {exc.code}") from None
    if info.kind != key.kind or info.fingerprint != (key.fingerprint or "").replace(" ", "").upper():
        raise EncryptionRefused("fingerprint_mismatch")

    inner = mail.message()
    # Mail clients that understand protected headers show the inner Subject once decrypted.
    inner.set_param("protected-headers", "v1")
    if info.kind == "pgp":
        outer = envelope.pgp_mime(inner, info.material)
    else:
        outer = envelope.smime(inner, info.material)
    for name in OUTER_HEADERS:
        if inner[name] is not None:
            outer[name] = inner[name]
    subject = " ".join(str(outer_subject).split())  # no newline can reach a header
    outer["Subject"] = encoded_subject(subject)
    if outer["MIME-Version"] is None:
        outer["MIME-Version"] = "1.0"
    return EncryptedEmail(outer, mail, subject)


def mark_failed(key: EncryptionKey, code: str, *, outbox_pk: int) -> bool:
    """Record that `key` stopped working at send time. Returns whether it changed state.

    The keys mirrored from it follow in the same transaction, without reading key material."""
    from django.db import transaction

    from apps.audit import services as audit
    from apps.contacts.models import Contact
    from apps.keys import mirroring

    was = key.state
    if was not in ("active", "pending"):
        return False
    with transaction.atomic():
        Contact.objects.select_for_update().filter(pk=key.contact_id).only("pk").first()  # before key rows
        changed = EncryptionKey.objects.filter(pk=key.pk, state=was).update(
            state="failed", last_error_kind=code, failed_while=was)
        if changed:
            audit.record_system("key.failed", key, {"code": code, "was": was, "contact": key.contact_id,
                                                    "outbox": outbox_pk})
            mirroring.key_changed(key.contact_id, create=False)
    if changed:
        _staff_alert(key, code)
    return bool(changed)


def _staff_alert(key: EncryptionKey, code: str) -> None:
    """A `staff.alert` webhook for the contact's organization: the key's id and the refusal code,
    never the fingerprint or the contact."""
    from apps.contacts.models import Contact
    from apps.webhooks import events

    org_id = Contact.objects.filter(pk=key.contact_id).values_list("org_id", flat=True).first()
    if org_id is not None:
        events.staff_alert({"kind": "key_failed", "key_id": key.pk, "reason": code}, org=org_id)


def encoded_subject(value: str) -> str:
    """A Subject header value: as it is when ASCII, else RFC 2047-encoded and folded knowing the
    "Subject: " in front of it (the outbox sets every letter's Subject through this too)."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        from email.header import Header

        # Folded knowing the "Subject: " in front of it, or the first line is left empty and a
        # long name decodes with a leading space.
        return Header(value, "utf-8", header_name="Subject").encode()
    return value
