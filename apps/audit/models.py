from django.conf import settings
from django.db import models

from apps.vault.fields import EncryptedJSONField


class AuditEntry(models.Model):
    """Who did what to which record. Who/what/when are plaintext; the detail is encrypted.

    Actions the system takes on its own (a watcher, the matching engine) may run while the
    vault is sealed, so their detail goes into `system_detail`, which is plaintext and holds
    only flat plain facts: ids, amounts, states, networks, transaction hashes, reason codes.
    """

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_username = models.CharField(max_length=150, blank=True, default="")  # snapshot, survives user deletion
    action = models.CharField(max_length=64)
    model_label = models.CharField(max_length=64)
    object_pk = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    detail = EncryptedJSONField(null=True, blank=True)
    system_detail = models.JSONField(null=True, blank=True)  # plaintext; see record_system

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["model_label", "object_pk"])]

    def __str__(self):
        return f"{self.action} {self.model_label}#{self.object_pk}"
