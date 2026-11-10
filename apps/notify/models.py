from django.db import models

from apps.vault.fields import EncryptedJSONField

# Who a letter goes to and what it says. Kept (encrypted) only while it may still be sent: once it is
# sent, dismissed or cancelled they are cleared, kind, state and timestamps staying for the record. A
# letter carries the client's working /i/ and /p/ links, which are otherwise stored only as hashes
# (audit DATA-4).
CONTENT_FIELDS = ("to", "cc", "subject_context", "body_context")


def cleared_contents() -> dict:
    """The `update()` arguments that forget a finished letter's recipients and contents."""
    return {field: None for field in CONTENT_FIELDS}


class OutboxMessage(models.Model):
    """An e-mail waiting to be sent. Recipients and contents are encrypted; nothing is
    sent while the vault is sealed, so a restart never loses a message.

    A message queued while the vault may be sealed (a payment a watcher found) is addressed
    later: it names a `recipient` ("contact" or "staff") and carries only `plain_context`
    (amounts, currency, ids, codes). Its recipients, language and contents are worked out
    when it is sent, and its encrypted columns stay NULL.
    """

    # key_confirm: the letter that confirms a key a client uploaded. It names that key (`key`) and is
    # encrypted to it, so only the key's holder can read the link inside.
    # staff_change_staff: a step-up change on the staff pages (audit SEC-3), to every active superuser.
    # key_replacement: the notice that a client uploaded a new key while the contact had an active one.
    # It names that active key and is encrypted to it, so only the holder of the key about to be
    # replaced reads it, and the cancel link inside (audit DATA-6).
    KINDS = ["invoice", "reminder", "receipt", "partial", "key_confirm", "key_replacement", "declared_staff",
             "vault_sealed_staff", "review_staff", "confirm_staff", "order_paid_staff", "staff_change_staff"]
    # cancelled: no longer true when its turn came (the invoice was voided, the receipt's
    # invoice is no longer paid, its links were reissued), so it was never sent; or it failed and
    # a person dismissed it (audited). A cancelled message is never sent, and no retry revives it.
    # A sent or cancelled message keeps no recipients and no contents (CONTENT_FIELDS).
    STATES = [("queued", "queued"), ("sent", "sent"), ("failed", "failed"), ("cancelled", "cancelled")]
    # "": the recipients are stored in `to`/`cc`; otherwise they are worked out at flush.
    RECIPIENTS = [("", "as stored"), ("contact", "contact"), ("staff", "staff")]

    kind = models.CharField(max_length=32)
    state = models.CharField(max_length=10, choices=STATES, default="queued")
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # Each channel of the staff alert keeps its own mark (both cleared by "retry now"): set once the
    # e-mail went out, and once a staff.alert event was recorded for an endpoint. A channel that has
    # nobody to tell (no STAFF_NOTIFY_EMAILS, no endpoint wanting staff alerts) is marked once the
    # other one told someone; with nobody at all, neither is marked.
    staff_alerted_at = models.DateTimeField(null=True, blank=True)
    webhook_alerted_at = models.DateTimeField(null=True, blank=True)
    # When the message reached `failed` (cleared by "retry now"): identifies one failure,
    # so an alert about an earlier failure cannot cover a later one.
    failed_at = models.DateTimeField(null=True, blank=True)
    # Why the last attempt failed: the exception's class, and a refusal's code, e.g.
    # "EncryptionRefused(expired)". Never its message, which may carry client data. Cleared once sent.
    last_error = models.CharField(max_length=64, blank=True, default="")
    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    language = models.CharField(max_length=5, default="en")
    to = EncryptedJSONField(null=True, blank=True)
    cc = EncryptedJSONField(null=True, blank=True)
    subject_context = EncryptedJSONField(null=True, blank=True)
    body_context = EncryptedJSONField(null=True, blank=True)
    recipient = models.CharField(max_length=8, choices=RECIPIENTS, blank=True, default="")
    contact = models.ForeignKey("contacts.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Plaintext, and only amounts, currency, ids and codes: never a name or an address.
    plain_context = models.JSONField(null=True, blank=True)
    # Encrypt to this key instead of the contact's active one: the key-confirmation letter,
    # which goes to a key that is still pending, and the replacement notice, which goes to the key
    # that was active when the new one was uploaded. Unset, a letter to a contact is encrypted to
    # their active key, if they have one. Letters to staff are never encrypted.
    key = models.ForeignKey("keys.EncryptionKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["state", "created_at"])]

    def __str__(self):
        return f"{self.kind} #{self.pk} ({self.state})"
