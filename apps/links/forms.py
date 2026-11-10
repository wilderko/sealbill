"""The buyer's checkout form (client-facing: every word in the nine languages) and the staff forms
(English, like every staff page)."""
import datetime
import re
from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.invoices.currencies import CURRENCIES, minor_units
from apps.invoices.pdf import format_money
from apps.orgs.models import Organization
from apps.staff.forms import OrganizationChoiceField, _percent_to_bps, parse_price, plain_decimal

from . import methods as method_types
from .models import (
    FIELD_MODES, MAX_CUSTOM_ANSWER, MAX_CUSTOM_FIELDS, MAX_CUSTOM_LABEL, MAX_DESCRIPTION, MAX_NOTE, MAX_TITLE,
    UNLIMITED_QUANTITY, PaymentLink,
)
from .services import Order

ADDRESS_PARTS = ("line1", "line2", "city", "zip", "state", "country")
_NUMBER = re.compile(r"^(\d*)(?:[.,](\d+))?$")


def _address_labels():
    return {"line1": _("Address line 1"), "line2": _("Address line 2"), "city": _("City"), "zip": _("Postal code"),
            "state": _("State or region"), "country": _("Country")}


# ---------------------------------------------------------------------------
# The buyer
# ---------------------------------------------------------------------------
class CheckoutForm(forms.Form):
    """The fields the link asks for. Amount bounds, the quantity cap and the coupon are checked again by
    the service (`apps.links.services.checkout`); its refusals come back onto these fields."""

    email = forms.EmailField(max_length=320, label=_("E-mail address"),
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "spellcheck": "false"}))
    us_tax_resident = forms.ChoiceField(label=_("Are you a US tax resident?"), widget=forms.RadioSelect,
                                        choices=[("yes", _("Yes, I am")), ("no", _("No, I am not"))])
    coupon = forms.CharField(max_length=40, required=False, label=_("Coupon code"),
                             widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}))

    def __init__(self, *args, link: PaymentLink, **kwargs):
        super().__init__(*args, **kwargs)
        self.link = link
        self.address_groups = []
        if not link.coupons.filter(active=True).exists():  # no coupon to type: no field for one
            del self.fields["coupon"]
        if link.customer_chooses:
            self.fields["amount"] = forms.CharField(max_length=24, label=_("Amount (%(currency)s)") % {
                "currency": link.currency}, widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}))
            self.fields["amount"].help_text = self._bounds_text()
        if link.quantity_cap > 1 and not link.customer_chooses:
            self.fields["quantity"] = forms.IntegerField(
                label=_("Quantity"), min_value=1, max_value=link.quantity_cap, initial=1,
                error_messages={"min_value": self._quantity_message(), "max_value": self._quantity_message()})
        if link.name_field != "off":
            self.fields["name"] = forms.CharField(max_length=255, required=link.name_field == "required", label=_("Full name"),
                                                  widget=forms.TextInput(attrs={"autocomplete": "name"}))
        for group, mode, title in (("billing", link.billing_field, _("Billing address")),
                                   ("shipping", link.shipping_field, _("Shipping address"))):
            if mode == "off":
                continue
            names = []
            for part, text in _address_labels().items():
                required = mode == "required" and part in ("line1", "city", "country")
                name = f"{group}_{part}"
                auto = {"line1": "address-line1", "line2": "address-line2", "city": "address-level2",
                        "zip": "postal-code", "state": "address-level1", "country": "country-name"}[part]
                self.fields[name] = forms.CharField(max_length=200, required=required, label=text,
                                                    widget=forms.TextInput(attrs={"autocomplete": f"{group} {auto}"}))
                names.append(name)
            self.address_groups.append({"title": title, "optional": mode == "optional", "names": names})
        if link.phone_field != "off":
            self.fields["phone"] = forms.CharField(max_length=40, required=link.phone_field == "required",
                                                   label=_("Phone"),
                                                   widget=forms.TextInput(attrs={"autocomplete": "tel", "type": "tel"}))
        for i, entry in enumerate(link.custom_fields or []):
            self.fields[f"custom_{i}"] = forms.CharField(max_length=MAX_CUSTOM_ANSWER, required=entry.get("required"),
                                                         label=entry.get("label"))
        self.order_fields(self._order())

    def _order(self):
        names = ["amount", "quantity", "email"]
        names += ["name"] if "name" in self.fields else []
        names += [n for g in self.address_groups for n in g["names"]]
        names += ["phone"] if "phone" in self.fields else []
        names += [n for n in self.fields if n.startswith("custom_")]
        return names + ["us_tax_resident", "coupon"]

    # -- what the page shows beside the fields
    @property
    def purchase_fields(self):
        """What is bought: the amount (when the buyer chooses it) or the quantity (when there may be more than one)."""
        return [self[n] for n in ("amount", "quantity") if n in self.fields]

    @property
    def detail_fields(self):
        """The buyer's details outside the address groups, in page order."""
        grouped = {n for g in self.address_groups for n in g["names"]}
        return [self[n] for n in self.fields if n not in grouped and n not in ("us_tax_resident", "amount", "quantity",
                                                                               "coupon")]

    @property
    def bounds_text(self):
        return self._bounds_text() if self.link.customer_chooses else ""

    @property
    def address_sections(self):
        """Each address as one group: an optional address says so once, on its title, and its parts are
        not marked one by one; lines 1 and 2 take the whole width."""
        out = []
        for g in self.address_groups:
            fields = []
            for n in g["names"]:
                bound = self[n]
                bound.wide = n.endswith(("_line1", "_line2"))
                bound.marked = not g["optional"] and not bound.field.required
                fields.append(bound)
            out.append({"title": g["title"], "optional": g["optional"], "fields": fields})
        return out

    def _money(self, minor):
        return format_money(minor, self.link.currency)

    def _bounds_text(self):
        lo, hi = self.link.min_minor, self.link.max_minor
        if lo is not None and hi is not None:
            return _("Between %(min)s and %(max)s.") % {"min": self._money(lo), "max": self._money(hi)}
        if lo is not None:
            return _("At least %(min)s.") % {"min": self._money(lo)}
        if hi is not None:
            return _("At most %(max)s.") % {"max": self._money(hi)}
        return ""

    def _quantity_message(self):
        return _("Choose a quantity from 1 to %(max)s.") % {"max": self.link.quantity_cap}

    # -- cleaning
    def clean_amount(self):
        raw = (self.cleaned_data.get("amount") or "").strip().replace(" ", "")
        match = _NUMBER.match(raw)
        places = minor_units(self.link.currency)
        if not raw or match is None or not (match.group(1) or match.group(2)) or (
                match.group(2) is not None and len(match.group(2)) > places):
            raise forms.ValidationError(_("Enter the amount as a plain number, such as 12.50."))
        whole, frac = match.group(1) or "0", match.group(2)
        value = Decimal(f"{whole}.{frac}") if frac is not None else Decimal(whole)
        if value > 10**12:
            raise forms.ValidationError(self.amount_message("amount_high"))
        minor = int(value.scaleb(places))
        if minor <= 0:
            raise forms.ValidationError(self.amount_message("amount_low"))
        return minor

    def amount_message(self, code):
        lo, hi = self.link.min_minor, self.link.max_minor
        if code == "amount_low":
            if lo:
                return _("The amount must be at least %(min)s.") % {"min": self._money(lo)}
            return _("The amount must be greater than zero.")
        return _("The amount may be at most %(max)s.") % {"max": self._money(hi or 10**12)}

    def clean_coupon(self):
        return (self.cleaned_data.get("coupon") or "").strip()[:40]

    def order(self) -> Order:
        cd = self.cleaned_data

        def address(group):
            parts = {p: (cd.get(f"{group}_{p}") or "").strip() for p in ADDRESS_PARTS if f"{group}_{p}" in self.fields}
            return {k: v for k, v in parts.items() if v} or None

        return Order(
            email=cd["email"], us_tax_resident=cd["us_tax_resident"], name=(cd.get("name") or "").strip(),
            billing=address("billing"), shipping=address("shipping"), phone=(cd.get("phone") or "").strip(),
            custom=[(entry["label"], (cd.get(f"custom_{i}") or "").strip())
                    for i, entry in enumerate(self.link.custom_fields or [])],
            amount_minor=cd.get("amount"), quantity=cd.get("quantity") or 1, coupon_code=cd.get("coupon") or "",
        )


