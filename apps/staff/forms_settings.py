"""Forms of the staff Settings area. Each form is a ModelForm over one settings model; the views save
them through `views_settings.audited_save`."""
import io
import re
from decimal import Decimal
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.contacts.models import Contact
from apps.invoices.currencies import CURRENCIES
from apps.invoices.return_url import is_hostname
from apps.invoices.snapshot import _instructions
from apps.orgs.models import BankAccount, FiatLink, Organization, default_org_settings
from apps.rails import addresses
from apps.rails.models import BTCPAY_ASSETS, BTCPAY_SPEED_POLICIES, CHAIN_KINDS, LN_BACKENDS, RailAccount, ton_enabled
from apps.rails.networks import NETWORKS

from .forms import ADDRESS_PARTS

# Apache refuses a request body over 2 MiB (deploy/apache/*.example); the logo must leave room for the form.
MAX_LOGO_BYTES = 1992294  # 1.9 MiB
LOGO_TOO_BIG = "The logo may be at most 1.9 MB."
LOGO_FORMATS = {"PNG", "JPEG"}
MAX_LOGO_PIXELS = 40_000_000
MAX_LOGO_SIDE = 1600
SETTINGS_KEYS = ("underpay_allowance_bps", "underpay_allowance_usd", "overpay_ceiling_bps", "accounting_currency")


def _lf(value):
    return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else value


def line_endings_as_lf(form, *names):
    """A browser posts a textarea with CRLF; an unchanged `Line1\\nLine2` would then look edited, be
    audited and be stored with CRLF. The field normalises what it reads, so `changed_data` and the
    saved value both compare and store LF, and so does the stored initial value."""
    for name in names:
        field = form.fields[name]
        original = field.to_python
        field.to_python = lambda value, _original=original: _lf(_original(value))
        if isinstance(form.initial.get(name), str):
            form.initial[name] = _lf(form.initial[name])
        if isinstance(field.initial, str):
            field.initial = _lf(field.initial)


