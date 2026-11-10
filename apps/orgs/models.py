import datetime
import string

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.vault.fields import EncryptedCharField, EncryptedJSONField, EncryptedTextField


def validate_number_format(fmt: str) -> None:
    """Only plain {year} and {seq} fields (with optional format spec); seq is required."""
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(fmt) if f is not None]
    except ValueError as exc:
        raise ValidationError({"number_format": f"Invalid number format: {exc}"})
    if any(f not in ("year", "seq") for f in fields):
        raise ValidationError({"number_format": "Only plain {year} and {seq} fields are allowed."})
    if "seq" not in fields:
        raise ValidationError({"number_format": "The format must contain {seq}."})
    try:
        a = fmt.format(year=2000, seq=1)
        b = fmt.format(year=2000, seq=2)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ValidationError({"number_format": f"Invalid number format: {exc}"})
    if a == b:
        raise ValidationError({"number_format": "The format must give different numbers for different sequences."})


def default_org_settings() -> dict:
    return {"underpay_allowance_bps": 50, "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": 1000,
            "accounting_currency": "EUR", "return_url_hosts": []}


class Organization(models.Model):
    slug = models.SlugField(unique=True)
    legal_name = EncryptedCharField(max_length=255, null=True, blank=True)
    address = EncryptedTextField(null=True, blank=True)
    tax_ids = EncryptedTextField(null=True, blank=True)
    email_from = EncryptedCharField(max_length=255, null=True, blank=True)
    footer_note = EncryptedTextField(null=True, blank=True)
    logo = models.ImageField(upload_to="logos/", blank=True)
    default_language = models.CharField(max_length=5, default="en")
    number_format = models.CharField(max_length=100, default="{year}-{seq:04d}")
    next_number = models.PositiveIntegerField(default=1)
    is_us_resident_issuer = models.BooleanField(default=False)
    settings = models.JSONField(default=default_org_settings, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["is_us_resident_issuer"],
                condition=Q(is_us_resident_issuer=True),
                name="one_us_resident_issuer",
            )
        ]

    def __str__(self):
        return self.slug

    def format_number(self, when: datetime.date, seq: int) -> str:
        return self.number_format.format(year=when.year, seq=seq)

    def clean(self):
        super().clean()
        validate_number_format(self.number_format)

    def save(self, *args, **kwargs):
        validate_number_format(self.number_format)
        super().save(*args, **kwargs)


class BankAccount(models.Model):
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_accounts")
    beneficiary_name = EncryptedCharField(max_length=255, null=True, blank=True)
    beneficiary_address = EncryptedTextField(null=True, blank=True)
    account_number = EncryptedCharField(max_length=64, null=True, blank=True)
    bank_name = EncryptedCharField(max_length=255, null=True, blank=True)
    bank_address = EncryptedTextField(null=True, blank=True)
    bank_swift = EncryptedCharField(max_length=32, null=True, blank=True)
    intermediary_bank_name = EncryptedCharField(max_length=255, null=True, blank=True)
    intermediary_swift = EncryptedCharField(max_length=32, null=True, blank=True)
    intermediary_account = EncryptedCharField(max_length=64, null=True, blank=True)
    instructions = EncryptedJSONField(null=True, blank=True)
    currency = models.CharField(max_length=8)
    label = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.org} {self.currency} {self.label}".strip()


class FiatLink(models.Model):
    KINDS = [(k, k) for k in ("revolut", "n26", "paypal", "wise", "other")]

    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="fiat_links")
    kind = models.CharField(max_length=16, choices=KINDS)
    label = models.CharField(max_length=100, blank=True)
    currencies = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    url = EncryptedCharField(max_length=2000, null=True, blank=True)
    instructions = EncryptedJSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.org} {self.kind} {self.label}".strip()
