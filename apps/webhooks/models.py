import uuid

from django.db import models

from apps.vault.fields import EncryptedCharField


class WebhookEndpoint(models.Model):
    """Where an organization's events are POSTed, signed with `secret`.

    The URL and the secret are encrypted, so nothing is delivered while the vault is sealed;
    events recorded meanwhile wait (see `WebhookEvent`). `events` lists the event types the
    endpoint wants; `["*"]` is all of them. An inactive endpoint is given nothing new, and what
    was queued for it is cancelled."""

    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = EncryptedCharField(max_length=2000)
    secret = EncryptedCharField(max_length=128)
    events = models.JSONField(default=list)
    description = models.CharField(max_length=200, blank=True, default="")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return f"webhook endpoint #{self.pk}"

    def wants(self, event_type: str) -> bool:
        return "*" in (self.events or []) or event_type in (self.events or [])


class WebhookEvent(models.Model):
    """Something that happened, recorded in plaintext so it can be recorded while sealed.

    `data` holds ids, statuses and kinds only: never a name, an address, an e-mail or an
    amount. A receiver reads the rest through the API. `org` is None for an alert about the
    service itself (`staff.alert` of a watcher or the outbox), which every organization's
    endpoints subscribed to it receive."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=32)
    org = models.ForeignKey("orgs.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payment = models.ForeignKey("payments.Payment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.type} {self.id}"


class WebhookDelivery(models.Model):
    """One event for one endpoint, and how its delivery went: the delivery log.

    queued: waiting for its turn (`next_attempt_at`, None = now); sent: the endpoint answered
    2xx; failed: every attempt failed, a person decides (Needs a person, "retry" or "dismiss");
    cancelled: the endpoint was deactivated before its turn, or a person dismissed the failure. `last_error_kind` is a code ("http_503",
    "redirect", "timeout", "refused:private_address"), never a URL or a message."""

    STATES = [("queued", "queued"), ("sent", "sent"), ("failed", "failed"), ("cancelled", "cancelled")]

    event = models.ForeignKey(WebhookEvent, on_delete=models.CASCADE, related_name="deliveries")
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    state = models.CharField(max_length=10, choices=STATES, default="queued")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error_kind = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    # Set once staff have been told this delivery failed (cleared by "retry").
    staff_alerted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["state", "created_at"])]

    def __str__(self):
        return f"webhook delivery #{self.pk} ({self.state})"
