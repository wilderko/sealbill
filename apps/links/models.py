"""Payment links: one shareable page, `/l/<token>/`, paid by many people (spec 2.2).

Each checkout makes an order: an invoice (or a payment request) with one line, sent at once without an
e-mail, and the buyer goes on to its existing pay page. The invoice records the link and the coupon it
came from (`Invoice.payment_link`, `Invoice.coupon`); what the buyer typed that the invoice does not hold
(the address they typed, their language, shipping, phone, custom answers) and the bookkeeping of the
order (its hold on the link's and the coupon's uses, fulfilment) live in `LinkOrder`.

Plaintext columns are what the payment path reads while the vault is sealed: ids, amounts, states,
times, codes. Everything a person wrote or typed is encrypted.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.orgs.models import Organization
from apps.vault.fields import EncryptedCharField, EncryptedJSONField, EncryptedTextField

FIELD_MODES = [("off", "Off"), ("optional", "Optional"), ("required", "Required")]
MAX_CUSTOM_FIELDS = 5
# 0 means "unlimited" (Zaprite's word), capped at the invoice line cap (`limits.MAX_LINES`): a
# quantity is one line, and the cap keeps one order's total within what an invoice may hold.
UNLIMITED_QUANTITY = 200
MAX_TITLE = 200
MAX_DESCRIPTION = 5000
MAX_NOTE = 5000
MAX_CUSTOM_LABEL = 100
MAX_CUSTOM_ANSWER = 500
HOLD_MINUTES = 60  # a checkout holds one of the link's (and the coupon's) uses this long unpaid
VOID_AFTER_DAYS = 7  # an unpaid order is voided after this


class PaymentLink(models.Model):
    KINDS = [("invoice", "Invoice"), ("payment_request", "Payment request")]
    METHODS = [("default", "The issuer's default methods"), ("custom", "A custom set")]

    org = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="payment_links")
    # The capability: the SHA-256 for lookup, and the token itself encrypted so the staff page can
    # show the URL again (2.3a item 4: a link is meant to be shared; its secrecy is only against guessing).
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    token = EncryptedTextField(null=True, blank=True, editable=False)
    title = EncryptedCharField(max_length=MAX_TITLE, null=True, blank=True)
    description = EncryptedTextField(null=True, blank=True)
    internal_note = EncryptedTextField(null=True, blank=True)
    currency = models.CharField(max_length=8)
    # A fixed price, or the customer chooses the amount within optional bounds (all in minor units).
    customer_chooses = models.BooleanField(default=False)
    price_minor = models.BigIntegerField(null=True, blank=True)
    min_minor = models.BigIntegerField(null=True, blank=True)
    max_minor = models.BigIntegerField(null=True, blank=True)
    tax_rate_bps = models.PositiveIntegerField(default=0)
    max_quantity = models.PositiveIntegerField(default=1)  # 0: unlimited (UNLIMITED_QUANTITY)
    name_field = models.CharField(max_length=8, choices=FIELD_MODES, default="required")
    billing_field = models.CharField(max_length=8, choices=FIELD_MODES, default="off")
    shipping_field = models.CharField(max_length=8, choices=FIELD_MODES, default="off")
    phone_field = models.CharField(max_length=8, choices=FIELD_MODES, default="off")
    # Up to five extra questions: [{"label": str, "required": bool}]. Staff-written, client-facing.
    custom_fields = EncryptedJSONField(null=True, blank=True)
    kind = models.CharField(max_length=16, choices=KINDS, default="invoice")
    methods = models.CharField(max_length=8, choices=METHODS, default="default")
    # A custom set is stored as method TYPES, never account ids (2.3a item 5), so it applies to whichever
    # organization issues the order: see `apps.links.methods`.
    method_types = models.JSONField(default=list, blank=True)
    fulfillment = models.BooleanField(default=False)
    notify_staff = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Payment link #{self.pk}"

    @property
    def quantity_cap(self) -> int:
        return UNLIMITED_QUANTITY if self.max_quantity == 0 else self.max_quantity

    @staticmethod
    def url_for(plain: str) -> str:
        return f"{settings.SITE_URL.rstrip('/')}/l/{plain}/"


class LinkImage(models.Model):
    """The link's picture, re-encoded by Pillow (metadata stripped) and kept encrypted in the database.
    It is only ever served inside the link page (a data: URI), never from a URL of its own."""

    link = models.OneToOneField(PaymentLink, on_delete=models.CASCADE, related_name="image")
    content_type = models.CharField(max_length=16)
    data = EncryptedTextField(null=True, blank=True)  # base64 of the re-encoded file
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class Coupon(models.Model):
    """A discount code of one link: a percentage off the line, before tax, one per order."""

    link = models.ForeignKey(PaymentLink, on_delete=models.CASCADE, related_name="coupons")
    code = models.CharField(max_length=40)  # upper case; typed by buyers, so it is no secret
    name = EncryptedCharField(max_length=200, null=True, blank=True)
    internal_note = EncryptedTextField(null=True, blank=True)
    percent_bps = models.PositiveIntegerField()  # 1..9999: a coupon never makes an order free
    max_uses = models.PositiveIntegerField(null=True, blank=True)  # Zaprite's "Remaining usages"
    valid_until = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["link", "code"], name="unique_coupon_code_per_link")]

    def __str__(self):
        return self.code


class LinkOrder(models.Model):
    """What a checkout adds to its invoice.

    `hold`: the order's claim on one of the link's and the coupon's uses (2.3a item 1). `reserved`
    while unpaid, for HOLD_MINUTES; `used` once paid (whenever that is); `lapsed` when the hold ran out
    or the invoice was voided first. A paid order whose hold had lapsed keeps its price; if that takes
    a capped link or coupon past its cap, staff get one item (`LinkAlert`, "over_cap")."""

    HOLDS = [("reserved", "reserved"), ("used", "used"), ("lapsed", "lapsed")]

    invoice = models.OneToOneField("invoices.Invoice", on_delete=models.PROTECT, related_name="link_order")
    # The contact the order was booked to in the link's own organization: a client of record the typed
    # e-mail matched, or the link buyer this checkout made (audit SEC-1). The invoice names the US-resident
    # issuer's copy instead when the order went there.
    buyer_contact = models.ForeignKey("contacts.Contact", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", editable=False)
    email = EncryptedCharField(max_length=320, null=True, blank=True)  # as typed: the letters go here
    language = models.CharField(max_length=8)  # of the checkout page: the letters are written in it
    quantity = models.PositiveIntegerField(default=1)
    # Shipping address, phone and the custom answers: {"shipping": {...}, "phone": str,
    # "custom": [{"label": str, "value": str}]}. Name and billing address are in the invoice's snapshot.
    answers = EncryptedJSONField(null=True, blank=True)
    us_answer = models.CharField(max_length=3)  # "yes" or "no", as typed
    hold = models.CharField(max_length=8, choices=HOLDS, default="reserved")
    reserved_until = models.DateTimeField()
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    fulfilled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["hold", "reserved_until"], name="link_order_hold")]

    def __str__(self):
        return f"Order #{self.pk}"


class LinkAlert(models.Model):
    """A payment-link matter for Needs a person. Ids only: the page says what it means."""

    KINDS = [
        ("us_mismatch", "The buyer's US tax resident answer differs from the contact's"),
        ("over_cap", "A paid order took a capped link or coupon past its cap"),
        ("no_methods", "The issuer offers none of the link's payment methods"),
        ("no_us_issuer", "A US tax resident checked out and no organization is the US-resident issuer"),
    ]

    kind = models.CharField(max_length=16, choices=KINDS)
    link = models.ForeignKey(PaymentLink, on_delete=models.CASCADE, related_name="alerts")
    coupon = models.ForeignKey(Coupon, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="+")
    issuer = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    class Meta:
        constraints = [
            # One open "nothing to offer" item per link and issuer, however many buyers try. Two
            # constraints: NULLs are never equal in a unique index, so the issuer-less kind needs its own.
            models.UniqueConstraint(fields=["kind", "link", "issuer"], condition=Q(resolved_at__isnull=True,
                                    kind__in=("no_methods", "no_us_issuer")), name="one_open_link_problem"),
            models.UniqueConstraint(fields=["kind", "link"], condition=Q(resolved_at__isnull=True, issuer__isnull=True,
                                    kind__in=("no_methods", "no_us_issuer")), name="one_open_issuerless_link_problem"),
        ]
