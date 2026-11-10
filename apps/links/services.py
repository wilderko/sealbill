"""Payment links: creating and editing them, checkout, and what happens to an order afterwards.

Checkout (spec 2.2) makes, in one transaction: the contact when the address is new, an invoice of the
link's document kind with one line (the title, the quantity, the price or the amount chosen, the
coupon's percentage off before tax, the link's tax), issued under the US-resident rule, and sends it
without an e-mail (`send_invoice(email_client=False)`: number, tokens, snapshot). The buyer goes on to
its pay page. Everything after that is the ordinary flow; `order_paid` is called by `apply_payments`.

Rules that are ours, not Zaprite's (2.3, 2.3a):
- nothing is e-mailed before payment; an unpaid order is voided after VOID_AFTER_DAYS, as the system;
- a public form never shows or changes what a contact stores: the order is booked to an existing
  contact with the typed address, its buyer block is what was typed;
- the order is routed on the typed answer to the US question alone, never on the stored flag (2.3a
  item 3, revised 2026-11-04: a public link reveals nothing stored); a typed answer the stored flag
  contradicts is one staff item, and a typed "no" against a stored "yes" is held from fulfilment
  until a person resolves it (`held_for_us_review`);
- the link's and the coupon's uses are held HOLD_MINUTES at checkout and become uses when paid.
"""
import base64
import dataclasses
import datetime
import io
import logging
import re
from types import SimpleNamespace

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit import services as audit
from apps.contacts.models import Contact
from apps.contacts.services import create_unrecorded_contact, find_contact
from apps.invoices import money, tokens
from apps.invoices.currencies import CURRENCIES
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices.services import (
    SETTLED, _live_methods, create_invoice, default_methods, forget_link_copies, resolve_issuer, send_invoice,
    void_invoice,
)
from apps.vault.vault import vault

from . import methods as method_types
from .models import (
    HOLD_MINUTES, MAX_CUSTOM_ANSWER, MAX_CUSTOM_FIELDS, MAX_CUSTOM_LABEL, MAX_DESCRIPTION, MAX_NOTE, MAX_TITLE,
    UNLIMITED_QUANTITY, VOID_AFTER_DAYS, Coupon, LinkAlert, LinkImage, LinkOrder, PaymentLink,
)

logger = logging.getLogger(__name__)

# Under Apache's LimitRequestBody (2 MiB, deploy/apache), with room for the rest of the form, so an image
# too large is refused here, in our own words, not by Apache with a bare 413 (final review m3).
MAX_IMAGE_BYTES = 1536 * 1024
MAX_IMAGE_SIDE = 1920  # larger pictures are scaled down: the page carries it as a data: URI
MAX_IMAGE_PIXELS = 40_000_000  # read off the header and refused before a single pixel is decoded
IMAGE_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
COUPON_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,39}$")
MAX_PRICE_MINOR = 10**12
FIELD_MODE_VALUES = ("off", "optional", "required")
# What staff may set through `create_link` / `update_link`.
LINK_FIELDS = (
    "title", "description", "internal_note", "currency", "customer_chooses", "price_minor", "min_minor",
    "max_minor", "tax_rate_bps", "max_quantity", "name_field", "billing_field", "shipping_field", "phone_field",
    "custom_fields", "kind", "methods", "method_types", "fulfillment", "notify_staff", "active", "valid_until",
    "max_uses",
)


class LinkError(ValueError):
    """A link or coupon a person tried to save breaks a rule; the message says which (in English)."""


