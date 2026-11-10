from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.contacts.models import Contact
from apps.invoices.errors import InvoiceError
from apps.orgs.models import Organization
from apps.vault.fields import EncryptedCharField, EncryptedJSONField, EncryptedTextField

# Columns a sent invoice may still change (set by the lifecycle services).
LIFECYCLE_FIELDS = {
    "status", "sent_at", "voided_at", "paid_at", "view_token_hash", "pay_token_hash",
    "void_reason", "voided_by", "view_token_copy", "pay_token_copy", "reminder_sent_at",
}


class InvoiceQuerySet(models.QuerySet):
    def delete(self):
        if self.exclude(status="draft").exists():
            raise InvoiceError("only draft invoices can be deleted")
        return super().delete()


class Invoice(models.Model):
    KINDS = [("invoice", "invoice"), ("payment_request", "payment request")]
    STATUSES = [
        (s, s)
        for s in ("draft", "sent", "overdue", "declared", "partially_paid", "paid", "overpaid", "void")
    ]

    org = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="invoices")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="invoices")
    requested_by_org = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=16, choices=KINDS, default="invoice")
    number = models.CharField(max_length=64, null=True, blank=True)
    currency = models.CharField(max_length=8)
    issue_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="draft")
    external_reference = models.CharField(max_length=200, null=True, blank=True)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)
    title = EncryptedTextField(null=True, blank=True)
    notes = EncryptedTextField(null=True, blank=True)
    metadata = EncryptedJSONField(null=True, blank=True)
    offered_methods = models.JSONField(default=list, blank=True)
    # The (network, asset) pairs the offered crypto rails may be quoted in, as [network, asset] lists;
    # empty: every pair those rails enable. A payment link's custom method set narrows it.
    offered_pairs = models.JSONField(default=list, blank=True)
    # The payment link and the coupon a checkout made this order from (`apps.links`); NULL otherwise.
    payment_link = models.ForeignKey("links.PaymentLink", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="invoices")
    coupon = models.ForeignKey("links.Coupon", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="invoices")
    # SHA-256 of the capability tokens; NULL until the invoice is sent. Unique (and so indexed):
    # a hash names exactly one invoice.
    view_token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    pay_token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # A payment-link order only (spec 2.3a item 2): the tokens themselves, encrypted, beside the hashes,
    # so its receipt and partial-payment letter can carry the links (nobody was e-mailed them before
    # payment). Forgotten once the order is paid and its receipt is out, or voided. NULL otherwise.
    view_token_copy = EncryptedTextField(null=True, blank=True, editable=False)
    pay_token_copy = EncryptedTextField(null=True, blank=True, editable=False)
    issued_by_rule = models.BooleanField(default=False)
    # False: the caller (a shop) writes to its own customers, so this system sends the client no letter
    # for the invoice: not the invoice, the receipt, nor the partial-payment letter. Set once at send and
    # guarded like every other column afterwards. Staff alerts and webhooks are not affected.
    client_letters = models.BooleanField(default=True)
    # Where the pay page's Back button leads (an https address on a host the requesting organization
    # allows; checked by apps.invoices.return_url). Set at creation; a sent invoice cannot change it.
    return_url = models.URLField(max_length=500, blank=True)
    # "v2:" + HMAC-SHA256 (vault-derived key) of the API request that created the invoice, so a
    # reused Idempotency-Key with another body is caught; "" when there was no such request, and
    # for the unkeyed hashes stored before v2 (blanked: they named the client to anyone with the
    # database).
    request_fingerprint = models.CharField(max_length=80, blank=True, default="")
    # The seller, buyer and payment details as they were when the invoice was sent
    # (`apps.invoices.snapshot`); NULL for a draft. Guarded like every other column once sent.
    snapshot = EncryptedJSONField(null=True, blank=True, editable=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # A person decided to keep money paid over the total (no refund). The confirmed sum at that moment
    # is recorded, so more money arriving later brings the invoice back to a person.
    overpayment_kept_minor = models.BigIntegerField(null=True, blank=True)
    overpayment_kept_at = models.DateTimeField(null=True, blank=True)
    overpayment_kept_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # When the one payment reminder was queued (apps.invoices.reminders); NULL until then.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    void_reason = EncryptedTextField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InvoiceQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "number"], name="unique_invoice_number_per_org"),
            models.UniqueConstraint(
                fields=["requested_by_org", "external_reference"],
                name="unique_external_reference_per_requester",
            ),
            models.UniqueConstraint(
                fields=["requested_by_org", "idempotency_key"],
                name="unique_idempotency_key_per_requester",
            ),
        ]

    def __str__(self):
        return self.number or f"{self.kind} #{self.pk}"

    def delete(self, *args, **kwargs):
        stored = Invoice.objects.filter(pk=self.pk).values_list("status", flat=True).first()
        if stored is not None and stored != "draft":
            raise InvoiceError("only draft invoices can be deleted")
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        # Read through values_list, never by loading rows: a lifecycle save (status, paid_at)
        # runs while the vault is sealed, and loading the contact or the stored invoice would
        # decrypt their encrypted columns.
        if self.contact_id and self.org_id:
            contact_org = Contact.objects.filter(pk=self.contact_id).values_list("org_id", flat=True).first()
            if contact_org != self.org_id:
                raise InvoiceError("the contact belongs to another organization")
        if self.pk and not self._state.adding:
            stored_status = Invoice.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored_status is not None and stored_status != "draft":
                if self.status == "draft":
                    raise InvoiceError("sent invoices cannot be edited")
                update_fields = kwargs.get("update_fields")
                names = set(update_fields) if update_fields is not None else None
                # Only the columns being written that a sent invoice may not change are compared,
                # so a lifecycle-only save loads (and decrypts) nothing.
                guarded = [
                    f for f in self._meta.concrete_fields
                    if f.name not in LIFECYCLE_FIELDS and f.name != "id"
                    and (names is None or f.name in names or f.attname in names)
                ]
                if guarded:
                    stored = Invoice.objects.filter(pk=self.pk).values_list(*(f.attname for f in guarded)).get()
                    if any(getattr(self, f.attname) != value for f, value in zip(guarded, stored)):
                        raise InvoiceError("sent invoices cannot be edited")
        super().save(*args, **kwargs)

    def view_url(self, plain: str) -> str:
        return f"{settings.SITE_URL.rstrip('/')}/i/{plain}/"

    def pay_url(self, plain: str) -> str:
        return f"{settings.SITE_URL.rstrip('/')}/p/{plain}/"


class LineItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    name = EncryptedCharField(max_length=500, null=True, blank=True)
    description = EncryptedTextField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1"))
    unit_price_minor = models.BigIntegerField(default=0)
    tax_rate_bps = models.PositiveIntegerField(default=0)
    discount_rate_bps = models.PositiveIntegerField(default=0)
    discount_amount_minor = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]

    def _guard(self):
        status = Invoice.objects.filter(pk=self.invoice_id).values_list("status", flat=True).first()
        if status != "draft":
            raise InvoiceError("sent invoices cannot be edited")

    def save(self, *args, **kwargs):
        self._guard()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard()
        return super().delete(*args, **kwargs)
