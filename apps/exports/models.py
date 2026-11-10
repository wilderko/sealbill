from django.db import models


class DailyRate(models.Model):
    """One day's price of 1 `base` in `quote` (major units), cached for the books.

    Plaintext only: the worker fills it and the export reads it, neither needs the vault.
    `rate` is Decimal text, never a float; empty means the source has no such rate (do not ask again).
    """

    day = models.DateField()
    base = models.CharField(max_length=8)
    quote = models.CharField(max_length=8)
    rate = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=32)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["day", "base", "quote"], name="unique_daily_rate")]

    def __str__(self):
        return f"{self.day} {self.base}/{self.quote}"