CHECKOUT_ERRORS = {
    "quantity": ("quantity", None),
    "coupon_invalid": ("coupon", _("This code is not valid.")),
    "coupon_used_up": ("coupon", _("This code has been used up.")),
}


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------
class LocalDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


def _optional_minor(text, currency, label):
    if not (text or "").strip():
        return None
    value = plain_decimal(text, label, minor_units(currency))
    minor = value.scaleb(minor_units(currency))
    if minor > 10**12:
        raise forms.ValidationError(f"{label}: that amount is too large.")
    return int(minor)


class LinkForm(forms.Form):
    organization = OrganizationChoiceField(Organization.objects.order_by("slug"), label="Organization",
                                           empty_label=None,
                                           help_text="Who sells. A US tax resident's order is issued by the "
                                                     "US-resident organization instead.")
    title = forms.CharField(max_length=MAX_TITLE, help_text="Shown to buyers, and the line on the invoice.")
    description = forms.CharField(max_length=MAX_DESCRIPTION, required=False, widget=forms.Textarea(attrs={"rows": 3}),
                                  help_text="Shown to buyers, as written.")
    image = forms.FileField(required=False, help_text="PNG, JPEG or WebP, at most 1.5 MB; 960 × 540 suits the page.")
    remove_image = forms.BooleanField(required=False, label="Remove the current image")
    kind = forms.ChoiceField(choices=PaymentLink.KINDS, label="Document",
                             help_text="A payment request has no legal invoice document and no number.")
    currency = forms.ChoiceField(choices=[(c, c) for c in CURRENCIES], initial="EUR")
    customer_chooses = forms.BooleanField(required=False, label="The customer chooses the amount")
    price = forms.CharField(max_length=32, required=False, label="Price")
    min_amount = forms.CharField(max_length=32, required=False, label="Minimum amount")
    max_amount = forms.CharField(max_length=32, required=False, label="Maximum amount")
    tax_percent = forms.CharField(max_length=8, required=False, label="Tax %",
                                  widget=forms.TextInput(attrs={"placeholder": "0"}))
    max_quantity = forms.IntegerField(min_value=0, max_value=UNLIMITED_QUANTITY, initial=1,
                                      label="Maximum quantity per order",
                                      help_text=f"0 means unlimited (up to {UNLIMITED_QUANTITY}).")
    name_field = forms.ChoiceField(choices=FIELD_MODES, initial="required", label="Name")
    billing_field = forms.ChoiceField(choices=FIELD_MODES, initial="off", label="Billing address")
    shipping_field = forms.ChoiceField(choices=FIELD_MODES, initial="off", label="Shipping address")
    phone_field = forms.ChoiceField(choices=FIELD_MODES, initial="off", label="Phone")
    methods = forms.ChoiceField(choices=PaymentLink.METHODS, initial="default", widget=forms.RadioSelect,
                                label="Payment methods")
    method_types = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
                                             label="Custom set")
    fulfillment = forms.BooleanField(required=False, label="Paid orders need fulfilling",
                                     help_text="They are listed as to fulfil until someone marks them fulfilled.")
    notify_staff = forms.BooleanField(required=False, label="E-mail staff for each paid order")
    active = forms.BooleanField(required=False, initial=True, label="Active")
    valid_until = forms.DateTimeField(required=False, label="Valid until (UTC)", widget=LocalDateTimeInput(),
                                      input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"])
    max_uses = forms.IntegerField(required=False, min_value=1, label="Total uses",
                                  help_text="Orders in all; empty means no limit. An unpaid order holds its use 60 minutes.")
    internal_note = forms.CharField(max_length=MAX_NOTE, required=False, widget=forms.Textarea(attrs={"rows": 2}),
                                    help_text="Staff only.")

    def __init__(self, *args, link=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.link = link
        if link is not None:
            self.fields["organization"].disabled = True  # its orders and coupons are that organization's
        else:
            del self.fields["remove_image"]
        known = set()
        for org in Organization.objects.all():
            known.update(method_types.types_of(org))
        if link is not None:
            known.update(link.method_types or [])
        if self.is_bound:
            known.update(t for t in self.data.getlist("method_types") if method_types.valid(t))
        self.fields["method_types"].choices = [(t, method_types.label(t)) for t in sorted(known, key=method_types._order)]
        for i in range(MAX_CUSTOM_FIELDS):
            self.fields[f"custom_label_{i}"] = forms.CharField(max_length=MAX_CUSTOM_LABEL, required=False,
                                                               label=f"Question {i + 1}")
            self.fields[f"custom_required_{i}"] = forms.BooleanField(required=False, label="Required")

    @property
    def custom_rows(self):
        return [(self[f"custom_label_{i}"], self[f"custom_required_{i}"]) for i in range(MAX_CUSTOM_FIELDS)]

    def clean_tax_percent(self):
        return _percent_to_bps(self.cleaned_data["tax_percent"], "Tax")

    def clean(self):
        cd = super().clean()
        currency = cd.get("currency") or "EUR"
        try:
            cd["price_minor"] = parse_price(cd["price"], currency) if (cd.get("price") or "").strip() else None
        except forms.ValidationError as exc:
            self.add_error("price", exc)
        for field, label in (("min_amount", "Minimum amount"), ("max_amount", "Maximum amount")):
            try:
                cd[field.replace("_amount", "_minor")] = _optional_minor(cd.get(field), currency, label)
            except forms.ValidationError as exc:
                self.add_error(field, exc)
        return cd

    def link_fields(self) -> dict:
        cd = self.cleaned_data
        return {
            "title": cd["title"], "description": cd["description"], "internal_note": cd["internal_note"],
            "currency": cd["currency"], "customer_chooses": cd["customer_chooses"], "price_minor": cd.get("price_minor"),
            "min_minor": cd.get("min_minor"), "max_minor": cd.get("max_minor"), "tax_rate_bps": cd["tax_percent"],
            "max_quantity": cd["max_quantity"] if cd["max_quantity"] is not None else 1,
            "name_field": cd["name_field"], "billing_field": cd["billing_field"],
            "shipping_field": cd["shipping_field"], "phone_field": cd["phone_field"],
            "custom_fields": [{"label": cd.get(f"custom_label_{i}", ""), "required": cd.get(f"custom_required_{i}")}
                              for i in range(MAX_CUSTOM_FIELDS)],
            "kind": cd["kind"], "methods": cd["methods"], "method_types": cd["method_types"],
            "fulfillment": cd["fulfillment"], "notify_staff": cd["notify_staff"], "active": cd["active"],
            "valid_until": _aware(cd.get("valid_until")), "max_uses": cd.get("max_uses"),
        }


def _aware(value):
    if value is None or timezone.is_aware(value):
        return value
    return timezone.make_aware(value, datetime.timezone.utc)


def percent_text(bps: int) -> str:
    """Basis points as a plain percentage for a form field: 2000 -> "20", 1250 -> "12.5"."""
    text = format(Decimal(bps) / 100, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def initial_for(link: PaymentLink) -> dict:
    """The edit form, filled from the link."""
    from apps.staff.forms import major_text

    initial = {
        "organization": link.org_id, "title": link.title, "description": link.description,
        "internal_note": link.internal_note, "kind": link.kind, "currency": link.currency,
        "customer_chooses": link.customer_chooses,
        "price": major_text(link.price_minor, link.currency) if link.price_minor is not None else "",
        "min_amount": major_text(link.min_minor, link.currency) if link.min_minor is not None else "",
        "max_amount": major_text(link.max_minor, link.currency) if link.max_minor is not None else "",
        "tax_percent": percent_text(link.tax_rate_bps) if link.tax_rate_bps else "",
        "max_quantity": link.max_quantity, "name_field": link.name_field, "billing_field": link.billing_field,
        "shipping_field": link.shipping_field, "phone_field": link.phone_field, "methods": link.methods,
        "method_types": list(link.method_types or []), "fulfillment": link.fulfillment,
        "notify_staff": link.notify_staff, "active": link.active, "valid_until": link.valid_until,
        "max_uses": link.max_uses,
    }
    for i, entry in enumerate(link.custom_fields or []):
        initial[f"custom_label_{i}"] = entry.get("label")
        initial[f"custom_required_{i}"] = entry.get("required")
    return initial


class CouponForm(forms.Form):
    code = forms.CharField(max_length=40, help_text="Letters, digits, hyphens; buyers type it in any case.")
    name = forms.CharField(max_length=200, required=False)
    percent = forms.CharField(max_length=8, label="Discount %")
    max_uses = forms.IntegerField(required=False, min_value=1, label="Remaining uses",
                                  help_text="Empty: no limit. An unpaid order holds its use 60 minutes.")
    valid_until = forms.DateTimeField(required=False, label="Valid until (UTC)", widget=LocalDateTimeInput(),
                                      input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"])
    internal_note = forms.CharField(max_length=MAX_NOTE, required=False, label="Internal note")

    def clean_percent(self):
        bps = _percent_to_bps(self.cleaned_data["percent"], "Discount")
        if not 1 <= bps <= 9999:
            raise forms.ValidationError("The discount is more than 0 % and less than 100 %: a coupon never makes an "
                                        "order free.")
        return bps

    def clean_valid_until(self):
        return _aware(self.cleaned_data.get("valid_until"))
