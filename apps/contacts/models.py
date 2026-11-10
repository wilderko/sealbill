from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.orgs.models import Organization
from apps.vault.fields import (
    BlindIndexField,
    EncryptedCharField,
    EncryptedJSONField,
    EncryptedTextField,
    blind_index,
)


class Contact(models.Model):
    US_TAX_CHOICES = [("yes", "yes"), ("no", "no"), ("unknown", "unknown")]

    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="contacts")
    legal_name = EncryptedCharField(max_length=255, null=True, blank=True)
    display_name = EncryptedCharField(max_length=255, null=True, blank=True)
    address = EncryptedJSONField(null=True, blank=True)  # {line1,line2,city,zip,state,country}
    tax_ids = EncryptedTextField(null=True, blank=True)
    phone = EncryptedCharField(max_length=64, null=True, blank=True)
    notes = EncryptedTextField(null=True, blank=True)
    preferred_language = models.CharField(max_length=5, blank=True)
    us_tax_resident = models.CharField(max_length=7, choices=US_TAX_CHOICES, default="unknown")
    # The requesting organization this contact is the US-resident issuer's copy for
    # (`invoices.services._contact_in`): one copy per address and requesting organization, so
    # one organization's token never reads or rewrites what another, or the issuer itself, holds.
    # NULL for an organization's own contact. Contacts older than the column are all NULL: a copy
    # made then cannot be told from the issuer's own client.
    copy_for_org = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", editable=False)
    # A client of record: what e-mail and name lookups find, the API lists and new invoices reuse. A
    # payment-link checkout that matches no client of record makes a contact of its own from what the
    # visitor typed, a link buyer (False, audit SEC-1): anyone can type anything into a public form, and pay
    # any price under it. Never reused by another checkout, never found by lookups, outside the unique
    # e-mail constraints below; paying does not change it, a person does (`promote_link_buyer`, review F2).
    # The database default too: a row written by anything that does not know the column (code older than the
    # migration, while a deploy runs) is a client of record, as every contact was before.
    of_record = models.BooleanField(default=True, db_default=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Contact #{self.pk}"

    @property
    def is_us_tax_resident(self) -> bool:
        return self.us_tax_resident == "yes"

    @property
    def language(self) -> str:
        codes = {code for code, _ in settings.LANGUAGES}
        if self.preferred_language in codes:
            return self.preferred_language
        if self.org.default_language in codes:
            return self.org.default_language
        return "en"


class ContactEmail(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="emails")
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+", editable=False)
    # The contact's own `copy_for_org`, kept here so the constraints below can name it.
    copy_for_org = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE,
                                     related_name="+", editable=False)
    address = EncryptedCharField(max_length=320, null=True, blank=True)
    address_index = BlindIndexField(editable=False)
    primary = models.BooleanField(default=False)
    # The contact's own `of_record`, kept here so the constraints below can name it.
    of_record = models.BooleanField(default=True, db_default=True, editable=False)

    class Meta:
        # Among clients of record, an address belongs to one native contact per organization, and to one
        # copy per organization and requester (two constraints: NULLs are never equal in a unique index).
        # A checkout's own contact (not of record) holds its address beside them, never in their place.
        constraints = [
            models.UniqueConstraint(fields=["org", "address_index"],
                                    condition=Q(copy_for_org__isnull=True, of_record=True),
                                    name="unique_native_email_per_org"),
            models.UniqueConstraint(fields=["org", "address_index", "copy_for_org"],
                                    condition=Q(copy_for_org__isnull=False, of_record=True),
                                    name="unique_copy_email_per_requester"),
        ]

    def save(self, *args, **kwargs):
        if not self.address or not self.address.strip():
            raise ValueError("e-mail address is required")
        self.org_id = self.contact.org_id
        self.copy_for_org_id = self.contact.copy_for_org_id
        self.of_record = self.contact.of_record
        self.address_index = blind_index(self.address)
        super().save(*args, **kwargs)