class CheckoutError(Exception):
    """Checkout refused. `code`: unavailable, amount_low, amount_high, quantity, coupon_invalid,
    coupon_used_up, no_methods, no_us_issuer, total."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclasses.dataclass
class Order:
    """What a buyer typed on the link's form."""

    email: str
    us_tax_resident: str  # "yes" or "no"
    name: str = ""
    billing: dict | None = None
    shipping: dict | None = None
    phone: str = ""
    custom: list = dataclasses.field(default_factory=list)  # [(label, answer)]
    amount_minor: int | None = None
    quantity: int = 1
    coupon_code: str = ""


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
def _clean(fields: dict, link: PaymentLink | None = None) -> dict:
    """The fields as stored, or LinkError. Called with every field the link will have."""
    out = dict(fields)
    out["title"] = (out.get("title") or "").strip()
    if not out["title"]:
        raise LinkError("A title is required.")
    if len(out["title"]) > MAX_TITLE:
        raise LinkError(f"The title may have at most {MAX_TITLE} characters.")
    for name, cap in (("description", MAX_DESCRIPTION), ("internal_note", MAX_NOTE)):
        out[name] = (out.get(name) or "").strip() or None
        if out[name] and len(out[name]) > cap:
            raise LinkError(f"The {name.replace('_', ' ')} may have at most {cap} characters.")
    out["currency"] = (out.get("currency") or "").upper()
    if out["currency"] not in CURRENCIES:
        raise LinkError("Choose a currency.")
    chooses = bool(out.get("customer_chooses"))
    out["customer_chooses"] = chooses
    for name in ("price_minor", "min_minor", "max_minor"):
        value = out.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0
                                  or value > MAX_PRICE_MINOR):
            raise LinkError("An amount must be a whole number of minor units between 0 and 10^12.")
    if chooses:
        out["price_minor"] = None
        lo, hi = out.get("min_minor"), out.get("max_minor")
        if lo is not None and hi is not None and lo > hi:
            raise LinkError("The minimum amount is above the maximum.")
        if hi == 0:
            raise LinkError("The maximum amount must be greater than zero.")
    else:
        out["min_minor"] = out["max_minor"] = None
        if not out.get("price_minor"):
            raise LinkError("A fixed price must be greater than zero.")
    tax = out.get("tax_rate_bps") or 0
    if not 0 <= tax <= 10000:
        raise LinkError("Tax must be between 0 and 100 %.")
    out["tax_rate_bps"] = tax
    quantity = out.get("max_quantity", 1)
    if quantity is None or not 0 <= quantity <= UNLIMITED_QUANTITY:
        raise LinkError(f"The quantity per order is 0 (unlimited, up to {UNLIMITED_QUANTITY}) to {UNLIMITED_QUANTITY}.")
    out["max_quantity"] = quantity
    for name in ("name_field", "billing_field", "shipping_field", "phone_field"):
        out[name] = out.get(name) or ("required" if name == "name_field" else "off")
        if out[name] not in FIELD_MODE_VALUES:
            raise LinkError(f"Unknown setting for {name}.")
    custom = []
    for entry in out.get("custom_fields") or []:
        text = (entry.get("label") or "").strip()
        if not text:
            continue
        if len(text) > MAX_CUSTOM_LABEL:
            raise LinkError(f"A question may have at most {MAX_CUSTOM_LABEL} characters.")
        custom.append({"label": text, "required": bool(entry.get("required"))})
    if len(custom) > MAX_CUSTOM_FIELDS:
        raise LinkError(f"At most {MAX_CUSTOM_FIELDS} extra questions.")
    out["custom_fields"] = custom or None
    if out.get("kind", "invoice") not in ("invoice", "payment_request"):
        raise LinkError("Unknown document type.")
    out["kind"] = out.get("kind", "invoice")
    mode = out.get("methods") or "default"
    if mode not in ("default", "custom"):
        raise LinkError("Unknown payment-method setting.")
    out["methods"] = mode
    types = sorted({str(t) for t in out.get("method_types") or []})
    if mode == "custom":
        if not types:
            raise LinkError("Choose at least one payment method, or use the issuer's defaults.")
        bad = [t for t in types if not method_types.valid(t)]
        if bad:
            raise LinkError(f"Unknown payment method: {bad[0]}.")
        foreign = [t for t in types if t.startswith("bank:") and t != f"bank:{out['currency']}"]
        if foreign:
            raise LinkError(f"A bank transfer must be in the link's currency ({out['currency']}).")
    out["method_types"] = types if mode == "custom" else []
    for name in ("fulfillment", "notify_staff"):
        out[name] = bool(out.get(name))
    out["active"] = bool(out.get("active", True))
    max_uses = out.get("max_uses")
    if max_uses is not None and max_uses < 1:
        raise LinkError("Total uses must be at least 1, or left empty.")
    return {name: out.get(name) for name in LINK_FIELDS}


