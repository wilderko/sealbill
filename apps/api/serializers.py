"""Shapes only. Every rule lives in the services these views call."""
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.core.validators import MaxLengthValidator
from rest_framework import serializers

from apps.invoices import limits, money
from apps.payments.models import Declaration, Payment
from apps.rails.models import CHAIN_KINDS
from apps.webhooks.events import EVENT_TYPES

BIG = 10**12


def validate_quantity(value: str) -> str:
    try:
        q = Decimal(str(value).strip())
    except InvalidOperation:
        raise serializers.ValidationError("quantity must be a decimal number")
    if not q.is_finite() or q.normalize().as_tuple().exponent < -2:
        raise serializers.ValidationError("quantity may have at most 2 decimal places")
    if abs(q).to_integral_value(ROUND_DOWN) >= 10**10:
        raise serializers.ValidationError("quantity may have at most 10 integer digits")
    return str(value).strip()


def paginated(item):
    """The envelope LimitOffsetPagination really returns, for the OpenAPI schema."""
    name = item.__name__.replace("Serializer", "")
    return type(f"Paginated{name}List", (serializers.Serializer,), {
        "count": serializers.IntegerField(),
        "next": serializers.URLField(allow_null=True),
        "previous": serializers.URLField(allow_null=True),
        "results": item(many=True),
    })


LANGS = ("en", "sk", "cs", "de", "es", "fr", "pl", "ko", "ru")


class TotalsSerializer(serializers.Serializer):
    subtotal = serializers.IntegerField()
    discount = serializers.IntegerField()
    tax = serializers.IntegerField()
    total = serializers.IntegerField()


class LineInSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=limits.MAX_LINE_NAME)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True,
                                        max_length=limits.MAX_LINE_DESCRIPTION)
    quantity = serializers.CharField(required=False, default="1", validators=[validate_quantity],
                                     help_text="Decimal string, at most 10 integer and 2 decimal digits.")
    unit_price_minor = serializers.IntegerField(min_value=-BIG, max_value=BIG)
    tax_rate_bps = serializers.IntegerField(required=False, min_value=0, max_value=100000, default=0)
    discount_rate_bps = serializers.IntegerField(required=False, min_value=0, max_value=10000, default=0)
    discount_amount_minor = serializers.IntegerField(required=False, min_value=0, max_value=BIG, default=0)


class LineOutSerializer(serializers.Serializer):
    position = serializers.IntegerField()
    name = serializers.CharField(allow_null=True)
    description = serializers.CharField(allow_null=True)
    quantity = serializers.CharField()
    unit_price_minor = serializers.IntegerField()
    tax_rate_bps = serializers.IntegerField()
    discount_rate_bps = serializers.IntegerField()
    discount_amount_minor = serializers.IntegerField()

    def to_representation(self, item):
        data = super().to_representation(item)
        data["quantity"] = f"{item.quantity:.2f}"
        return data


class ContactFieldsSerializer(serializers.Serializer):
    legal_name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    display_name = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=255)
    address = serializers.JSONField(required=False, allow_null=True)
    tax_ids = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=64)
    notes = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    preferred_language = serializers.ChoiceField(choices=LANGS + ("",), required=False)
    us_tax_resident = serializers.ChoiceField(choices=("yes", "no", "unknown"), required=False)


class ContactCreateSerializer(ContactFieldsSerializer):
    email = serializers.EmailField()


class ContactOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    emails = serializers.SerializerMethodField()
    legal_name = serializers.CharField(allow_null=True)
    display_name = serializers.CharField(allow_null=True)
    address = serializers.JSONField(allow_null=True)
    tax_ids = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    notes = serializers.CharField(allow_null=True)
    preferred_language = serializers.CharField()
    us_tax_resident = serializers.CharField()
    created_at = serializers.DateTimeField()
    has_paid_invoice = serializers.SerializerMethodField(
        help_text="True when an invoice of this contact (or of the copy the US-resident issuer keeps of it) is paid "
                  "or overpaid: somebody paid under this contact. Offer only these as evidence of a client.")

    def get_emails(self, contact) -> list[str]:
        return [e.address for e in contact.emails.order_by("-primary", "pk")]

    def get_has_paid_invoice(self, contact) -> bool:
        from apps.contacts.services import has_paid_invoice

        return has_paid_invoice(contact)


