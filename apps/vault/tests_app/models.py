from django.db import models

from apps.vault.fields import BlindIndexField, EncryptedCharField, EncryptedJSONField, EncryptedTextField


class Sample(models.Model):
    """Test-only model; the app is installed by config/settings/test.py alone."""

    name = EncryptedCharField(max_length=20, null=True, blank=True)
    note = EncryptedTextField(null=True, blank=True)
    data = EncryptedJSONField(null=True, blank=True)
    email_idx = BlindIndexField(blank=True, default="")
    plain = models.CharField(max_length=20, blank=True, default="")
