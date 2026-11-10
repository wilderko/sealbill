import datetime

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.vault.fields import EncryptedCharField, EncryptedTextField

# How long the link in a key-confirmation letter works, counted from `confirm_sent_at`.
CONFIRM_VALID_FOR = datetime.timedelta(days=7)
# How long the cancel link in a replacement notice works, counted from `confirm_sent_at` too: the
# notice is queued with the confirmation letter.
CANCEL_VALID_FOR = datetime.timedelta(days=7)


class EncryptionKey(models.Model):
    """A PGP public key or an S/MIME certificate that a contact's e-mail is encrypted to.

    Only `validation.inspect` decides what may be stored here: `material` is what it
    normalized (armored PGP or a PEM certificate) and `fingerprint` is what it computed.
    Both are encrypted; a fingerprint names the person as surely as their address does.

    A key a client uploaded waits in `pending` until a link in a letter encrypted to it is
    answered. At most one key per contact is `active`, and only that one is ever used.

    When the contact had an active key at the upload, that key (`replaces`) is sent a notice with a
    cancel link: whoever reads the client's mailbox and holds a forwarded invoice link could
    otherwise swap their own key in unseen (audit DATA-6).
    """

    KINDS = [("pgp", "PGP"), ("smime", "S/MIME")]
    # replaced: another key became active; revoked: staff or the client withdrew it;
    # failed: it stopped working at send time (expired, revoked upstream), see last_error_kind.
    STATES = [("pending", "pending"), ("active", "active"), ("replaced", "replaced"), ("revoked", "revoked"),
              ("failed", "failed")]
    # mirror: a copy, on the US-resident issuer's copy of a contact, of the key `mirrors` names
    # (see apps.keys.mirroring). Never a key anyone added there.
    SOURCES = [("staff", "staff"), ("api", "api"), ("client", "client"), ("mirror", "mirror")]

    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="encryption_keys")
    kind = models.CharField(max_length=5, choices=KINDS)
    material = EncryptedTextField()
    fingerprint = EncryptedCharField(max_length=128)
    expires_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=8, choices=STATES, default="pending")
    source = models.CharField(max_length=6, choices=SOURCES)
    # sha256 hex of the confirmation token; the token itself is never stored.
    confirm_token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    confirm_sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # A client upload made while the contact had an active key: that key, which was sent the
    # replacement notice; the sha256 hex of the notice's cancel token (the token itself is never
    # stored); and when the cancel link was used (it works once).
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # A KeyRejected / EncryptionRefused code, never a message: codes carry no key material.
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    # The state a `failed` key was in when it failed. One that failed while active keeps the
    # contact's mail from going out in plaintext until a person acts; one that failed while
    # still pending (never confirmed) does not, or anyone could stop a contact's mail by
    # uploading a key that is about to expire.
    failed_while = models.CharField(max_length=8, blank=True, default="",
                                    choices=[("", "-"), ("active", "active"), ("pending", "pending")])
    # For a `source="mirror"` row: the original's key it follows. Every state change of that key
    # reaches the mirror in the same transaction (apps.keys.mirroring); NULL once the key is gone,
    # and such an orphan is revoked, never taken for a key staff added to the copy.
    mirrors = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["contact"], condition=Q(state="active"),
                                    name="one_active_key_per_contact"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} key #{self.pk} ({self.state})"
