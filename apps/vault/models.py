from django.db import models


class RateCounter(models.Model):
    """One rate-limit window: how many attempts a key made since the window opened, and when it
    closes (Unix seconds). Counted by a conditional UPDATE in the database, never by reading the
    count and writing it back, so parallel attempts cannot all read the same number (red-team
    report 2026-11-10, A1). `cache_purge` deletes the windows that have closed."""

    key = models.CharField(max_length=255, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    window_end = models.FloatField(db_index=True)

    class Meta:
        db_table = "pay_rate_counter"

    def __str__(self):
        return f"{self.key}: {self.count}"
