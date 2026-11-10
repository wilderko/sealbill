from django.db import models


class WorkerHeartbeat(models.Model):
    """One row (`pk=1`) the worker loop touches on every pass. Plaintext only, so the heartbeat
    is written and read while the vault is sealed. `jobs` maps a job name to the ISO time it
    last ran."""

    beat_at = models.DateTimeField()
    jobs = models.JSONField(default=dict)

    def __str__(self):
        return f"worker heartbeat {self.beat_at:%Y-%m-%d %H:%M:%S}"
