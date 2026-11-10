import datetime
from decimal import Decimal
import re
import uuid

from types import SimpleNamespace

from django import forms
from django.conf import settings
from django.forms import BaseFormSet, formset_factory

from apps.invoices import limits
from apps.invoices.currencies import CURRENCIES, minor_units
from apps.invoices.models import Invoice
from apps.invoices.money import totals_of
from apps.orgs.models import Organization
from apps.payments.services import RAILS


MAX_MINOR = 10**12


# What a person recording money by hand sees for each rail (the value posted stays the rail's code).
RAIL_LABELS = {"bank": "Bank transfer", "fiat_link": "Payment link", "manual": "Recorded by hand"}


class OrganizationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        from .orgnames import org_name

        return org_name(obj)


class RecordPaymentForm(forms.Form):
    amount = forms.CharField(max_length=32)
    rail = forms.ChoiceField(choices=[(r, RAIL_LABELS.get(r, r)) for r in RAILS])
    note = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, currency="EUR", **kwargs):
        super().__init__(*args, **kwargs)
        self.currency = currency
        self.fields["amount"].label = f"Amount received ({currency})"

    def clean_amount(self):
        return parse_minor(self.cleaned_data["amount"], self.currency)


_PLAIN_NUMBER = re.compile(r"^(\d*)(?:[.,](\d+))?$")


def plain_decimal(text: str, label: str, places: int) -> Decimal:
    """A number read from the typed text: digits with at most one decimal separator ("." or ","),
    and no more decimal places than `places`; ".5" is 0.5. Too many digits after the separator is
    refused, never rounded, and the message names both readings: "12.345" EUR may be a price with
    three decimals or twelve thousand three hundred and forty-five with a thousands separator."""
    raw = (text or "").strip()
    match = _PLAIN_NUMBER.match(raw)
    if match is None or not (match.group(1) or match.group(2)):
        raise forms.ValidationError(f"{label}: enter a plain number such as 12.50, with no thousands separators.")
    whole, frac = match.group(1) or "0", match.group(2)
    if frac is not None and len(frac) > places:
        raise forms.ValidationError(f"{label}: at most {places} decimal places, and no thousands separators "
                                    f"(type 1000, not 1,000).")
    return Decimal(f"{whole}.{frac}") if frac is not None else Decimal(whole)


def parse_minor(text: str, currency: str) -> int:
    """An amount typed in major units, as whole minor units of `currency`."""
    exponent = minor_units(currency)
    value = plain_decimal(text, "Amount", exponent)
    if value <= 0:
        raise forms.ValidationError("The amount must be greater than zero.")
    if value > MAX_MINOR:
        raise forms.ValidationError("That amount is too large.")
    minor = value.scaleb(exponent)
    if minor > MAX_MINOR:
        raise forms.ValidationError("That amount is too large.")
    return int(minor)


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=2000)

    def clean_reason(self):
        value = self.cleaned_data["reason"].strip()
        if not value:
            raise forms.ValidationError("A reason is required.")
        return value


class OptionalReasonForm(forms.Form):
    reason = forms.CharField(max_length=2000, required=False)


class AssignPaymentForm(forms.Form):
    invoice = forms.CharField(max_length=64)
    amount = forms.CharField(max_length=32)
    note = forms.CharField(max_length=2000)


class RefundNoteForm(forms.Form):
    note = forms.CharField(max_length=2000)


class RequiredNoteForm(forms.Form):
    """A decision a person takes on Needs a person and must explain: keeping a payment the gateway
    calls invalid, accepting an exchange rate that moved more than 10 %."""

    note = forms.CharField(max_length=2000)


class NoteForm(forms.Form):
    note = forms.CharField(max_length=2000, required=False)


class TokenForm(forms.Form):
    token = forms.CharField(max_length=16, widget=forms.TextInput(
        attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "autofocus": True, "spellcheck": "false"}))


class PasskeyNameForm(forms.Form):
    """The name a passkey is listed under. The browser's response travels beside it and is verified
    by `apps.staff.passkeys`, never by a form."""

    name = forms.CharField(
        max_length=64, label="Name",
        help_text="Name it after what holds it, so you can tell your passkeys apart: for example "
                  "“MacBook Touch ID” or “YubiKey 5C”.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false", "placeholder": "MacBook Touch ID"}))


def parse_price(text: str, currency: str) -> int:
    """A unit price typed in major units, as minor units of `currency`. Never a float; more decimals
    than the currency has are refused, not rounded."""
    exponent = minor_units(currency)
    value = plain_decimal(text, "Unit price", exponent)
    if value > MAX_MINOR:
        raise forms.ValidationError("That amount is too large.")
    minor = value.scaleb(exponent)
    if minor > MAX_MINOR:
        raise forms.ValidationError("That amount is too large.")
    return int(minor)


def major_text(minor: int, currency: str) -> str:
    return format(Decimal(minor).scaleb(-minor_units(currency)), "f")


def _percent_to_bps(text: str, label: str) -> int:
    if not (text or "").strip():
        return 0
    value = plain_decimal(text, label, 2)
    if value > 100:
        raise forms.ValidationError(f"{label} must be between 0 and 100.")
    return int(value * 100)


US_CHOICES = [("", "Choose…"), ("yes", "Yes"), ("no", "No"), ("unknown", "Unknown")]
ADDRESS_PARTS = ("line1", "line2", "city", "zip", "state", "country")