def create_link(org, *, actor=None, image=None, channel: str = "staff", **fields) -> tuple[PaymentLink, str]:
    """A new link of `org` and its token (shown to staff; stored hashed and encrypted)."""
    clean = _clean(fields)
    plain, digest = tokens.new_token()
    with transaction.atomic():
        link = PaymentLink.objects.create(org=org, token_hash=digest, token=plain, **clean)
        audit.record(actor, "link.create", link, {"org": org.slug, "currency": link.currency, "kind": link.kind,
                                                  "active": link.active, "channel": channel})
        if image is not None:
            set_image(link, image, actor=actor)
    return link, plain


def update_link(link: PaymentLink, *, actor=None, channel: str = "staff", **fields) -> PaymentLink:
    """Change a link. Orders already made keep what they were made with; the URL does not change."""
    current = {name: getattr(link, name) for name in LINK_FIELDS}
    clean = _clean({**current, **fields})
    changed = sorted(name for name, value in clean.items() if current[name] != value)
    if not changed:
        return link
    with transaction.atomic():
        for name in changed:
            setattr(link, name, clean[name])
        link.save(update_fields=[*changed, "updated_at"])
        audit.record(actor, "link.update", link, {"fields": changed, "channel": channel})
        if "active" in changed and not link.active:
            audit.record(actor, "link.deactivate", link, {"channel": channel})
    return link


def rotate_token(link: PaymentLink, *, actor=None, channel: str = "staff") -> str:
    """A new URL; the old one stops working at once (audited)."""
    plain, digest = tokens.new_token()
    with transaction.atomic():
        PaymentLink.objects.filter(pk=link.pk).update(token_hash=digest, token=plain)
        audit.record(actor, "link.rotate", link, {"link": link.pk, "channel": channel})
    link.token_hash, link.token = digest, plain
    return plain


def resolve_token(plain: str) -> PaymentLink | None:
    if not plain:
        return None
    return PaymentLink.objects.select_related("org").filter(token_hash=tokens.hash_token(plain)).first()


def uses_taken(link: PaymentLink, *, now=None, exclude_order=None) -> int:
    """Orders of the link that count against its total: paid ones, and holds still running."""
    return _taken(Q(invoice__payment_link=link), now, exclude_order)


def coupon_uses_taken(coupon: Coupon, *, now=None, exclude_order=None) -> int:
    return _taken(Q(invoice__coupon=coupon), now, exclude_order)


def _taken(scope: Q, now, exclude_order) -> int:
    now = now or timezone.now()
    rows = LinkOrder.objects.filter(scope).filter(
        Q(hold="used") | Q(hold="reserved", reserved_until__gt=now, invoice__status__in=_HOLDING))
    if exclude_order is not None:
        rows = rows.exclude(pk=exclude_order)
    return rows.count()


_HOLDING = ("sent", "overdue", "declared", "partially_paid", "paid", "overpaid")


def unavailable_reason(link: PaymentLink, *, now=None) -> str | None:
    """Why the link takes no orders now ("inactive", "expired", "used_up"), or None."""
    now = now or timezone.now()
    if not link.active:
        return "inactive"
    if link.valid_until is not None and link.valid_until <= now:
        return "expired"
    if link.max_uses is not None and uses_taken(link, now=now) >= link.max_uses:
        return "used_up"
    return None


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------
def normalize_code(code: str) -> str:
    return (code or "").strip().upper()


def add_coupon(link: PaymentLink, *, code: str, percent_bps: int, name: str = "", max_uses=None, valid_until=None,
               internal_note: str = "", actor=None) -> Coupon:
    code = normalize_code(code)
    if not COUPON_CODE.match(code):
        raise LinkError("A code is 1 to 40 letters, digits, hyphens or underscores.")
    if not isinstance(percent_bps, int) or not 1 <= percent_bps <= 9999:
        raise LinkError("The discount is more than 0 % and less than 100 %: a coupon never makes an order free.")
    if max_uses is not None and max_uses < 1:
        raise LinkError("Remaining uses must be at least 1, or left empty.")
    if len(name or "") > 200 or len(internal_note or "") > MAX_NOTE:
        raise LinkError("The name or the note is too long.")
    try:
        with transaction.atomic():
            coupon = Coupon.objects.create(link=link, code=code, percent_bps=percent_bps, name=(name or "").strip() or None,
                                           max_uses=max_uses, valid_until=valid_until,
                                           internal_note=(internal_note or "").strip() or None)
            audit.record(actor, "coupon.create", coupon, {"link": link.pk, "code": code, "percent_bps": percent_bps,
                                                          "max_uses": max_uses})
    except IntegrityError:
        raise LinkError(f"This link already has a coupon {code}.") from None
    return coupon


