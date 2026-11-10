import re

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.quotes import groups

# Canonical digit strings: no sign, no leading zero, so equal amounts are equal strings and the
# unique constraint on the amount means what it says.
_CANONICAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
AMOUNT_FIELDS = ("amount_base_units", "min_accept_base_units", "ceiling_base_units")


class Quote(models.Model):
    """For one invoice and one rail: how much of which asset on which network settles what the
    invoice still owes, at which rate, until when.

    On a shared receiving address the amount is unique among the quotes of its **amount group**
    that hold it (`apps.quotes.groups`: every network and every USD stablecoin the address takes),
    so an arrival is matched by amount alone. An amount stays held after the quote closes and is
    freed only when the quote is `released`. Every column is plaintext: watchers, matching and
    the worker read and change quotes while the vault is sealed.
    """

    STATES = [(s, s) for s in ("open", "expired", "cancelled", "paid", "released")]

    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="quotes")
    rail_account = models.ForeignKey("rails.RailAccount", on_delete=models.PROTECT, related_name="quotes")
    network = models.CharField(max_length=32)
    asset = models.CharField(max_length=16)
    address = models.CharField(max_length=128, blank=True, default="")  # "" for BTCPay and Lightning
    # Integer base units as digit strings: SQLite would round a big integer column.
    amount_base_units = models.CharField(max_length=78)
    min_accept_base_units = models.CharField(max_length=78)
    ceiling_base_units = models.CharField(max_length=78)
    invoice_amount_minor = models.BigIntegerField()  # the part of the invoice this quote settles
    rate = models.CharField(max_length=64, blank=True, default="")
    rate_second = models.CharField(max_length=64, blank=True, default="")
    rate_source = models.CharField(max_length=64, blank=True, default="")
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=STATES, default="open")
    external_id = models.CharField(max_length=128, blank=True, default="")  # BTCPay invoice id or LN hash
    payment_uri = models.TextField(blank=True, default="")
    checkout_url = models.CharField(max_length=500, blank=True, default="")
    bolt11 = models.TextField(blank=True, default="")
    check_soon = models.BooleanField(default=False)
    # BTCPay and Lightning: when the gateway last answered for this quote. A quote not read since
    # it expired may still have money on its way, so it is polled and kept until it is.
    read_at = models.DateTimeField(null=True, blank=True)
    # Written by `save` from the columns above; "" for a quote without an address. The unique
    # constraint is on (amount_group, group_amount): the amount at a common number of decimals.
    amount_group = models.CharField(max_length=160, blank=True, default="")
    group_amount = models.CharField(max_length=96, blank=True, default="")
    # Issued before amount groups existed, and another held quote of its group already had the
    # same amount: kept as it was (a client may be paying it), outside the unique constraint until
    # it is released. Only migration 0003 sets it; the allocator still counts its amount as held.
    shares_amount = models.BooleanField(default=False)

    class Meta:
        ordering = ["issued_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["amount_group", "group_amount"],
                condition=~Q(state="released") & ~Q(address="") & Q(shares_amount=False),
                name="unique_held_quote_amount",
            ),
            models.UniqueConstraint(
                fields=["rail_account", "external_id"],
                condition=~Q(external_id=""),
                name="unique_quote_external_id",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "expires_at"], name="quote_state_expires"),
            models.Index(fields=["state", "closed_at"], name="quote_state_closed"),
            models.Index(fields=["invoice", "state"], name="quote_invoice_state"),
        ]

    def __str__(self):
        return f"Quote #{self.pk} ({self.state}) for invoice #{self.invoice_id}"

    @property
    def amount(self) -> int:
        return int(self.amount_base_units)

    @property
    def min_accept(self) -> int:
        return int(self.min_accept_base_units)

    @property
    def ceiling(self) -> int:
        return int(self.ceiling_base_units)

    def clean(self):
        super().clean()
        values = {name: self.__dict__.get(name) for name in AMOUNT_FIELDS}
        if any(v is None for v in values.values()):
            return  # a deferred field is not loaded here, nor checked
        for name, value in values.items():
            if not (isinstance(value, str) and _CANONICAL.match(value)):
                raise ValidationError({name: "must be a whole number written in digits, without leading zeros"})
        if int(values["amount_base_units"]) <= 0:
            raise ValidationError({"amount_base_units": "must be positive"})
        if not int(values["min_accept_base_units"]) <= int(values["amount_base_units"]) <= int(
                values["ceiling_base_units"]):
            raise ValidationError({"amount_base_units": "must lie between the minimum accepted and the ceiling"})

    def save(self, *args, **kwargs):
        self.clean()  # the amounts are validated on every write
        self._set_amount_group()
        super().save(*args, **kwargs)

    def _set_amount_group(self):
        loaded = [self.__dict__.get(n) for n in ("network", "asset", "address", "amount_base_units")]
        if any(v is None for v in loaded):
            return  # a deferred column: the stored group stays as it is
        network, asset, address, amount = loaded
        self.amount_group = groups.amount_group(network, asset, address)
        self.group_amount = str(groups.group_amount(network, asset, int(amount))) if self.amount_group else ""