class InvoiceForm(forms.Form):
    organization = OrganizationChoiceField(Organization.objects.order_by("slug"), label="Requesting organization",
                                           empty_label=None)
    kind = forms.ChoiceField(choices=[("invoice", "Invoice"), ("payment_request", "Payment request")], label="Type")
    email = forms.EmailField(max_length=320, label="Contact e-mail")
    legal_name = forms.CharField(max_length=255, required=False)
    display_name = forms.CharField(max_length=255, required=False)
    tax_ids = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), max_length=2000,
                              label="Tax IDs")
    address_line1 = forms.CharField(max_length=200, required=False, label="Address line 1")
    address_line2 = forms.CharField(max_length=200, required=False, label="Address line 2")
    address_city = forms.CharField(max_length=100, required=False, label="City")
    address_zip = forms.CharField(max_length=30, required=False, label="Postal code")
    address_state = forms.CharField(max_length=100, required=False, label="State or region")
    address_country = forms.CharField(max_length=100, required=False, label="Country")
    preferred_language = forms.ChoiceField(choices=[("", "Organization default")] + list(settings.LANGUAGES),
                                           required=False)
    us_tax_resident = forms.ChoiceField(choices=US_CHOICES, label="US tax resident")
    currency = forms.ChoiceField(choices=[(c, c) for c in CURRENCIES])
    issue_date = forms.DateField()
    due_days = forms.IntegerField(min_value=0, max_value=3650, required=False, label="Due in (days)",
                                  help_text="Leave empty for due on receipt.")
    title = forms.CharField(max_length=limits.MAX_TITLE, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), max_length=limits.MAX_NOTES)
    form_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing = editing
        if editing:  # who it is for and who asks cannot move once there is a draft
            for name in ("organization", "email"):
                self.fields[name].disabled = True
        if not self.is_bound and not self.initial.get("form_token") and not editing:
            self.initial["form_token"] = str(uuid.uuid4())
        self.initial.setdefault("issue_date", datetime.date.today())

    def clean_form_token(self):
        token = self.cleaned_data.get("form_token", "").strip()
        if self.editing:
            return ""
        try:
            return str(uuid.UUID(token))
        except ValueError:
            raise forms.ValidationError("The form expired; reload the page.")

    def contact_fields(self) -> dict:
        """What was typed about the contact; a blank never overwrites what is on file."""
        cd = self.cleaned_data
        out = {k: cd[k].strip() for k in ("legal_name", "display_name", "tax_ids") if cd[k].strip()}
        address = {part: cd[f"address_{part}"].strip() for part in ADDRESS_PARTS if cd[f"address_{part}"].strip()}
        if address:
            out["address"] = address
        if cd["preferred_language"]:
            out["preferred_language"] = cd["preferred_language"]
        out["us_tax_resident"] = cd["us_tax_resident"]
        return out


class LineForm(forms.Form):
    name = forms.CharField(max_length=limits.MAX_LINE_NAME)
    description = forms.CharField(max_length=limits.MAX_LINE_DESCRIPTION, required=False)
    quantity = forms.CharField(max_length=16, required=False, widget=forms.TextInput(attrs={"placeholder": "1"}))
    unit_price = forms.CharField(max_length=32, label="Unit price")
    tax_percent = forms.CharField(max_length=8, required=False, label="Tax %",
                                  widget=forms.TextInput(attrs={"placeholder": "0"}))
    discount_percent = forms.CharField(max_length=8, required=False, label="Discount %",
                                       widget=forms.TextInput(attrs={"placeholder": "0"}))

    def __init__(self, *args, currency="EUR", **kwargs):
        super().__init__(*args, **kwargs)
        self.currency = currency
        self.fields["unit_price"].label = f"Unit price ({currency})"

    def clean_quantity(self):
        raw = self.cleaned_data["quantity"].strip()
        if not raw:
            return Decimal(1)
        value = plain_decimal(raw, "Quantity", 2)
        if value <= 0:
            raise forms.ValidationError("The quantity must be greater than zero.")
        if value >= 10**10:
            raise forms.ValidationError("That quantity is too large.")
        return value

    def clean_unit_price(self):
        return parse_price(self.cleaned_data["unit_price"], self.currency)

    def clean_tax_percent(self):
        return _percent_to_bps(self.cleaned_data["tax_percent"], "Tax")

    def clean_discount_percent(self):
        return _percent_to_bps(self.cleaned_data["discount_percent"], "Discount")

    def as_line(self) -> dict:
        cd = self.cleaned_data
        return {"name": cd["name"].strip(), "description": cd["description"].strip() or None,
                "quantity": cd["quantity"], "unit_price_minor": cd["unit_price"],
                "tax_rate_bps": cd["tax_percent"], "discount_rate_bps": cd["discount_percent"]}


class BaseLineFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(form.cleaned_data for form in self.forms):
            raise forms.ValidationError("Add at least one line.")
        total = totals_of(SimpleNamespace(
            quantity=l["quantity"], unit_price_minor=l["unit_price_minor"], tax_rate_bps=l["tax_rate_bps"],
            discount_rate_bps=l["discount_rate_bps"], discount_amount_minor=0) for l in self.lines()).total
        if total <= 0:
            raise forms.ValidationError("The invoice total must be greater than zero.")

    def lines(self) -> list[dict]:
        return [form.as_line() for form in self.forms if form.cleaned_data]


LineFormSet = formset_factory(LineForm, formset=BaseLineFormSet, extra=4, max_num=limits.STAFF_FORM_LINES,
                              validate_max=True)


class LinksForm(forms.Form):
    email_client = forms.BooleanField(required=False, label="E-mail the new links to the client")


class SendForm(forms.Form):
    email_client = forms.BooleanField(required=False, label="E-mail the client when sent")