def set_coupon_active(coupon: Coupon, active: bool, *, actor=None) -> Coupon:
    with transaction.atomic():
        Coupon.objects.filter(pk=coupon.pk).update(active=active)
        audit.record(actor, "coupon.activate" if active else "coupon.deactivate", coupon,
                     {"link": coupon.link_id, "code": coupon.code})
    coupon.active = active
    return coupon


def find_coupon(link: PaymentLink, code: str) -> Coupon | None:
    code = normalize_code(code)
    return Coupon.objects.filter(link=link, code=code).first() if code else None


def coupon_problem(coupon: Coupon | None, now) -> str | None:
    if coupon is None or not coupon.active or (coupon.valid_until is not None and coupon.valid_until <= now):
        return "coupon_invalid"
    if coupon.max_uses is not None and coupon_uses_taken(coupon, now=now) >= coupon.max_uses:
        return "coupon_used_up"
    return None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def set_image(link: PaymentLink, upload, *, actor=None) -> LinkImage:
    """Store `upload` (a PNG, JPEG or WebP of at most 1.5 MB) re-encoded by Pillow: every metadata block
    (EXIF, GPS, ICC, comments) is dropped, so only pixels reach the page. Never fetched from a URL."""
    from PIL import Image, UnidentifiedImageError

    if getattr(upload, "size", None) is not None and upload.size > MAX_IMAGE_BYTES:
        raise LinkError("The image may be at most 1.5 MB.")
    raw = upload.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise LinkError("The image may be at most 1.5 MB.")
    try:
        # Opening reads the header only. A small file can declare a huge picture, so its size is judged
        # here, before anything is decoded; Pillow's own bomb check (which raises at twice its limit) is
        # brought down to ours for the process at start-up (`LinksConfig.ready`), so a larger
        # declaration fails inside open() itself.
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            width, height = probe.size
            if fmt not in IMAGE_FORMATS:
                raise LinkError("The image must be a PNG, JPEG or WebP file.")
            if width * height > MAX_IMAGE_PIXELS:
                raise LinkError("The image is too large: at most 40 megapixels.")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            mode = "RGBA" if fmt != "JPEG" and ("A" in img.getbands() or img.mode == "P") else "RGB"
            clean = img.convert(mode)
            clean.info = {}  # no ICC profile, text chunk, EXIF or comment is carried into the new file
            out = io.BytesIO()
            if fmt == "JPEG":
                clean.save(out, "JPEG", quality=88, optimize=True)
            elif fmt == "WEBP":
                clean.save(out, "WEBP", quality=88)
            else:
                clean.save(out, "PNG", optimize=True)
            width, height = clean.size
    except LinkError:
        raise
    except Image.DecompressionBombError:
        raise LinkError("The image is too large: at most 40 megapixels.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise LinkError("The file is not an image this page can use (PNG, JPEG or WebP).") from None
    data = base64.b64encode(out.getvalue()).decode("ascii")
    with transaction.atomic():
        LinkImage.objects.filter(link=link).delete()
        image = LinkImage.objects.create(link=link, content_type=IMAGE_FORMATS[fmt], data=data, width=width,
                                         height=height)
        audit.record(actor, "link.image", link, {"content_type": image.content_type, "width": width, "height": height})
    return image


def remove_image(link: PaymentLink, *, actor=None) -> None:
    with transaction.atomic():
        if LinkImage.objects.filter(link=link).delete()[0]:
            audit.record(actor, "link.image_removed", link, {"link": link.pk})


def image_uri(link: PaymentLink) -> str:
    image = LinkImage.objects.filter(link=link).first()
    return f"data:{image.content_type};base64,{image.data}" if image is not None and image.data else ""


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
def _alert(kind: str, link: PaymentLink, *, invoice=None, coupon=None, issuer=None) -> None:
    """One staff item. A link that cannot be paid at all is one open item, however many buyers try."""
    if kind in ("no_methods", "no_us_issuer") and LinkAlert.objects.filter(
            kind=kind, link=link, issuer=issuer, resolved_at__isnull=True).exists():
        return
    try:
        with transaction.atomic():
            alert = LinkAlert.objects.create(kind=kind, link=link, invoice=invoice, coupon=coupon, issuer=issuer)
    except IntegrityError:
        return  # the same item was raised meanwhile
    audit.record_system("link_alert.raise", alert, {"kind": kind, "link": link.pk,
                                                    "invoice": getattr(invoice, "pk", invoice),
                                                    "coupon": getattr(coupon, "pk", coupon)})


def _check_amount(link: PaymentLink, order: Order) -> int:
    if not link.customer_chooses:
        return link.price_minor
    amount = order.amount_minor
    if amount is None or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise CheckoutError("amount_low")
    if link.min_minor is not None and amount < link.min_minor:
        raise CheckoutError("amount_low")
    if link.max_minor is not None and amount > link.max_minor:
        raise CheckoutError("amount_high")
    if amount > MAX_PRICE_MINOR:
        raise CheckoutError("amount_high")
    return amount


def order_line(link: PaymentLink, unit_minor: int, quantity: int, coupon: Coupon | None) -> dict:
    return {"name": link.title, "quantity": quantity, "unit_price_minor": unit_minor,
            "tax_rate_bps": link.tax_rate_bps, "discount_rate_bps": coupon.percent_bps if coupon else 0}


def line_total(line: dict) -> int:
    return money.totals_of([SimpleNamespace(discount_amount_minor=0, **line)]).total


def _methods_for(link: PaymentLink, issuer) -> tuple[list[str], list]:
    if link.methods == "custom":
        return method_types.resolve(issuer, link.method_types, link.currency)
    return default_methods(issuer, link.currency), []


def _address(parts: dict | None) -> dict | None:
    if not parts:
        return None
    clean = {k: str(v).strip() for k, v in parts.items() if v and str(v).strip()}
    return clean or None


def checkout(link: PaymentLink, order: Order, *, language: str = "en") -> tuple[Invoice, str]:
    """Make and send the order; returns (invoice, the pay token). CheckoutError when refused, and then
    nothing was made (a link that cannot be paid raises one staff item, outside the transaction)."""
    try:
        return _checkout(link, order, language)
    except _Refused as refused:
        _alert(refused.kind, link, issuer=refused.issuer)
        raise CheckoutError(refused.kind) from None
    except InvoiceError as exc:  # a rule of the invoice services the checks above did not foresee
        logger.warning("checkout on payment link #%s refused by the invoice services (%s)", link.pk,
                       type(exc).__name__)
        raise CheckoutError("total") from None


class _Refused(Exception):
    def __init__(self, kind, issuer=None):
        super().__init__(kind)
        self.kind, self.issuer = kind, issuer


def _checkout(link: PaymentLink, order: Order, language: str) -> tuple[Invoice, str]:
    email = (order.email or "").strip()
    if not email or order.us_tax_resident not in ("yes", "no"):
        raise CheckoutError("total")
    quantity = order.quantity
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= link.quantity_cap:
        raise CheckoutError("quantity")
    if link.customer_chooses and quantity != 1:
        raise CheckoutError("quantity")
    with transaction.atomic():
        link = PaymentLink.objects.select_for_update().select_related("org").get(pk=link.pk)  # holds are counted under it
        now = timezone.now()
        if unavailable_reason(link, now=now):
            raise CheckoutError("unavailable")
        unit = _check_amount(link, order)
        coupon = None
        if (order.coupon_code or "").strip():
            coupon = Coupon.objects.select_for_update().filter(link=link, code=normalize_code(order.coupon_code)).first()
            problem = coupon_problem(coupon, now)
            if problem:
                raise CheckoutError(problem)
        line = order_line(link, unit, quantity, coupon)
        total = line_total(line)
        if total <= 0 or total > MAX_PRICE_MINOR:
            raise CheckoutError("total")

        org = link.org
        contact = find_contact(org, email)
        says_yes = order.us_tax_resident == "yes"
        stored = contact.us_tax_resident if contact is not None else None
        try:
            issuer, _by_rule = resolve_issuer(org, contact or Contact(org=org, us_tax_resident="no"),
                                              typed_answer=order.us_tax_resident)
        except InvoiceError:
            raise _Refused("no_us_issuer") from None
        offered, pairs = _methods_for(link, issuer)
        probe = Invoice(org=issuer, offered_methods=offered)
        if not _live_methods(probe):
            raise _Refused("no_methods", issuer)

        name = (order.name or "").strip()
        billing = _address(order.billing)
        if contact is None:
            # A link buyer: a contact of this checkout's own, not a client of record (audit SEC-1). Never a contact
            # another checkout made, found by no API or staff lookup, and paying does not change that (F2).
            fields = {"legal_name": name or None, "address": billing, "phone": (order.phone or "").strip() or None,
                      "preferred_language": language, "us_tax_resident": order.us_tax_resident}
            contact = create_unrecorded_contact(org, email, audit_detail={"via": "payment_link", "link": link.pk},
                                                **fields)
        invoice, _new = create_invoice(
            org, contact, currency=link.currency, lines=[line], kind=link.kind, title=link.title,
            offered_methods=offered, offered_pairs=pairs, typed_us_answer=order.us_tax_resident, payment_link=link,
            coupon=coupon,
        )
        invoice, _view, pay = send_invoice(invoice, email_client=False, keep_links=True,
                                           buyer={"name": name, "address": billing})
        answers = {
            "shipping": _address(order.shipping), "phone": (order.phone or "").strip() or None,
            "custom": [{"label": label, "value": value[:MAX_CUSTOM_ANSWER]} for label, value in order.custom if value],
        }
        LinkOrder.objects.create(invoice=invoice, buyer_contact=contact, email=email, language=language,
                                 quantity=quantity,
                                 answers={k: v for k, v in answers.items() if v} or None,
                                 us_answer=order.us_tax_resident, hold="reserved",
                                 reserved_until=now + datetime.timedelta(minutes=HOLD_MINUTES))
        if stored is not None and ((says_yes and stored != "yes") or (not says_yes and stored == "yes")):
            _alert("us_mismatch", link, invoice=invoice)
    return invoice, pay


# ---------------------------------------------------------------------------
# After checkout
# ---------------------------------------------------------------------------
def order_paid(invoice) -> None:
    """The order was settled (`apply_payments`, holding the invoice lock; possibly sealed: plaintext
    only). Its hold becomes a use. A hold that had lapsed is honoured at the quoted price, and when that
    takes a capped link or coupon past its cap, staff get one item per cap. Staff are mailed when the
    link asks. Paying changes no contact: a link buyer stays one until a person promotes it (review S1, F2)."""
    order = (LinkOrder.objects.select_for_update().only("pk", "hold", "reserved_until", "invoice_id")
             .filter(invoice_id=invoice.pk).first())
    if order is None or order.hold == "used":
        return
    now = timezone.now()
    held = order.hold == "reserved" and order.reserved_until > now
    link = PaymentLink.objects.select_for_update().only("pk", "max_uses", "notify_staff").get(pk=invoice.payment_link_id)
    coupon = (Coupon.objects.select_for_update().only("pk", "max_uses", "link_id").get(pk=invoice.coupon_id)
              if invoice.coupon_id else None)
    over = []
    if not held:
        if link.max_uses is not None and uses_taken(link, now=now, exclude_order=order.pk) >= link.max_uses:
            over.append(None)
        if coupon is not None and coupon.max_uses is not None and \
                coupon_uses_taken(coupon, now=now, exclude_order=order.pk) >= coupon.max_uses:
            over.append(coupon.pk)
    LinkOrder.objects.filter(pk=order.pk).update(hold="used")
    audit.record_system("link_order.paid", order, {"invoice": invoice.pk, "link": link.pk, "hold_lapsed": not held})
    for coupon_pk in over:
        alert = LinkAlert.objects.create(kind="over_cap", link_id=link.pk, coupon_id=coupon_pk, invoice_id=invoice.pk)
        audit.record_system("link_alert.raise", alert, {"kind": "over_cap", "link": link.pk, "invoice": invoice.pk,
                                                        "coupon": coupon_pk})
    if link.notify_staff and settings.STAFF_NOTIFY_EMAILS:
        from apps.notify import services as notify

        notify.enqueue_addressed_later("order_paid_staff", recipient="staff", invoice=invoice,
                                       plain_context={"link_id": link.pk, "invoice_id": invoice.pk})


HELD_FOR_US_REVIEW = ("This order waits on its US tax residency item: the buyer answered that they are not a US "
                      "tax resident, but the contact is stored as one. Before fulfilling, check with the buyer. If "
                      "they are a US tax resident, refund the payment and send them a new invoice from the "
                      "US-resident issuer; otherwise resolve the item on Needs a person with a note.")


def held_for_us_review() -> Q:
    """The orders (a `LinkOrder` filter) not to fulfil yet: a typed "no" against a stored "yes", whose
    us_mismatch item no person has resolved (2.3a item 3, revised 2026-11-04). Plaintext only."""
    waiting = LinkAlert.objects.filter(kind="us_mismatch", resolved_at__isnull=True).values("invoice_id")
    return Q(us_answer="no", invoice_id__in=waiting)


def to_fulfil(orders):
    """Of `orders`, the paid ones a person should fulfil now: not fulfilled, and not held for review."""
    return orders.filter(invoice__status__in=SETTLED, fulfilled_at__isnull=True).exclude(held_for_us_review())


def mark_fulfilled(order: LinkOrder, user) -> LinkOrder:
    with transaction.atomic():
        fresh = LinkOrder.objects.select_for_update().select_related("invoice").get(pk=order.pk)
        if fresh.invoice.status not in SETTLED:
            raise LinkError("Only a paid order can be marked fulfilled.")
        if fresh.fulfilled_at is not None:
            raise LinkError("This order was already marked fulfilled.")
        if LinkOrder.objects.filter(held_for_us_review(), pk=fresh.pk).exists():
            raise LinkError(HELD_FOR_US_REVIEW)
        fresh.fulfilled_at, fresh.fulfilled_by = timezone.now(), user
        fresh.save(update_fields=["fulfilled_at", "fulfilled_by"])
        audit.record(user, "link_order.fulfil", fresh, {"invoice": fresh.invoice_id})
    return fresh


def resolve_alert(alert: LinkAlert, user, note: str = "") -> LinkAlert:
    """Close a staff item, with what the person checked (`note`, optional, kept in the audit entry)."""
    note = (note or "").strip()[:MAX_NOTE]
    with transaction.atomic():
        if not LinkAlert.objects.filter(pk=alert.pk, resolved_at__isnull=True).update(
                resolved_at=timezone.now(), resolved_by=user):
            raise LinkError("This item was already resolved.")
        detail = {"kind": alert.kind, "link": alert.link_id}
        if note:
            detail["note"] = note
        audit.record(user, "link_alert.resolve", alert, detail)
    alert.refresh_from_db()
    return alert


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------
def run_jobs(now=None) -> dict:
    """The `link_orders` worker job: lapse holds, void unpaid orders, forget the links of finished ones."""
    now = now or timezone.now()
    return {"lapsed": release_holds(now), "voided": void_unpaid(now), "forgotten": forget_finished_links()}


def release_holds(now) -> int:
    """Holds that ran out unpaid, or whose order was voided first, free their use (plaintext: sealed-safe)."""
    lapsed = LinkOrder.objects.filter(hold="reserved").filter(
        Q(reserved_until__lte=now) | Q(invoice__status="void")).exclude(invoice__status__in=SETTLED)
    return LinkOrder.objects.filter(pk__in=list(lapsed.values_list("pk", flat=True)), hold="reserved").update(hold="lapsed")


def _could_not_look(invoice_pk: int) -> str | None:
    """Why the 7-day void must wait for this order, though nothing says it was paid; None when it need
    not. "Could not look" is not "not paid" (final review I1): a quote of the order still holds its
    amount, a watcher of an account and network it was quoted on is failing, or blocks of that account and
    network were not scanned and nobody has acknowledged them (`quotes.services.could_not_look`, the rule
    the API's `money_may_be_coming` reads for a chain quote too). Plaintext only. The reason is a word,
    never an address."""
    from apps.quotes.models import Quote
    from apps.quotes.services import could_not_look

    return could_not_look(Quote.objects.filter(invoice_id=invoice_pk))


def void_unpaid(now) -> int:
    """Orders sent VOID_AFTER_DAYS ago and still unpaid are voided by the system. One a payment may be
    under way for is left: a detected payment, an open quote, a claim, any money confirmed; and one we
    could not look for (`_could_not_look`). A payment that arrives after the void goes to review like any
    payment for a void invoice."""
    if not vault.is_unsealed():
        return 0  # the void and its audit wait for the vault; nothing is lost by waiting
    cutoff = now - datetime.timedelta(days=VOID_AFTER_DAYS)
    candidates = list(Invoice.objects.filter(payment_link__isnull=False, status__in=("sent", "overdue"),
                                             sent_at__lte=cutoff).values_list("pk", flat=True))
    from apps.quotes.models import Quote
    from apps.quotes.services import gateway_money_may_be_coming

    voided = 0
    for pk in candidates:
        invoice = Invoice.objects.only("pk").get(pk=pk)
        if (Quote.objects.filter(invoice_id=pk).filter(Q(state="open") | gateway_money_may_be_coming()).exists()
                or invoice.payments.filter(Q(state__in=("detected", "confirmed"))
                                           | Q(state="needs_review", final=False)).exists()):
            continue
        waiting = _could_not_look(pk)
        if waiting:
            logger.info("link order #%s not voided yet: %s", pk, waiting)
            continue
        try:
            with transaction.atomic():  # no void without its record, and no record of a void that did not happen
                void_invoice(invoice, "", None)
                audit.record_system("invoice.void", invoice,
                                    {"reason": "unpaid_link_order", "after_days": VOID_AFTER_DAYS})
        except InvoiceError as exc:
            logger.info("link order #%s not voided: %s", pk, type(exc).__name__)
            continue
        voided += 1
    return voided


def forget_finished_links() -> int:
    """The encrypted links of an order are kept until its receipt is out (2.3a item 2): a settled order
    whose receipt is no longer waiting forgets them, as does a void one."""
    from apps.notify.models import OutboxMessage

    waiting = OutboxMessage.objects.filter(kind="receipt", state__in=("queued", "failed")).values("invoice_id")
    done = Invoice.objects.filter(payment_link__isnull=False).filter(
        Q(view_token_copy__isnull=False) | Q(pay_token_copy__isnull=False)).filter(
        Q(status="void") | (Q(status__in=SETTLED) & ~Q(pk__in=waiting)))
    return forget_link_copies(done.values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------
def order_recipient(invoice) -> tuple[str, str] | None:
    """(address, language) typed at checkout for a payment link's order, else None. Every letter about
    the order goes there, never to the contact's stored address (a public form never sends what a
    contact stores)."""
    if not getattr(invoice, "payment_link_id", None):
        return None
    order = LinkOrder.objects.filter(invoice_id=invoice.pk).only("pk", "email", "language").first()
    return (order.email, order.language) if order is not None and order.email else None


def buyer_name(invoice) -> str | None:
    """The buyer's name as typed (the snapshot's buyer block) for a payment link's order, else None."""
    if not getattr(invoice, "payment_link_id", None):
        return None
    snap = Invoice.objects.filter(pk=invoice.pk).values_list("snapshot", flat=True).first() or {}
    return (snap.get("buyer") or {}).get("name") or ""


def letter_details(invoice) -> dict | None:
    """For the letters of a link order: the address and language the buyer typed, the buyer's name as
    typed (the snapshot), and the links while they are kept. None for any other invoice."""
    if not invoice.payment_link_id:
        return None
    order = LinkOrder.objects.filter(invoice_id=invoice.pk).only("pk", "email", "language").first()
    if order is None:
        return None
    row = Invoice.objects.filter(pk=invoice.pk).only("pk", "view_token_copy", "pay_token_copy", "snapshot").get()
    view, pay = row.view_token_copy, row.pay_token_copy
    return {
        "to": order.email, "language": order.language,
        "name": ((row.snapshot or {}).get("buyer") or {}).get("name") or "",
        "view_url": invoice.view_url(view) if view else "", "pay_url": invoice.pay_url(pay) if pay else "",
    }