def clean_logo(upload) -> ContentFile:
    """A PNG or JPEG of at most 1.9 MB, re-encoded by Pillow as a PNG: EXIF, GPS, ICC and text chunks are
    dropped, so only pixels are stored. The PDF reads the logo by its .png name."""
    if getattr(upload, "size", 0) > MAX_LOGO_BYTES:
        raise forms.ValidationError(LOGO_TOO_BIG)
    raw = upload.read(MAX_LOGO_BYTES + 1)
    if len(raw) > MAX_LOGO_BYTES:
        raise forms.ValidationError(LOGO_TOO_BIG)
    message = "The logo must be a PNG or JPEG picture."
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in LOGO_FORMATS:
                raise forms.ValidationError(message)
            if probe.size[0] * probe.size[1] > MAX_LOGO_PIXELS:
                raise forms.ValidationError("The logo is too large: at most 40 megapixels.")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)  # the camera's orientation is metadata, and metadata goes
            img.thumbnail((MAX_LOGO_SIDE, MAX_LOGO_SIDE))
            clean = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
            clean.info = {}
            out = io.BytesIO()
            clean.save(out, "PNG", optimize=True)
    except forms.ValidationError:
        raise
    except Image.DecompressionBombError:
        raise forms.ValidationError("The logo is too large: at most 40 megapixels.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise forms.ValidationError(message) from None
    return ContentFile(out.getvalue(), name="logo.png")


def _host_lines(value) -> str:
    """Hosts one per line: trimmed, lowercase, blanks and repeats dropped. The same normalisation reads the
    stored list and the typed text, so a page that is posted back unchanged changes nothing."""
    if isinstance(value, (list, tuple)):
        value = "\n".join(v for v in value if isinstance(v, str))
    seen = []
    for line in re.split(r"[\r\n]+", value or ""):
        host = line.strip().lower()
        if host and host not in seen:
            seen.append(host)
    return "\n".join(seen)


class HostLinesField(forms.CharField):
    def to_python(self, value):
        return _host_lines(super().to_python(value))


class OrganizationForm(forms.ModelForm):
    logo = forms.FileField(
        required=False, help_text="PNG or JPEG, at most 1.9 MB. Stored as a PNG without metadata.",
        widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg", "data-max-bytes": str(MAX_LOGO_BYTES),
                                      "data-max-bytes-message": LOGO_TOO_BIG}))
    remove_logo = forms.BooleanField(required=False, label="Remove the logo")
    underpay_allowance_bps = forms.IntegerField(
        min_value=0, max_value=10000, label="Underpayment allowance (basis points)",
        help_text="A payment short by at most this share of the invoice still settles it. 50 is 0.5%.")
    underpay_allowance_usd = forms.DecimalField(
        min_value=Decimal("0"), decimal_places=2, max_digits=12, label="Underpayment allowance (USD)",
        help_text="The same, as a flat amount in US dollars.")
    overpay_ceiling_bps = forms.IntegerField(
        min_value=0, max_value=100000, label="Overpayment ceiling (basis points)",
        help_text="A payment above this share over the invoice is held for a person, not settled.")
    accounting_currency = forms.ChoiceField(choices=[(c, c) for c in CURRENCIES], label="Accounting currency",
                                            help_text="The currency of the accounting columns in the exports.")
    default_language = forms.ChoiceField(choices=django_settings.LANGUAGES, label="Default language")
    return_url_hosts = HostLinesField(
        required=False, label="Hosts a Back button may lead to", max_length=5000,
        widget=forms.Textarea(attrs={"rows": 3, "spellcheck": "false"}),
        help_text="One host per line, such as shop.example.com. An invoice this organization requests "
                  "may carry a return_url on exactly these hosts (https only, subdomains do not count).")

    class Meta:
        model = Organization
        fields = ["slug", "legal_name", "address", "tax_ids", "email_from", "footer_note", "logo",
                  "default_language", "number_format", "is_us_resident_issuer"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stored = self.instance.settings if isinstance(self.instance.settings, dict) else {}
        # A key the organization never had shows its default, so the browser posts what the page shows.
        current = {**default_org_settings(), **{k: v for k, v in stored.items() if k in SETTINGS_KEYS}}
        # Initial values whatever the binding, typed like the cleaned values, so changed_data (and the
        # audit entry built from it) names only the fields that really changed.
        for key in SETTINGS_KEYS:
            try:
                value = current[key]
                if key == "underpay_allowance_usd":
                    value = Decimal(str(value)).quantize(Decimal("0.01"))
                elif key in ("underpay_allowance_bps", "overpay_ceiling_bps"):
                    value = int(value)
                else:
                    value = str(value or default_org_settings()[key]).upper()  # any case; null or empty is the default
            except (ArithmeticError, ValueError, TypeError):
                value = Decimal(str(default_org_settings()[key])).quantize(Decimal("0.01")) \
                    if key == "underpay_allowance_usd" else default_org_settings()[key]
            self.initial[key] = value
        self.initial["return_url_hosts"] = _host_lines(stored.get("return_url_hosts"))
        code = self.initial["accounting_currency"]
        if code not in CURRENCIES:
            self.fields["accounting_currency"].choices = [(code, f"{code} (unsupported)"),
                                                          *self.fields["accounting_currency"].choices]
        self.fields["legal_name"].required = True
        if not self.instance.logo:
            del self.fields["remove_logo"]
        code = self.instance.default_language
        if code and code not in dict(django_settings.LANGUAGES):
            self.fields["default_language"].choices = [(code, f"{code} (unsupported)"), *django_settings.LANGUAGES]
        line_endings_as_lf(self, "address", "tax_ids", "footer_note")
        self.fields["is_us_resident_issuer"].label = "US-resident issuer"
        self.fields["is_us_resident_issuer"].help_text = "At most one organization can be the issuer for US tax residents."

    def clean_logo(self):
        upload = self.cleaned_data.get("logo")
        if upload and hasattr(upload, "read") and self.files.get(self.add_prefix("logo")) is upload:
            return clean_logo(upload)
        return upload

    @property
    def changed_data(self):
        names = [n for n in super().changed_data if n != "remove_logo"]
        if self._removing_logo and "logo" not in names:
            names.append("logo")
        return names

    @property
    def _removing_logo(self) -> bool:
        return bool(self.is_bound and "remove_logo" in self.fields and self.cleaned_data.get("remove_logo")
                    and not self.files.get(self.add_prefix("logo")))  # a new file wins over removal

    def clean_return_url_hosts(self):
        text = self.cleaned_data["return_url_hosts"]
        bad = [h for h in text.split("\n") if h and not is_hostname(h)]
        if bad:
            raise forms.ValidationError(
                f"Not a bare host name: {bad[0][:60]}. Write only the host, such as shop.example.com.")
        return text

    def clean_is_us_resident_issuer(self):
        flag = self.cleaned_data["is_us_resident_issuer"]
        if flag:
            others = Organization.objects.filter(is_us_resident_issuer=True).exclude(pk=self.instance.pk)
            if others.exists():
                raise forms.ValidationError("Another organization is already the US-resident issuer.")
        return flag

    def clean(self):
        cleaned = super().clean()
        if self._removing_logo:
            cleaned["logo"] = False  # the model field's way of saying "clear it"
        merged = dict(self.instance.settings) if isinstance(self.instance.settings, dict) else {}
        for key in SETTINGS_KEYS:
            if key in cleaned:
                value = cleaned[key]
                merged[key] = str(value.quantize(Decimal("0.01"))) if isinstance(value, Decimal) else value
        if "return_url_hosts" in cleaned:
            merged["return_url_hosts"] = [h for h in cleaned["return_url_hosts"].split("\n") if h]
        self.instance.settings = merged
        return cleaned


MAX_INSTRUCTION_CHARS = 2000


class InstructionsMixin:
    """One optional textarea per site language for the `instructions` JSON of a payment method. Stored
    languages the site does not offer are kept as they are."""

    def _init_instructions(self):
        stored = _instructions(self.instance.instructions)
        self.instruction_fields = []
        for code, name in django_settings.LANGUAGES:
            key = f"instr_{code}"
            self.fields[key] = forms.CharField(
                required=False, label=f"Instructions ({name})", max_length=MAX_INSTRUCTION_CHARS,
                widget=forms.Textarea(attrs={"rows": 3}))
            value = stored.get(code)
            self.initial[key] = value if isinstance(value, str) else ""  # bound or not: changed_data stays exact
            self.instruction_fields.append(self[key])
        line_endings_as_lf(self, *(f"instr_{code}" for code, _ in django_settings.LANGUAGES))

    @property
    def changed_data(self):
        names = super().changed_data
        collapsed = [n for n in names if not n.startswith("instr_")]
        if len(collapsed) != len(names):
            collapsed.append("instructions")
        return collapsed

    def clean(self):
        cleaned = super().clean()
        if any(n.startswith("instr_") for n in super().changed_data):
            merged = _instructions(self.instance.instructions)
            for code, _name in django_settings.LANGUAGES:
                text = (cleaned.get(f"instr_{code}") or "").strip()
                if text:
                    merged[code] = text
                else:
                    merged.pop(code, None)
            self.instance.instructions = merged or None
        return cleaned


class BankAccountForm(InstructionsMixin, forms.ModelForm):
    currency = forms.ChoiceField(choices=[(c, c) for c in CURRENCIES])

    class Meta:
        model = BankAccount
        fields = ["label", "currency", "beneficiary_name", "beneficiary_address", "account_number", "bank_name",
                  "bank_address", "bank_swift", "intermediary_bank_name", "intermediary_swift",
                  "intermediary_account"]
        labels = {"account_number": "IBAN / account number", "bank_swift": "SWIFT / BIC",
                  "intermediary_swift": "Intermediary SWIFT / BIC"}
        widgets = {"beneficiary_address": forms.Textarea(attrs={"rows": 3}),
                   "bank_address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_instructions()
        line_endings_as_lf(self, "beneficiary_address", "bank_address")
        code = self.instance.currency
        if code and code not in CURRENCIES:
            self.fields["currency"].choices = [(code, f"{code} (unsupported)"), *self.fields["currency"].choices]


class FiatLinkForm(InstructionsMixin, forms.ModelForm):
    currencies = forms.MultipleChoiceField(choices=[(c, c) for c in CURRENCIES], required=False,
                                           widget=forms.CheckboxSelectMultiple,
                                           help_text="The invoice currencies this link can take.")
    url = forms.CharField(max_length=2000, label="Payment URL", help_text="Must start with https://")

    class Meta:
        model = FiatLink
        fields = ["kind", "label", "url", "currencies"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_instructions()
        current = self.instance.currencies if isinstance(self.instance.currencies, list) else []
        current = [str(c) for c in current]
        self.initial["currencies"] = current  # a list of codes, whatever the binding, so changed_data is exact
        extra = [c for c in current if c not in CURRENCIES]
        if extra:
            self.fields["currencies"].choices = [*self.fields["currencies"].choices, *[(c, f"{c} (unsupported)") for c in extra]]

    def clean_url(self):
        url = (self.cleaned_data["url"] or "").strip()
        if not url.lower().startswith("https://") or len(url) <= len("https://") or any(ch.isspace() for ch in url):
            raise forms.ValidationError("The URL must start with https://")
        return url

    def clean_currencies(self):
        return sorted(self.cleaned_data["currencies"])


def url_problem(url: str, *, loopback_ok: bool) -> str:
    """Why a rail URL may not be used, or "". A credential travels to it, and the worker calls it: https
    only, so it cannot be read on the way, and only to public addresses (audit SEC-4, the webhook
    resolver's rule; `http.destination_problem` checks the URL as the calls do, resolving the host). A
    node on this machine (LND, CLN: `loopback_ok`) only on LOCAL_LIGHTNING_PORT, where plain http is
    allowed too."""
    from apps.rails import http

    port = http.local_lightning_port()
    local = f" A node on this machine is reached only on port {port}, the LOCAL_LIGHTNING_PORT setting." if port \
        else " A node on this machine needs the LOCAL_LIGHTNING_PORT setting to name its port."
    code = http.destination_problem(url, http.LOCAL_NODE if loopback_ok else http.STAFF_URL)
    if code is None:
        return ""
    if code == "bad_url":
        return "Enter a full URL, such as https://host.example, without spaces, backslashes or a user name."
    if code == "unresolvable":
        return "The host name could not be resolved."
    if code == "private_address":
        return "The host resolves to a private, loopback or reserved address." + (local if loopback_ok else "")
    if loopback_ok and port:
        return f"The URL must start with https:// (http only for this machine, on port {port}: localhost or 127.0.0.1)."
    return "The URL must start with https://." + (local if loopback_ok else "")


def url_host(url) -> tuple:
    """(host, port) of a stored URL, for telling "another service" from "another path"."""
    try:
        parts = urlsplit(str(url or "").strip())
        return ((parts.hostname or "").lower(), parts.port)
    except ValueError:
        return (str(url), None)


class RailAccountForm(forms.ModelForm):
    """One form per rail kind. Organization and kind are fixed by the view (chosen at create time,
    read-only afterwards); `credentials` is never a field. Kind-specific `config` keys are separate
    fields merged into the JSON, and every list/JSON initial is typed like its cleaned value so
    `changed_data` (and the audit entry) names only what really changed."""

    networks = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    assets = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    url = forms.CharField(required=False, max_length=500, label="URL")
    store_id = forms.CharField(required=False, max_length=200, label="Store ID")
    speed_policy = forms.ChoiceField(required=False, label="Speed policy")
    backend = forms.ChoiceField(required=False, label="Backend")

    class Meta:
        model = RailAccount
        fields = ["label", "active", "address", "networks", "assets"]

    def __init__(self, kind, *args, **kwargs):
        args = list(args)
        data = args[0] if args else kwargs.get("data")
        if data is not None and kind in CHAIN_KINDS and (data.get("address") or "").strip():
            # The same address in another spelling is no change: compare the normalized form, as the
            # model stores it, so the audit entry does not name `address` for a re-spelling.
            try:
                normalized = addresses.normalize(kind, data["address"].strip())
            except ValueError:
                normalized = None
            if normalized:
                data = data.copy()
                data["address"] = normalized
                if args:
                    args[0] = data
                else:
                    kwargs["data"] = data
        super().__init__(*args, **kwargs)
        inst = self.instance
        self.kind = kind
        inst.kind = kind
        cfg = inst.config if isinstance(inst.config, dict) else {}
        creating = inst.pk is None
        if kind in CHAIN_KINDS:
            family_nets = {k: n for k, n in NETWORKS.items() if n.family == kind}
            for name in ("url", "store_id", "speed_policy", "backend"):
                del self.fields[name]
            if kind == "evm":
                self.fields["networks"].choices = [(k, n.name) for k, n in family_nets.items()]
            else:
                del self.fields["networks"]
                if creating:
                    inst.networks = list(family_nets)
            symbols = sorted({s for n in family_nets.values() for s in n.assets})
            self.fields["assets"].choices = [(s, s) for s in symbols]
            if kind == "ton" and not ton_enabled() and creating:
                self.initial["active"] = False
        else:
            del self.fields["address"], self.fields["networks"]
            if kind == "btcpay":
                del self.fields["backend"]
                self.fields["assets"].choices = [(a, a) for a in BTCPAY_ASSETS]
                self.fields["speed_policy"].choices = [(p, p) for p in BTCPAY_SPEED_POLICIES]
                self.initial["speed_policy"] = str(cfg.get("speed_policy", "MediumSpeed"))
                self.initial["url"] = str(cfg.get("url", ""))
                self.initial["store_id"] = str(cfg.get("store_id", ""))
            else:  # lightning
                for name in ("store_id", "speed_policy", "assets"):
                    del self.fields[name]
                inst.assets = ["BTC"]
                self.fields["backend"].choices = [("", "---------"), *[(b, b) for b in LN_BACKENDS]]
                self.fields["backend"].required = True
                self.initial["backend"] = str(cfg.get("backend", ""))
                self.initial["url"] = str(cfg.get("url", ""))
                self.fields["url"].help_text = "Needed for lnd, cln and lnbits; not used for nwc."
        # typed initials, bound or not, so changed_data compares like with like
        for name in ("networks", "assets"):
            if name in self.fields:
                self.initial[name] = [str(x) for x in (getattr(inst, name) or [])]
                known = {c for c, _ in self.fields[name].choices}
                extra = [x for x in self.initial[name] if x not in known]
                if extra:
                    self.fields[name].choices = [*self.fields[name].choices, *[(x, f"{x} (unsupported)") for x in extra]]

    def clean_networks(self):
        return sorted(self.cleaned_data["networks"])

    def clean_assets(self):
        return sorted(self.cleaned_data["assets"])

    def clean(self):
        cleaned = super().clean()
        if self.kind in ("btcpay", "lightning"):
            cfg = dict(self.instance.config) if isinstance(self.instance.config, dict) else {}
            keys = ("url", "store_id", "speed_policy") if self.kind == "btcpay" else ("backend", "url")
            for key in keys:
                value = (cleaned.get(key) or "").strip()
                if value:
                    cfg[key] = value
                else:
                    cfg.pop(key, None)
            if self.kind == "lightning" and cfg.get("backend") in ("nwc", None):
                cfg.pop("url", None)
            elif self.kind == "lightning" and not cfg.get("url"):
                self.add_error("url", "A URL is needed for this backend.")
            if cfg.get("url") and "url" not in self.errors:
                loopback_ok = self.kind == "lightning" and cfg.get("backend") in ("lnd", "cln")
                problem = url_problem(cfg["url"], loopback_ok=loopback_ok)
                if problem:
                    self.add_error("url", problem)
            self.instance.config = cfg
        return cleaned

    def _update_errors(self, errors):
        # The model reports `config` and `kind`, which are not form fields: show them where they belong.
        error_dict = getattr(errors, "error_dict", None)
        if error_dict:
            for key in [k for k in error_dict if k not in self.fields and k != NON_FIELD_ERRORS]:
                moved = error_dict.pop(key)
                text = " ".join(m for e in moved for m in e.messages)
                locked = re.search(r"its (url|store_id|backend) cannot change", text)
                if key == "config" and locked and locked.group(1) in self.fields:
                    target = locked.group(1)
                elif key == "config" and self.kind == "btcpay" and "speed_policy" in text:
                    target = "speed_policy"
                elif key == "config" and self.kind == "lightning" and "backend" in text:
                    target = "backend"
                else:
                    target = NON_FIELD_ERRORS
                error_dict.setdefault(target, []).extend(moved)
        super()._update_errors(errors)


class ContactForm(forms.ModelForm):
    """A contact's fields. The address is edited as typed parts (the shape the invoice form uses) and
    stored as the JSON object the model holds. The organization and e-mail addresses are not editable."""
    _PARTS = ADDRESS_PARTS

    address_line1 = forms.CharField(max_length=200, required=False, label="Address line 1")
    address_line2 = forms.CharField(max_length=200, required=False, label="Address line 2")
    address_city = forms.CharField(max_length=100, required=False, label="City")
    address_zip = forms.CharField(max_length=30, required=False, label="Postal code")
    address_state = forms.CharField(max_length=100, required=False, label="State or region")
    address_country = forms.CharField(max_length=100, required=False, label="Country")
    preferred_language = forms.ChoiceField(choices=[("", "Organization default")] + list(django_settings.LANGUAGES),
                                           required=False)
    us_tax_resident = forms.ChoiceField(choices=Contact.US_TAX_CHOICES, label="US tax resident")

    class Meta:
        model = Contact
        fields = ("legal_name", "display_name", "tax_ids", "phone", "notes", "preferred_language", "us_tax_resident")
        widgets = {"tax_ids": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"tax_ids": "Tax IDs"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        address = self.instance.address if isinstance(self.instance.address, dict) else {}
        for part in self._PARTS:
            self.fields[f"address_{part}"].initial = str(address.get(part) or "")
        for name in ("legal_name", "display_name", "tax_ids", "phone", "notes"):
            self.fields[name].required = False
        line_endings_as_lf(self, "tax_ids", "notes")

    @property
    def changed_data(self):
        names = super().changed_data
        collapsed = [n for n in names if not n.startswith("address_")]
        if len(collapsed) != len(names):
            collapsed.append("address")
        return collapsed

    def save(self, commit=True):
        if any(n.startswith("address_") for n in super().changed_data):
            # Start from what is stored: keys outside the six parts survive, and untouched parts stay as they are.
            stored = dict(self.instance.address) if isinstance(self.instance.address, dict) else {}
            for part in self._PARTS:
                if f"address_{part}" in super().changed_data:
                    value = self.cleaned_data[f"address_{part}"].strip()
                    if value:
                        stored[part] = value
                    else:
                        stored.pop(part, None)
            self.instance.address = stored or None
        return super().save(commit)
