import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.vault.fields import EncryptedTextField


class Declaration(models.Model):
    """A client's "I have paid". A claim for staff to review, never evidence of money."""

    STATES = [("open", "open"), ("matched", "matched"), ("dismissed", "dismissed")]

    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="declarations")
    created_at = models.DateTimeField(auto_now_add=True)
    network = models.CharField(max_length=32, blank=True)
    tx_hash = models.CharField(max_length=128, blank=True)
    note = EncryptedTextField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=STATES, default="open")
    # The named transaction is looked up on chain (apps.payments.matching): how often that could
    # not be done, and when it was found among the transfers to our addresses.
    verify_attempts = models.PositiveIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    # When staff were e-mailed about it, or found that they need not be: only a claim they must act
    # on is mailed (`services.tell_staff_if_needed`, the owner's rule of 2026-11-11).
    staff_told_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Declaration #{self.pk} for invoice #{self.invoice_id or "-"}"


# Why a payment sits in the review queue. Sentences are for staff; the code is what is stored.
REVIEW_REASONS = {
    "token_not_allowed": _("The token is not one this invoice accepts."),
    "no_matching_invoice": _("No open invoice matches this payment."),
    "ambiguous": _("More than one open invoice could match this payment."),
    "after_expiry": _("The payment arrived after the quote expired."),
    "duplicate": _("This looks like a duplicate of another payment."),
    "overpaid_beyond_ceiling": _("The payment is far above what was owed."),
    "invoice_closed": _("The invoice was already closed when the payment arrived."),
    "before_quote": _("The payment was made before the quote was issued."),
    "dust": _("A very small transfer, probably address-poisoning spam."),
    "dropped": _("The transaction was dropped from the chain."),
    "btcpay_partial": _("BTCPay reports a partial payment."),
    "btcpay_marked": _("BTCPay invoice was marked as paid by hand."),
    "btcpay_invalid": _("BTCPay reports the invoice as invalid."),
    "btcpay_late": _("BTCPay reports a late payment."),
}

_DIGITS = re.compile(r"^[0-9]{1,78}$")
DIGIT_FIELDS = ("amount_base_units", "refund_due_base_units")


class Payment(models.Model):
    """Money that reached an invoice. Only `confirmed` payments count toward the balance."""

    RAILS = [("bank", "bank"), ("fiat_link", "fiat link"), ("manual", "manual"), ("btcpay", "btcpay"),
             ("lightning", "lightning"), ("evm", "evm"), ("tron", "tron"), ("solana", "solana"), ("ton", "ton")]
    STATES = [("detected", "detected"), ("confirmed", "confirmed"), ("rejected", "rejected"),
              ("needs_review", "needs review"), ("reversed", "reversed")]

    invoice = models.ForeignKey("invoices.Invoice", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="payments")
    rail = models.CharField(max_length=16, choices=RAILS)
    network = models.CharField(max_length=32, blank=True, default="")
    asset = models.CharField(max_length=16, blank=True, default="")
    amount_minor = models.BigIntegerField()  # in the invoice currency
    received_amount = models.DecimalField(max_digits=38, decimal_places=18)  # in `asset`
    tx_hash = models.CharField(max_length=128, blank=True, default="")
    log_index = models.IntegerField(default=0)
    external_id = models.CharField(max_length=128, blank=True, default="")
    state = models.CharField(max_length=16, choices=STATES, default="detected")
    detected_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Set once apply_payments has counted this payment (so e-mails go out once per change).
    applied = models.BooleanField(default=False)
    evidence_note = EncryptedTextField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=200, blank=True, default="")
    rail_account = models.ForeignKey("rails.RailAccount", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="payments")
    # The quote a crypto payment was attributed to (its amount, window and rate).
    quote = models.ForeignKey("quotes.Quote", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="payments")
    # Exact on-chain amounts are digit strings: SQLite would round a big integer column.
    amount_base_units = models.CharField(max_length=78, blank=True, default="")
    contract = models.CharField(max_length=128, blank=True, default="")
    to_address = models.CharField(max_length=128, blank=True, default="")
    from_address = models.CharField(max_length=128, blank=True, default="")
    block_number = models.BigIntegerField(null=True, blank=True)
    block_time = models.DateTimeField(null=True, blank=True)
    final = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=32, blank=True, default="")
    refund_due_base_units = models.CharField(max_length=78, blank=True, default="")
    refund_acknowledged_at = models.DateTimeField(null=True, blank=True)
    refund_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    needs_staff_confirmation = models.BooleanField(default=False)
    # The gateway called this payment invalid after it was confirmed (BTCPay `Invalid` after
    # `Settled`: a double spend, a reorg, a payment that never confirmed). Nothing was undone: a
    # person reverses it, or keeps it (`gateway_invalid_acknowledged_*`). Set once, so staff are
    # told once (audit MONEY-4).
    gateway_invalid_at = models.DateTimeField(null=True, blank=True)
    gateway_invalid_acknowledged_at = models.DateTimeField(null=True, blank=True)
    gateway_invalid_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    check_attempts = models.IntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        super().clean()
        for name in DIGIT_FIELDS:
            value = self.__dict__.get(name, "")  # a deferred field is not loaded here, nor checked
            if value != "" and not (isinstance(value, str) and _DIGITS.match(value)):
                raise ValidationError({name: "must be digits only (1 to 78) or empty"})

    def save(self, *args, **kwargs):
        self.clean()  # the digit strings are validated on every write
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["detected_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_payment_idempotency_key_per_invoice",
            ),
            models.UniqueConstraint(
                fields=["network", "tx_hash", "log_index"],
                condition=~models.Q(tx_hash=""),
                name="unique_onchain_payment",
            ),
            models.UniqueConstraint(
                fields=["rail", "external_id"],
                condition=~models.Q(external_id=""),
                name="unique_payment_rail_external_id",
            ),
            models.CheckConstraint(
                condition=models.Q(invoice__isnull=False) | models.Q(state__in=("needs_review", "rejected")),
                name="payment_invoice_required_unless_review",
            ),
        ]

    def __str__(self):
        return f"Payment #{self.pk} ({self.state}) for invoice #{self.invoice_id or "-"}"