class InvoiceCreateSerializer(serializers.Serializer):
    contact = ContactCreateSerializer(required=False)
    contact_id = serializers.IntegerField(required=False, min_value=1, max_value=2**31 - 1)
    currency = serializers.CharField(max_length=8)
    # Spelled out rather than many=True: the validator is what puts the cap (maxItems) in the schema.
    lines = serializers.ListSerializer(child=LineInSerializer(), allow_empty=False, max_length=limits.MAX_LINES,
                                       validators=[MaxLengthValidator(limits.MAX_LINES)])
    kind = serializers.ChoiceField(choices=("invoice", "payment_request"), default="invoice")
    issue_date = serializers.DateField(
        required=False, help_text=f"From {limits.EARLIEST_ISSUE_DATE.isoformat()} to "
                                  f"{limits.ISSUE_DATE_DAYS_AHEAD} days from today; today when omitted.")
    due_days = serializers.IntegerField(required=False, min_value=0, max_value=3650, allow_null=True)
    title = serializers.CharField(required=False, allow_blank=True, default="", max_length=limits.MAX_TITLE)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=limits.MAX_NOTES)
    external_reference = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=200)
    offered_methods = serializers.ListField(child=serializers.CharField(max_length=40, trim_whitespace=False), required=False, max_length=20)
    method_kinds = serializers.ListField(
        child=serializers.ChoiceField(choices=("bank", "fiat_link", "rail")), required=False, allow_empty=False,
        max_length=3,
        help_text="Instead of `offered_methods`: offer every active method of these kinds that the issuing "
                  "organization has (a US tax resident's issuer is the US-resident one). Resolved at creation; "
                  "409 `no_methods` when none exists. Giving both is 400 `methods_conflict`.")
    return_url = serializers.CharField(
        required=False, allow_blank=True, default="", trim_whitespace=False,
        help_text="https only, at most 500 characters. Its host must be listed in the requesting "
                  "organization's `return_url_hosts` (exact host; subdomains do not count), else 400 "
                  "`return_url_not_allowed`. The pay page shows a Back button to it. Frozen at send.")

    def validate_issue_date(self, value):
        error = limits.issue_date_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, attrs):
        if bool(attrs.get("contact")) == bool(attrs.get("contact_id")):
            raise serializers.ValidationError("give exactly one of `contact` (by e-mail) or `contact_id`")
        return attrs


class SendSerializer(serializers.Serializer):
    email_client = serializers.BooleanField(
        required=False, default=None, allow_null=True,
        help_text="E-mail the invoice letter (with the links) to the client. Default true, unless "
                  "`client_letters` is false. Only the invoice letter: receipts and the one reminder "
                  "(which replaces the links) are `client_letters`.")
    client_letters = serializers.BooleanField(
        required=False, default=True,
        help_text="Default true. False: you write to this client yourself, so no letter at all goes to them "
                  "for this invoice (not the invoice letter, the receipt, or the partial-payment letter). "
                  "Staff alerts and webhooks are unaffected. Stored at send; it cannot be changed later. "
                  "`true` for `email_client` together with false here is refused with 400.")

    def validate(self, attrs):
        if not attrs["client_letters"] and attrs.get("email_client"):
            raise serializers.ValidationError("`client_letters: false` contradicts `email_client: true`")
        if attrs.get("email_client") is None:
            attrs["email_client"] = attrs["client_letters"]
        return attrs


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ManualPaymentSerializer(serializers.Serializer):
    amount_minor = serializers.IntegerField(min_value=1, max_value=BIG)
    rail = serializers.ChoiceField(choices=("bank", "fiat_link", "manual"))
    note = serializers.CharField(required=False, allow_blank=True, default="")


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False)


class InvoiceOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    org = serializers.CharField(source="org.slug")
    issuer_org = serializers.SerializerMethodField()
    requested_by_org = serializers.CharField(source="requested_by_org.slug")
    issued_by_rule = serializers.BooleanField()
    contact_id = serializers.IntegerField()
    kind = serializers.CharField()
    status = serializers.CharField()
    number = serializers.CharField(allow_null=True)
    currency = serializers.CharField()
    issue_date = serializers.DateField()
    due_date = serializers.DateField(allow_null=True)
    title = serializers.CharField(allow_null=True)
    notes = serializers.CharField(allow_null=True)
    external_reference = serializers.CharField(allow_null=True)
    payment_link_id = serializers.IntegerField(allow_null=True, help_text="The payment link this invoice was ordered from.")
    coupon_code = serializers.CharField(source="coupon.code", allow_null=True,
                                        help_text="The coupon code the buyer used on the payment link.")
    offered_methods = serializers.ListField(child=serializers.CharField())
    return_url = serializers.CharField(read_only=True, help_text="Where the pay page's Back button leads; empty when none.")
    client_letters = serializers.BooleanField(
        read_only=True,
        help_text="False when the invoice was sent with `client_letters: false`: this system sends the "
                  "client no letter for it. Set at send, never changeable.")
    totals = serializers.SerializerMethodField()
    amount_paid_minor = serializers.SerializerMethodField(help_text="Confirmed payments, in the currency's minor units.")
    balance_due_minor = serializers.SerializerMethodField(help_text="max(total - paid, 0), in minor units.")
    lines = LineOutSerializer(many=True)
    created_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True)
    paid_at = serializers.DateTimeField(allow_null=True)
    voided_at = serializers.DateTimeField(allow_null=True)

    def get_issuer_org(self, invoice) -> str:
        return invoice.org.slug

    def get_totals(self, invoice) -> dict:
        return TotalsSerializer(money.totals(invoice)._asdict()).data

    def get_amount_paid_minor(self, invoice) -> int:
        from apps.invoices.services import confirmed_total_minor
        return confirmed_total_minor(invoice)

    def get_balance_due_minor(self, invoice) -> int:
        from apps.invoices.services import confirmed_total_minor
        return max(money.stored_total(invoice) - confirmed_total_minor(invoice), 0)


class InvoiceWithLinksSerializer(InvoiceOutSerializer):
    """Returned once by `send` and `reissue-links`; the plain tokens are never stored."""

    view_url = serializers.CharField()
    pay_url = serializers.CharField()


class PaymentOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    invoice_id = serializers.IntegerField(allow_null=True)
    rail = serializers.CharField(help_text="bank, fiat_link, manual, btcpay, lightning, evm, tron, solana or ton.")
    network = serializers.CharField()
    asset = serializers.CharField()
    amount_minor = serializers.IntegerField()
    received_amount = serializers.DecimalField(max_digits=38, decimal_places=18)
    amount_base_units = serializers.CharField(help_text="Exact amount received, an integer in the asset's smallest unit.")
    tx_hash = serializers.CharField()
    log_index = serializers.IntegerField()
    final = serializers.SerializerMethodField(
        help_text="True for a confirmed payment, or a chain transfer deep enough that staff may confirm it. False "
                  "when not confirmed yet: a chain transfer below finality, or a gateway payment not settled or "
                  "waiting for a person.")
    review_reason = serializers.CharField(help_text="Why the payment waits, or waited, for a person; kept after a person confirms it, empty if none ever had to.")
    refund_due_base_units = serializers.CharField(help_text="What was paid beyond what the quote settles; empty if nothing.")
    needs_staff_confirmation = serializers.BooleanField()
    quote = serializers.IntegerField(source="quote_id", allow_null=True)
    state = serializers.CharField()
    detected_at = serializers.DateTimeField()
    confirmed_at = serializers.DateTimeField(allow_null=True)
    evidence_note = serializers.CharField(allow_null=True)

    def get_final(self, payment) -> bool:
        """What the stored column says, or true for a confirmed payment off the chain: staff confirmed
        an XMR or ZEC payment (or recorded a manual one) and nothing about it is still pending. Output
        only: the column itself, which the services read, is not changed."""
        return bool(payment.final or (payment.state == "confirmed" and payment.rail not in CHAIN_KINDS))


class DeclarationOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    invoice_id = serializers.IntegerField()
    network = serializers.CharField()
    tx_hash = serializers.CharField()
    note = serializers.CharField(allow_null=True)
    state = serializers.CharField()
    created_at = serializers.DateTimeField()


class QuoteRequestSerializer(serializers.Serializer):
    method = serializers.RegexField(r"^rail:[0-9]{1,12}$", help_text="`rail:<id>`, one of the invoice's `offered_methods`.")
    network = serializers.CharField(max_length=32)
    asset = serializers.CharField(max_length=16)
    refresh = serializers.BooleanField(required=False, default=False,
                                       help_text="Cancel the open quote for this key and issue a new one.")


class QuoteOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    rail = serializers.CharField(source="rail_account.method_key", help_text="`rail:<id>`.")
    network = serializers.CharField()
    asset = serializers.CharField()
    amount_base_units = serializers.CharField(help_text="Exact amount to send, an integer in the asset's smallest unit.")
    amount = serializers.SerializerMethodField(help_text="The same amount in whole units, a decimal string.")
    address = serializers.CharField(help_text="Where to send it; empty for BTCPay and Lightning.")
    payment_uri = serializers.CharField()
    bolt11 = serializers.CharField()
    checkout_url = serializers.CharField()
    invoice_amount_minor = serializers.IntegerField(help_text="The part of the invoice this quote settles.")
    rate = serializers.CharField()
    rate_source = serializers.CharField()
    expires_at = serializers.DateTimeField()
    state = serializers.ChoiceField(choices=("open", "expired", "cancelled", "paid", "released"))
    money_may_be_coming = serializers.SerializerMethodField(
        help_text="True while money for this quote may still be on its way, or nobody could look for it. A BTCPay "
                  "or Lightning quote: a payment the gateway has seen but not settled, or no good read since the "
                  "quote expired. A chain quote: it still holds its amount (it is not released, about a day after "
                  "it closes), a watcher of its account and network is failing or has not read since it expired, "
                  "or blocks of its address were skipped and nobody has checked them yet. Do not void the invoice "
                  "while any quote says so.")
    money_may_be_coming_reason = serializers.SerializerMethodField(
        help_text="Why `money_may_be_coming` is true, or empty when it is false: `quote_held` (a chain quote still "
                  "holds its amount), `watcher_failing`, `watcher_behind` (the chain scan has not read since the "
                  "quote expired), `blocks_not_scanned` (skipped blocks wait for a person) or `gateway` (a BTCPay or "
                  "Lightning quote). Only `quote_held` ends by itself, a day after the quote closes; the watcher "
                  "reasons last as long as the outage does.")

    def _reason(self, quote) -> str:
        """Asked once per quote and kept on it: the flag and the reason come from the same reading."""
        cached = getattr(quote, "_money_may_be_coming_reason", None)
        if cached is not None:
            return cached
        from apps.quotes.models import Quote
        from apps.quotes.services import could_not_look, gateway_money_may_be_coming
        from apps.rails.models import CHAIN_KINDS

        this = Quote.objects.filter(pk=quote.pk)
        if quote.rail_account.kind in CHAIN_KINDS:  # audit LS-2: the chain watcher's "could not look" too
            reason = could_not_look(this, read_past_expiry=True) or ""
        else:
            reason = "gateway" if this.filter(gateway_money_may_be_coming()).exists() else ""
        quote._money_may_be_coming_reason = reason
        return reason

    def get_money_may_be_coming(self, quote) -> bool:
        return self._reason(quote) != ""

    def get_money_may_be_coming_reason(self, quote) -> str:
        return self._reason(quote)

    def get_amount(self, quote) -> str:
        from apps.portal.crypto import decimals_for
        from apps.rails.uris import decimal_amount

        decimals = decimals_for(quote.network, quote.asset)
        return decimal_amount(int(quote.amount_base_units), decimals) if decimals is not None else ""


class AssignSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    amount_minor = serializers.IntegerField(min_value=1, max_value=BIG, help_text="Credited, in the invoice currency.")
    note = serializers.CharField(allow_blank=False, trim_whitespace=True)


class PaymentOptionSerializer(serializers.Serializer):
    network = serializers.CharField()
    asset = serializers.CharField()


class PaymentMethodSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="`bank:<id>`, `fiat:<id>` or `rail:<id>`, as used in `offered_methods`.")
    kind = serializers.ChoiceField(choices=("bank", "fiat", "rail"))
    label = serializers.CharField(allow_null=True)
    currencies = serializers.ListField(child=serializers.CharField())
    options = PaymentOptionSerializer(many=True, required=False,
                                      help_text="Rails only: the network and asset pairs a quote may name.")


class PaymentMethodsSerializer(serializers.Serializer):
    currency = serializers.CharField()
    issuer_org = serializers.CharField()
    issued_by_rule = serializers.BooleanField()
    methods = PaymentMethodSerializer(many=True)


class KeyInSerializer(serializers.Serializer):
    key = serializers.CharField(trim_whitespace=False,
                                help_text="One PGP public key (ASCII-armored) or one S/MIME certificate (PEM).")


class KeyOutSerializer(serializers.Serializer):
    """Never the key material: what was uploaded stays in the vault."""

    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=("pgp", "smime"))
    fingerprint = serializers.CharField(help_text="Uppercase hex: the OpenPGP fingerprint, or the SHA-256 of the "
                                                  "certificate.")
    state = serializers.ChoiceField(choices=("pending", "active", "replaced", "revoked", "failed"))
    expires_at = serializers.DateTimeField(allow_null=True,
                                           help_text="When encryption to it stops working; null: never.")
    source = serializers.ChoiceField(
        choices=("staff", "api", "client", "mirror"),
        help_text="`mirror`: a copy of another organization's key for this address, kept by the US-resident "
                  "issuer rule; it follows that key and cannot be added through the API.")


class WebhookCreateSerializer(serializers.Serializer):
    url = serializers.CharField(max_length=2000, trim_whitespace=False,
                                help_text="https only; the host must resolve to public addresses only.")
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=("*", *EVENT_TYPES)), required=False, allow_empty=False,
        help_text='Event types to receive; ["*"] (the default) is all of them.')
    description = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class WebhookOutSerializer(serializers.Serializer):
    """Never the secret, and never the URL's query string or credentials."""

    id = serializers.IntegerField()
    url = serializers.SerializerMethodField(help_text="Scheme, host and path; a query string shows as `?...`.")
    events = serializers.ListField(child=serializers.CharField())
    description = serializers.CharField()
    active = serializers.BooleanField()
    created_at = serializers.DateTimeField()

    def get_url(self, endpoint) -> str:
        from apps.webhooks.delivery import display_url

        return display_url(endpoint.url)


class WebhookCreatedSerializer(WebhookOutSerializer):
    """The new endpoint with its signing secret: the only response that ever carries it."""

    secret = serializers.SerializerMethodField(
        help_text="Signs every delivery (X-Pay-Signature). Shown in this response only.")

    def get_secret(self, _endpoint) -> str:
        return self.context["secret"]


class WebhookDeliveryOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    event_id = serializers.UUIDField()
    event_type = serializers.CharField(source="event.type")
    state = serializers.ChoiceField(choices=("queued", "sent", "failed", "cancelled"))
    attempts = serializers.IntegerField()
    last_status_code = serializers.IntegerField(allow_null=True)
    last_error_kind = serializers.CharField(help_text="http_<status>, redirect, timeout, tls, connection, "
                                                      "refused:<scheme|private_address|unresolvable|bad_url>, ...")
    next_attempt_at = serializers.DateTimeField(allow_null=True)
    sent_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()


class MeSerializer(serializers.Serializer):
    organization = serializers.CharField()
    token = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())


class ErrorSerializer(serializers.Serializer):
    error = serializers.CharField()
    detail = serializers.JSONField()


class CustomQuestionSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    required = serializers.BooleanField(default=False)


class PaymentLinkFieldsSerializer(serializers.Serializer):
    """What may be set on a payment link. The rules are the staff form's: `apps.links.services` applies them."""

    title = serializers.CharField(max_length=200, trim_whitespace=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True, allow_null=True)
    internal_note = serializers.CharField(max_length=5000, required=False, allow_blank=True, allow_null=True,
                                          help_text="Staff only; never shown to buyers.")
    currency = serializers.CharField(max_length=8)
    customer_chooses = serializers.BooleanField(required=False, help_text="The buyer enters the amount, within min and max.")
    price_minor = serializers.IntegerField(required=False, allow_null=True, help_text="Fixed price, minor units.")
    min_minor = serializers.IntegerField(required=False, allow_null=True)
    max_minor = serializers.IntegerField(required=False, allow_null=True)
    tax_rate_bps = serializers.IntegerField(required=False, min_value=0, max_value=10000)
    max_quantity = serializers.IntegerField(required=False, min_value=0, max_value=200,
                                            help_text="Per order; 0 means any quantity up to 200.")
    name_field = serializers.CharField(max_length=8, required=False, help_text="off, optional or required.")
    billing_field = serializers.CharField(max_length=8, required=False, help_text="off, optional or required.")
    shipping_field = serializers.CharField(max_length=8, required=False, help_text="off, optional or required.")
    phone_field = serializers.CharField(max_length=8, required=False, help_text="off, optional or required.")
    custom_fields = CustomQuestionSerializer(many=True, required=False, allow_null=True, help_text="Up to five.")
    kind = serializers.CharField(max_length=16, required=False, help_text="invoice or payment_request.")
    methods = serializers.CharField(max_length=8, required=False, help_text="default or custom.")
    method_types = serializers.ListField(child=serializers.CharField(max_length=60), required=False,
                                         help_text="With methods=custom: method types as GET /api/v1/payment-methods names them.")
    fulfillment = serializers.BooleanField(required=False)
    notify_staff = serializers.BooleanField(required=False)
    active = serializers.BooleanField(required=False, help_text="false deactivates the link.")
    valid_until = serializers.DateTimeField(required=False, allow_null=True)
    max_uses = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class PaymentLinkOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    org = serializers.CharField(source="org.slug")
    title = serializers.CharField(allow_null=True)
    description = serializers.CharField(allow_null=True)
    internal_note = serializers.CharField(allow_null=True)
    currency = serializers.CharField()
    customer_chooses = serializers.BooleanField()
    price_minor = serializers.IntegerField(allow_null=True)
    min_minor = serializers.IntegerField(allow_null=True)
    max_minor = serializers.IntegerField(allow_null=True)
    tax_rate_bps = serializers.IntegerField()
    max_quantity = serializers.IntegerField()
    name_field = serializers.CharField()
    billing_field = serializers.CharField()
    shipping_field = serializers.CharField()
    phone_field = serializers.CharField()
    custom_fields = CustomQuestionSerializer(many=True, allow_null=True)
    kind = serializers.CharField()
    methods = serializers.CharField()
    method_types = serializers.ListField(child=serializers.CharField())
    fulfillment = serializers.BooleanField()
    notify_staff = serializers.BooleanField()
    active = serializers.BooleanField()
    valid_until = serializers.DateTimeField(allow_null=True)
    max_uses = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PaymentLinkDetailSerializer(PaymentLinkOutSerializer):
    """One link. `url` is the only place the link's secret leaves the system: whoever holds it can buy.
    A token that may only read gets null (context `show_url` false, red-team A4)."""

    url = serializers.SerializerMethodField(
        help_text="The public page. Null for a token without `invoices:write`, and when the stored copy "
                  "cannot be opened (rotate).")

    def get_url(self, link) -> str | None:
        from apps.vault.fields import UndecryptableValue

        if not self.context.get("show_url", True):
            return None
        try:
            return link.url_for(link.token) if link.token else None
        except UndecryptableValue:
            return None
