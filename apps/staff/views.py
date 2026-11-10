"""Staff pages at the site root: password login, then a second factor (a passkey or a TOTP code), then work.

Every page except login/verify needs an authenticated `is_staff` user whose session carries a
verified OTP device (django-otp). All state changes are POSTs behind CSRF. The only script beyond the
interface's own is the passkey ceremony (`static/ui/passkeys.js`, see `views_security`).
"""
import datetime
from decimal import Decimal
import functools
import re

from django import forms
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse as url_reverse
from django.utils import timezone, translation
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import login as otp_login

from apps.audit import services as audit
from apps.audit.models import AuditEntry
from apps.contacts.models import Contact
from apps.invoices.errors import InvoiceError
from apps.invoices import migration as migration_lock
from apps.invoices.migration import staff_note
from apps.invoices.models import Invoice
from apps.invoices.money import line_total, totals
from apps.invoices.pdf import format_money
from apps.contacts.services import apply_typed_fields, effective_us_flag, find_contact, find_or_create_contact
from apps.invoices.currencies import CURRENCIES
from apps.invoices.money import stored_total
from apps.invoices.services import (
    INVOICE_PLAIN_FIELDS, create_invoice, keep_overpayment, lock_invoice, overpaid_needing_a_person, default_methods, primary_address, queue_invoice_letter, reissue_links,
    resolve_issuer, send_invoice, update_draft, void_invoice,
)
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.rails.models import RailAccount
from apps.keys import services as keys
from apps.keys.models import CONFIRM_VALID_FOR, EncryptionKey
from apps.keys.validation import KeyRejected
from apps.keys.views import read_upload, reason_text
from apps.payments.models import REVIEW_REASONS
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.payments.models import Declaration, Payment
from apps.payments.services import (
    DECLARATION_PLAIN_FIELDS, PAYMENT_PLAIN_FIELDS, acknowledge_gateway_invalid, acknowledge_refund, assign_payment,
    awaiting_finality, payment_org_ids,
)
from apps.rails import fx, health
from apps.portal.crypto import decimals_for
from apps.rails.models import FxLegRate, PendingTransfer, ScanGap, WatcherHealth
from apps.rails.networks import NETWORKS, asset_by_contract, explorer_tx_url
from apps.rails.uris import decimal_amount
from apps.payments.services import (
    confirm_payment, confirmed_minor, dismiss_declaration, record_manual_payment, reject_payment, reverse_payment,
)
from apps.vault.fields import UndecryptableValue
from apps.vault.ratelimit import address_bucket, client_ip, rate_limited
from apps.vault.registry import plaintext_columns
from apps.webhooks import delivery as webhook_delivery
from apps.webhooks import events as webhook_events
from apps.webhooks.models import WebhookDelivery

from . import device_cookie, passkeys
from .forms import (
    ADDRESS_PARTS, AssignPaymentForm, InvoiceForm, LineFormSet, LinksForm, NoteForm, SendForm, major_text, OptionalReasonForm, ReasonForm, RecordPaymentForm, RefundNoteForm, RequiredNoteForm, TokenForm,
    parse_minor,
)
from .models import StaffPasskey, StaffTOTPDevice
from .orgnames import org_name, org_names


CLIENT_ROOTS = ("healthz", "theme", "unlock", "i", "p", "k", "l", "api", "hooks", "llms.txt")


def _safe_next(request, default="/"):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    host, secure = {request.get_host()}, request.is_secure()
    if not (nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, allowed_hosts=host, require_https=secure)):
        return default
    if nxt == "/staff" or nxt.startswith(("/staff/", "/staff?")):
        # The old prefix goes first; what is left is collapsed to one slash and checked again, so
        # /staff//evil.example can never become //evil.example.
        nxt = "/" + re.sub(r"^[/\\]+", "", nxt[len("/staff"):])
        if not url_has_allowed_host_and_scheme(nxt, allowed_hosts=host, require_https=secure):
            return default
    return nxt


def _harden(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex"
    return response


def staff_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect(url_reverse("staff:login") + "?" + urlencode({"next": request.get_full_path()}))
        if not (user.is_active and user.is_staff):
            return _harden(HttpResponseForbidden("Staff only."))
        if not user.is_verified():
            return redirect(url_reverse("staff:verify") + "?" + urlencode({"next": request.get_full_path()}))
        try:
            response = view(request, *args, **kwargs)
        except UndecryptableValue:
            # Raised when a row is loaded, so no view can guard the read itself. One page for all of them.
            return _harden(render(request, "staff/unreadable.html", {"path": request.path}, status=409))
        return _harden(response)

    return wrapper


def _login_limited(request):
    # One page for every refusal: it must not tell which accounts exist.
    return _harden(render(request, "staff/login.html", {"form": AuthenticationForm(request), "limited": True},
                          status=429))


def login_view(request):
    if request.method == "POST":
        # Count and refuse BEFORE touching the credentials: a bound form authenticates, and its
        # error text would tell a guesser whether the password was right.
        username = (request.POST.get("username") or "").strip().lower()[:150]
        # The address first, and nothing else once it is refused: a refused address must not go on
        # writing a counter for every name it tries (audit WEB-3).
        if rate_limited("staff-login", address_bucket(client_ip(request)), 10, 300):
            return _login_limited(request)
        # A browser that signed in to this account before (password and TOTP) carries the device
        # cookie: its attempts are counted against the cookie, not the account, so naming the
        # account from elsewhere cannot lock its owner out (audit WEB-2). TOTP still follows.
        device = device_cookie.device_for(request, username)
        if device is not None:
            limited = rate_limited("staff-login-device", device, 10, 300)
        else:
            limited = rate_limited("staff-login-user", username, 10, 300)
        if limited:
            return _login_limited(request)
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect(url_reverse("staff:verify") + "?" + urlencode({"next": _safe_next(request)}))
    else:
        form = AuthenticationForm(request)
    return _harden(render(request, "staff/login.html", {"form": form, "next": _safe_next(request, "")}))


def second_factor_limited(request) -> bool:
    """The second step's two buckets, shared by the code and the passkey, so switching between them
    buys no extra attempts: the address first (by its /64 for IPv6), and nothing else is counted once
    it is refused; then the account."""
    if rate_limited("staff-otp-address", address_bucket(client_ip(request)), 10, 300):
        return True
    return rate_limited("staff-otp", str(request.user.pk), 10, 300)


def complete_second_factor(request, device):
    """The end of a sign-in, by code or by passkey: the django-otp login, the time it happened (for
    changes to the second factors) and the device cookie (audit WEB-2)."""
    otp_login(request, device)
    passkeys.mark_verified(request)
    response = redirect(_safe_next(request))
    device_cookie.issue(response, request.user)
    return response


def verify_page(request, form=None, *, status=200, **extra):
    """The second step. It offers what the account has: "Use a passkey" when it holds one, and the
    code form when it holds an authenticator (or nothing yet, which is how a new account starts)."""
    user = request.user
    has_passkey = StaffPasskey.objects.filter(user=user, confirmed=True).exists()
    has_totp = StaffTOTPDevice.objects.filter(user=user, confirmed=True).exists()
    form = form or TokenForm()
    if has_passkey:  # "Use a passkey" is the primary action: the code field does not take the focus
        form.fields["token"].widget.attrs.pop("autofocus", None)
    context = {"form": form, "next": _safe_next(request, ""), "has_passkey": has_passkey,
               "show_code": has_totp or not has_passkey, "again": user.is_verified(), **extra}
    return _harden(render(request, "staff/verify.html", context, status=status))


def verify_view(request):
    user = request.user
    if not user.is_authenticated:
        return redirect("staff:login")
    if not (user.is_active and user.is_staff):
        return _harden(HttpResponseForbidden("Staff only."))
    form = TokenForm(request.POST or None)
    if request.method == "POST":
        if second_factor_limited(request):
            return verify_page(request, form, limited=True, status=429)
        if form.is_valid():
            token = form.cleaned_data["token"].strip().replace(" ", "")
            verified = None
            # Only the encrypted devices: a plaintext django-otp TOTPDevice is never accepted. Locked
            # while verifying, as django-otp does, so its throttling holds against parallel guesses.
            with transaction.atomic():
                for device in StaffTOTPDevice.objects.devices_for_user(user, confirmed=True).select_for_update():
                    if device.verify_token(token):
                        verified = device
                        break
            if verified is not None:
                return complete_second_factor(request, verified)
            form.add_error("token", "That code was not accepted.")
    return verify_page(request, form)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("staff:login")


@staff_required
def needs_person(request):
    return render(request, "staff/needs_person.html", _needs_person_context())


NEEDS_PERSON_CAP = 200  # rows per section: the page must load however long a queue grows
GATEWAY_STUCK_AFTER = datetime.timedelta(hours=6)  # a gateway payment seen but unsettled this long is listed
KEY_EXPIRY_WARNING = datetime.timedelta(days=30)  # an active encryption key expiring this soon is listed
_PAYMENT_ROW = (*PAYMENT_PLAIN_FIELDS, "invoice__number", "invoice__status")  # never an encrypted column
_PENDING_ROW = (*plaintext_columns(PendingTransfer), "account__label")
_WEBHOOK_ROW = (*plaintext_columns(WebhookDelivery), "event__type", "endpoint__description", "endpoint__org__slug")


class Section:
    """The first `NEEDS_PERSON_CAP` rows of a queue, and how many it holds in all."""

    def __init__(self, queryset, decorate=None):
        self.total = queryset.count()
        self.rows = list(queryset[:NEEDS_PERSON_CAP]) if self.total else []
        if decorate is not None:
            for row in self.rows:
                decorate(row)

    @property
    def capped(self):
        return self.total > len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __bool__(self):
        return bool(self.rows)


def _in_asset(base_units: str, decimals: int | None, symbol: str) -> str:
    """A digit string of base units, shown in whole units of the asset where its decimals are known."""
    if decimals is None or not base_units.isdigit():
        return f"{base_units} base units {symbol}".rstrip()
    return f"{decimal_amount(int(base_units), decimals)} {symbol}".rstrip()


def _payment_decimals(p) -> int | None:
    if p.network in NETWORKS or p.rail in ("btcpay", "lightning"):
        return decimals_for(p.network, p.asset)
    return None


def _decorate_payment(p) -> None:
    """What the page shows beside a payment: the reason as its sentence, a link to the
    transaction (none on a network we do not list), the refund in the asset, and whether a
    person may confirm it yet."""
    reason = REVIEW_REASONS.get(p.review_reason)
    p.reason_text = str(reason) if reason else p.review_reason
    p.explorer_url = explorer_tx_url(p.network, p.tx_hash) if p.network in NETWORKS and p.tx_hash else ""
    p.refund_text = _in_asset(p.refund_due_base_units, _payment_decimals(p), p.asset) if p.refund_due_base_units else ""
    p.awaiting_finality = awaiting_finality(p)


def _decorate_pending(t) -> None:
    net = NETWORKS.get(t.network)
    spec = None
    if net is not None:
        spec = asset_by_contract(t.network, t.contract) if t.contract else next(
            (a for a in net.assets.values() if not a.contract), None)
    t.amount_text = _in_asset(t.amount_base_units, spec.decimals if spec else None, spec.symbol if spec else "")
    t.explorer_url = explorer_tx_url(t.network, t.tx_hash) if net is not None and t.tx_hash else ""


def watcher_words(name: str) -> str:
    """A watcher as staff read it: "Ethereum, transfers to “Stablecoins”" for `scan:evm:ethereum:1`.
    The raw name stays beside it on the page, for searching the logs."""
    from apps.portal.crypto import network_name

    parts = name.split(":")
    if parts[0] in ("scan", "ingest") and len(parts) == 4:
        _kind, _family, network, pk = parts
        label = (RailAccount.objects.filter(pk=int(pk)).values_list("label", flat=True).first()
                 if pk.isascii() and pk.isdigit() else None)
        target = f"transfers to “{label}”" if label else f"transfers to wallet #{pk}"
        return f"{network_name(network)}, {'recording ' if parts[0] == 'ingest' else ''}{target}"
    if parts[0] in ("btcpay", "lightning") and len(parts) == 2:
        label = (RailAccount.objects.filter(pk=int(parts[1])).values_list("label", flat=True).first()
                 if parts[1].isascii() and parts[1].isdigit() else None)
        what = "BTCPay" if parts[0] == "btcpay" else "Lightning node"
        return f"{what}, “{label}”" if label else f"{what} #{parts[1]}"
    if parts[0] == "amounts" and len(parts) == 4:
        _prefix, _family, unit, pk = parts
        label = (RailAccount.objects.filter(pk=int(pk)).values_list("label", flat=True).first()
                 if pk.isascii() and pk.isdigit() else None)
        what = "USD stablecoins" if unit == "USD" else unit
        return f"Unique amounts for {what} on “{label}”" if label else f"Unique amounts for {what} on wallet #{pk}"
    if parts[0] == "finality" and len(parts) == 2:
        return f"{network_name(parts[1])}, finality checks"
    if parts[0] == "declarations" and len(parts) == 2:
        return f"{network_name(parts[1])}, checks of declared transactions"
    if name == "ingest-retry":
        return "Recording transfers that were seen"
    return name


def _decorate_watcher(w) -> None:
    """What the failure means when it is not "payments are not being seen" (`health.explanation`),
    and the watcher in words."""
    w.explanation = health.explanation(w.name, w.last_error_kind) or ""
    w.words = watcher_words(w.name)


def _decorate_fx_leg(w) -> None:
    """A refused exchange-rate leg: which currency's quotes are refused, why, and the refused rate a
    person may accept (a jump only: a stale answer is waited out, never accepted)."""
    base, quote = fx.leg_of(w.name) or ("", w.name)
    w.currency = quote
    w.explanation = fx.leg_explanation(w.name, w.last_error_kind)
    w.leg = FxLegRate.objects.filter(base=base, quote=quote).first()
    w.can_accept = w.last_error_kind == "jump" and w.leg is not None and bool(w.leg.pending_rate)


def _decorate_key(k) -> None:
    """The fingerprint, read for this row alone (the list loads plaintext columns only), so one
    value the vault cannot open shows as such instead of taking the page down."""
    try:
        k.fingerprint_text = k.fingerprint
    except UndecryptableValue:
        k.fingerprint_text = "(cannot be decrypted)"


def _link_alert_rows():
    from apps.links.models import LinkAlert

    return (LinkAlert.objects.filter(resolved_at__isnull=True).select_related("invoice", "coupon")
            .only("pk", "kind", "link_id", "coupon_id", "coupon__code", "invoice_id", "invoice__number",
                  "invoice__status", "issuer_id", "created_at").order_by("created_at", "pk"))


def _decorate_link_alert(a) -> None:
    """What the item means, in a sentence (plaintext columns only)."""
    from apps.links.models import LinkOrder

    if a.kind == "us_mismatch":
        said = LinkOrder.objects.filter(invoice_id=a.invoice_id).values_list("us_answer", flat=True).first()
        a.text = ("Buyer says US tax resident, contact says not. The order went to the US-resident issuer, as "
                  "the buyer answered; nothing stored was changed. Check the contact's flag." if said == "yes" else
                  # Residual R5: a paid order cannot be voided, so the item says what can be done.
                  "The buyer answered that they are not a US tax resident, but this contact is stored as one. "
                  "Before fulfilling, check with the buyer. If they are a US tax resident, refund the payment and "
                  "send them a new invoice from the US-resident issuer; otherwise resolve this item with a note.")
    elif a.kind == "over_cap":
        a.text = (f"A payment that arrived after its 60-minute hold took coupon {a.coupon.code} past its remaining "
                  "uses. The order kept its price." if a.coupon_id else
                  "A payment that arrived after its 60-minute hold took this link past its total uses. The order "
                  "kept its price.")
    elif a.kind == "no_methods":
        a.text = (f"{org_name(Organization.objects.only('pk', 'slug', 'legal_name').get(pk=a.issuer_id))} offers none of "
                  "this link's payment methods, so buyers are told it cannot be paid right now. Add a method to "
                  "that organization or change the link's set.") if a.issuer_id else "No payment method to offer."
    else:
        a.text = ("A US tax resident tried to check out, and no organization is marked as the US-resident issuer. "
                  "Nothing was made.")


def _open_us_mismatch_invoices():
    from apps.links.models import LinkAlert

    return LinkAlert.objects.filter(kind="us_mismatch", resolved_at__isnull=True,
                                    invoice__isnull=False).values("invoice_id")


def _key_rows(queryset):
    return queryset.only(*plaintext_columns(EncryptionKey))


def _payment_rows(queryset):
    return queryset.select_related("invoice").only(*_PAYMENT_ROW).order_by("detected_at", "pk")


# Listed, but not counted as open: address-poisoning spam, collapsed behind a count.
UNCOUNTED = ("dust",)


def _queues():
    """Every queue on Needs a person, in page order, as (name, queryset, decorate). Every queryset
    loads plaintext columns only, so one value the key cannot open never takes the page down: this
    is where staff are sent when something is wrong. The page and the navigation's open count both
    read from here, so they cannot disagree."""
    in_review = Payment.objects.filter(state="needs_review")
    failing = WatcherHealth.objects.filter(failing_since__isnull=False).order_by("failing_since", "name")
    # Recipients and contents stay encrypted and unloaded: kind, state and timing are enough.
    stale = timezone.now() - datetime.timedelta(hours=1)
    return [
        ("declarations", Declaration.objects.filter(state="open").select_related("invoice")
         .only(*DECLARATION_PLAIN_FIELDS, "invoice__number", "invoice__status").order_by("created_at", "pk"), None),
        ("reviews", _payment_rows(in_review.exclude(review_reason="dust")), _decorate_payment),
        # Address-poisoning spam: listed, but collapsed behind a count (nobody is mailed about it).
        ("dust", _payment_rows(in_review.filter(review_reason="dust")), _decorate_payment),
        # XMR/ZEC, and money recorded by hand: nothing watches for it, so a person says it arrived.
        ("awaiting", _payment_rows(
            Payment.objects.filter(state="detected")
            .filter(Q(needs_staff_confirmation=True) | Q(rail__in=("bank", "fiat_link", "manual")))), _decorate_payment),
        # A BTCPay or Lightning payment the gateway saw hours ago and has not settled: the poller
        # keeps asking, but a person should look at the gateway (or confirm it from the wallet).
        # Other detected payments are waiting on the chain, not on a person.
        ("stuck_gateway", _payment_rows(
            Payment.objects.filter(state="detected", rail__in=("btcpay", "lightning"), needs_staff_confirmation=False,
                                   detected_at__lt=timezone.now() - GATEWAY_STUCK_AFTER)), _decorate_payment),
        ("refunds", _payment_rows(Payment.objects.exclude(refund_due_base_units="")
                                  .filter(refund_acknowledged_at__isnull=True)), _decorate_payment),
        # Confirmed, then called invalid by the gateway (a double spend, a reorg): still counted until a
        # person reverses it from the invoice or keeps it here (audit MONEY-4).
        ("invalidated", _payment_rows(Payment.objects.filter(state="confirmed", gateway_invalid_at__isnull=False,
                                                             gateway_invalid_acknowledged_at__isnull=True)),
         _decorate_payment),
        # An exchange-rate leg is not a watcher that cannot look: quotes in its currency are refused.
        ("fx_legs", failing.filter(name__startswith=fx.LEG_PREFIX), _decorate_fx_leg),
        ("watchers", failing.exclude(name__startswith=fx.LEG_PREFIX), _decorate_watcher),
        ("pending", PendingTransfer.objects.select_related("account").only(*_PENDING_ROW).order_by("pk"),
         _decorate_pending),
        ("gaps", ScanGap.objects.filter(acknowledged_at__isnull=True).order_by("pk"), None),
        # Kept overpayments leave the queue until the confirmed sum moves again (owner, 2026-11-09).
        ("overpaid", overpaid_needing_a_person().only(*INVOICE_PLAIN_FIELDS).order_by("-paid_at", "pk"), None),
        # The contact became a US tax resident after these were made: void and reissue (never silently).
        # A payment link's order with an open US mismatch item is listed there instead (one item per order).
        ("wrong_issuer", Invoice.objects.filter(contact__us_tax_resident="yes", org__is_us_resident_issuer=False,
                                                status__in=("draft", "sent", "overdue", "declared", "partially_paid"))
         .exclude(pk__in=_open_us_mismatch_invoices())
         .select_related("org").only(*INVOICE_PLAIN_FIELDS, "org__slug").order_by("pk"), None),
        # Keys that stopped working at send time. Once the contact has a new active key the failed
        # one no longer matters; until then their mail is refused rather than sent unencrypted.
        ("failed_keys", _key_rows(EncryptionKey.objects.filter(state="failed")
                                  .exclude(contact__encryption_keys__state="active").order_by("pk")), _decorate_key),
        ("expiring_keys", _key_rows(EncryptionKey.objects.filter(
            state="active", expires_at__isnull=False, expires_at__lte=timezone.now() + KEY_EXPIRY_WARNING,
        ).order_by("expires_at", "pk")), _decorate_key),
        ("undelivered", OutboxMessage.objects.filter(Q(state="failed") | Q(state="queued", created_at__lt=stale))
         .select_related("invoice").only(*notify.PLAINTEXT_FIELDS, "invoice__number")
         .order_by("created_at", "pk"), None),
        # Webhook deliveries that failed for good, or still retrying after an hour. The endpoint's URL
        # and secret are encrypted and stay unloaded: the event type, endpoint and failure kind are enough.
        # Payment links: a buyer's US answer the contact contradicts, a late payment past a cap, a link its
        # issuer cannot be paid through. Ids only; the sentence says what each means.
        ("link_alerts", _link_alert_rows(), _decorate_link_alert),
        ("undelivered_webhooks", WebhookDelivery.objects.filter(
            Q(state="failed") | Q(state="queued", attempts__gt=0, created_at__lt=stale))
         .select_related("event", "endpoint__org").only(*_WEBHOOK_ROW).order_by("created_at", "pk"), None),
    ]


def _needs_person_context(error=None):
    sections = {name: Section(queryset, decorate) for name, queryset, decorate in _queues()}
    counted = [section for name, section in sections.items() if name not in UNCOUNTED]
    open_count = sum(section.total for section in counted)
    from .context import fresh_counts

    return {
        **sections,
        "open_count": open_count, "nav_counts": fresh_counts(open_count),
        "clear_count": sum(1 for section in counted if not section),
        "error": error, "review_reasons": REVIEW_REASONS, "org_names": org_names(),
    }


def needs_person_count() -> int:
    """What the navigation shows beside Needs a person: the rows waiting in its queues."""
    return sum(queryset.count() for name, queryset, _ in _queues() if name not in UNCOUNTED)


@require_http_methods(["GET", "POST"])
@staff_required
def contact_keys(request, pk):
    """A contact's encryption keys. A key uploaded here is active at once (staff vouch for it);
    the page never shows key material."""
    contact = get_object_or_404(Contact.objects.select_related("org"), pk=pk)
    if request.method == "GET":
        return _keys_page(request, contact)
    data = read_upload(request)
    if data is None:
        return _keys_page(request, contact, "Paste a key or choose a file.", 400)
    try:
        keys.add_key(contact, data, source="staff", actor=request.user)
    except KeyRejected as exc:
        with translation.override("en"):
            text = reason_text(exc.code)
        return _keys_page(request, contact, f"Key refused ({exc.code}): {text}", 400)
    except keys.KeyCheckUnavailable:
        return _keys_page(request, contact, "Could not check the key right now: gpg failed on this server. Nothing "
                                            "was stored; try again in a few minutes.", 503)
    return redirect("staff:contact_keys", pk=contact.pk)


def _keys_page(request, contact, error=None, status=200):
    """Capped like every list here. Pending keys whose link expired can never be confirmed: they are
    collapsed under a count instead of crowding out the keys that matter."""
    rows = (EncryptionKey.objects.filter(contact=contact).only(*plaintext_columns(EncryptionKey))
            .order_by("-created_at", "-pk"))
    dead = Q(state="pending") & (Q(confirm_sent_at__isnull=True)
                                 | Q(confirm_sent_at__lte=timezone.now() - CONFIRM_VALID_FOR))
    addresses = [e.address for e in contact.emails.order_by("-primary", "pk")]
    return render(request, "staff/contact_keys.html", {
        "contact": contact, "keys": Section(rows.exclude(dead), _decorate_contact_key),
        "expired_pending": Section(rows.filter(dead), _decorate_contact_key),
        "addresses": addresses, "error": error, "org_name": org_name(contact.org),
    }, status=status)


def _decorate_contact_key(k) -> None:
    try:
        k.fingerprint_text = keys.format_fingerprint(k.fingerprint)
    except UndecryptableValue:
        k.fingerprint_text = "(cannot be decrypted)"


@require_POST
@staff_required
def revoke_key(request, pk):
    key = get_object_or_404(EncryptionKey.objects.only(*plaintext_columns(EncryptionKey)), pk=pk)
    keys.revoke_key(key, request.user)
    return redirect("staff:contact_keys", pk=key.contact_id)


@require_POST
@staff_required
def retry_outbox(request, pk):
    message = get_object_or_404(OutboxMessage.objects.only("pk"), pk=pk)
    notify.retry_now(OutboxMessage.objects.filter(pk=message.pk), actor=request.user)
    messages.success(request, "Asked for another attempt.")
    return redirect(_safe_next(request, url_reverse("staff:needs_person")))


@require_POST
@staff_required
def retry_webhook(request, pk):
    row = get_object_or_404(WebhookDelivery.objects.only("pk"), pk=pk)
    webhook_delivery.retry_now(WebhookDelivery.objects.filter(pk=row.pk), actor=request.user)
    messages.success(request, "Asked for another attempt.")
    return redirect(_safe_next(request, url_reverse("staff:needs_person")))


@require_POST
@staff_required
def dismiss_outbox(request, pk):
    """Close a failed e-mail for good (it is never sent afterwards), with the reason, audited."""
    message = get_object_or_404(OutboxMessage.objects.only("pk"), pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A reason is required to dismiss a message.", 400)
    if not notify.dismiss(message, request.user, form.cleaned_data["reason"]):
        return _person_error(request, "Only a failed message can be dismissed, and this one is not failed.", 409)
    messages.success(request, "Dismissed the message.")
    return redirect(_safe_next(request, url_reverse("staff:needs_person")))


@require_POST
@staff_required
def dismiss_webhook(request, pk):
    """Close a failed webhook delivery for good, with the reason, audited."""
    row = get_object_or_404(WebhookDelivery.objects.only("pk"), pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A reason is required to dismiss a delivery.", 400)
    if not webhook_delivery.dismiss(row, request.user, form.cleaned_data["reason"]):
        return _person_error(request, "Only a failed delivery can be dismissed, and this one is not failed.", 409)
    messages.success(request, "Dismissed the delivery.")
    return redirect(_safe_next(request, url_reverse("staff:needs_person")))


def _locked_if_migrated(request, invoice, person=False):
    """A migrated invoice is history: its actions answer 409 and change nothing. Fails closed when the
    migration data cannot be read. `invoice` may be an Invoice or an id; `person` answers on the
    "needs a person" page (for actions that start there) instead of the invoice page."""
    invoice_id = getattr(invoice, "pk", invoice)
    if invoice_id is None:
        return None
    reason = migration_lock.refusal(invoice_id)
    if reason is None:
        return None
    if person:
        return _person_error(request, reason, 409)
    ctx = _detail_context(invoice if hasattr(invoice, "pk") else _invoice(invoice_id))
    ctx["error"] = reason
    return render(request, "staff/invoice_detail.html", ctx, status=409)


def _safe_source_url(url) -> str:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _detail_context(invoice, form=None, void_form=None):
    t = totals(invoice)
    info, unreadable = migration_lock.read_state(invoice.pk)
    migrated = info is not None or unreadable is not None
    paid = confirmed = confirmed_minor(invoice)
    payments = invoice.payments.all()
    audit_entries = AuditEntry.objects.filter(model_label="invoices.invoice", object_pk=str(invoice.pk)) | \
        AuditEntry.objects.filter(model_label="payments.payment", object_pk__in=[str(p.pk) for p in payments]) | \
        AuditEntry.objects.filter(model_label="payments.declaration",
                                  object_pk__in=[str(d.pk) for d in invoice.declarations.all()])
    return {
        "invoice": invoice,
        "org_name": org_name(invoice.org), "requested_by_name": org_name(invoice.requested_by_org),
        "lines": [(l, format_money(line_total(l), invoice.currency)) for l in invoice.lines.all()],
        "total": format_money(t.total, invoice.currency),
        "paid": format_money(paid, invoice.currency),
        "remaining": format_money(max(0, t.total - paid), invoice.currency),
        "payments": [(p, format_money(p.amount_minor, invoice.currency), awaiting_finality(p)) for p in payments],
        "declarations": invoice.declarations.all(),
        "audit": audit_entries.order_by("-created_at", "-pk"),
        "migration": ({"note": staff_note(info), "status": info.get("source_status") or "",
                       "url": _safe_source_url(info.get("source_url"))} if info is not None else None),
        "migration_unreadable": unreadable is not None,
        "can_pay": not migrated and invoice.status not in ("draft", "void"),
        # the same rule void_invoice enforces: no void while confirmed money stands
        "can_void": not migrated and invoice.status in ("sent", "overdue", "declared") and confirmed == 0,
        "is_draft": invoice.status == "draft",
        # money over the total that nobody has decided about yet (refund it, or keep it here)
        "overpayment_open": not migrated and invoice.status == "overpaid" and invoice.overpayment_kept_minor != confirmed,
        "overpayment_excess": format_money(max(0, confirmed - t.total), invoice.currency),
        "overpayment_kept": invoice.status == "overpaid" and invoice.overpayment_kept_minor == confirmed,
        "can_reissue": not migrated and invoice.status not in ("draft", "void"),
        "send_form": SendForm(initial={"email_client": True}),
        "form": form or RecordPaymentForm(currency=invoice.currency),
        "void_form": void_form or ReasonForm(),
        **_link_order_context(invoice),
    }


def _link_order_context(invoice) -> dict:
    """An order made through a payment link: the link, its coupon, and whether it waits to be fulfilled."""
    if not invoice.payment_link_id:
        return {}
    from apps.links.models import LinkOrder, PaymentLink

    order = LinkOrder.objects.filter(invoice_id=invoice.pk).select_related("fulfilled_by").first()
    link = PaymentLink.objects.only("pk", "fulfillment").get(pk=invoice.payment_link_id)
    from apps.invoices.snapshot import address_lines

    answers = []  # what the buyer typed beyond the buyer block: what fulfilling the order needs
    try:
        stored = (order.answers or {}) if order is not None else {}
        email = order.email if order is not None else ""
    except UndecryptableValue:
        stored, email = {}, "(cannot be decrypted)"
    if email:
        answers.append(("E-mail typed", email))
    if stored.get("phone"):
        answers.append(("Phone", stored["phone"]))
    if stored.get("shipping"):
        answers.append(("Ship to", ", ".join(address_lines(stored["shipping"]))))
    answers += [(entry.get("label") or "", entry.get("value") or "") for entry in stored.get("custom") or []]
    from apps.links.services import held_for_us_review

    held = order is not None and LinkOrder.objects.filter(held_for_us_review(), pk=order.pk).exists()
    return {"link_order": order, "order_link": link, "coupon_code": invoice.coupon.code if invoice.coupon_id else "",
            "order_answers": answers, "order_us_answer": order.us_answer if order is not None else "",
            "order_held": held,
            "to_fulfil": link.fulfillment and order is not None and order.fulfilled_at is None and not held
            and invoice.status in ("paid", "overpaid")}


@staff_required
def invoice_detail(request, pk):
    return render(request, "staff/invoice_detail.html", _detail_context(_invoice(pk)))


def _invoice(pk):
    # metadata is read through migration.read_state, which survives a value that cannot be decrypted
    return get_object_or_404(Invoice.objects.select_related("contact", "org", "requested_by_org")
                             .defer("metadata"), pk=pk)


@require_POST
@staff_required
def record_payment(request, pk):
    invoice = _invoice(pk)
    if (locked := _locked_if_migrated(request, invoice)) is not None:
        return locked
    form = RecordPaymentForm(request.POST, currency=invoice.currency)
    error = None
    if form.is_valid():
        try:
            record_manual_payment(invoice, amount_minor=form.cleaned_data["amount"], rail=form.cleaned_data["rail"],
                                  note=form.cleaned_data["note"], user=request.user)
            return redirect("staff:invoice", pk=pk)
        except InvoiceError as exc:
            error = str(exc)
    invoice.refresh_from_db()
    ctx = _detail_context(invoice, form=form)
    ctx["error"] = error
    return render(request, "staff/invoice_detail.html", ctx, status=400 if error is None else 409)


@require_POST
@staff_required
def void(request, pk):
    invoice = _invoice(pk)
    if (locked := _locked_if_migrated(request, invoice)) is not None:
        return locked
    form = ReasonForm(request.POST)
    error = None
    if form.is_valid():
        try:
            with transaction.atomic():
                void_invoice(invoice, form.cleaned_data["reason"], request.user)
                audit.record(request.user, "invoice.void", invoice, {"reason": form.cleaned_data["reason"]})
            return redirect("staff:invoice", pk=pk)
        except InvoiceError as exc:
            error = str(exc)
    invoice.refresh_from_db()
    ctx = _detail_context(invoice, void_form=form)
    ctx["error"] = error
    return render(request, "staff/invoice_detail.html", ctx, status=400 if error is None else 409)


def _payment_action(request, pk, action):
    payment = get_object_or_404(Payment.objects.select_related("invoice"), pk=pk)
    if (locked := _locked_if_migrated(request, payment.invoice_id)) is not None:
        return locked
    form = NoteForm(request.POST)
    note = form.cleaned_data["note"].strip() if form.is_valid() else ""
    try:
        if action == "confirm":
            confirm_payment(payment, request.user, note)
        else:
            reject_payment(payment, request.user, note)
    except InvoiceError as exc:
        if payment.invoice_id is None:  # a payment in the review queue has no invoice page
            return render(request, "staff/needs_person.html", _needs_person_context(str(exc)), status=409)
        ctx = _detail_context(payment.invoice)
        ctx["error"] = str(exc)
        return render(request, "staff/invoice_detail.html", ctx, status=409)
    if payment.invoice_id is None:
        return redirect("staff:needs_person")
    return redirect("staff:invoice", pk=payment.invoice_id)


@require_POST
@staff_required
def confirm(request, pk):
    return _payment_action(request, pk, "confirm")


@require_POST
@staff_required
def reject(request, pk):
    return _payment_action(request, pk, "reject")


@require_POST
@staff_required
def reverse(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("invoice"), pk=pk)
    if payment.invoice_id is None:
        return render(request, "staff/needs_person.html",
                      _needs_person_context("A payment with no invoice cannot be reversed."), status=409)
    if (locked := _locked_if_migrated(request, payment.invoice_id)) is not None:
        return locked
    form = ReasonForm(request.POST)
    error = None
    if form.is_valid():
        try:
            reverse_payment(payment, request.user, form.cleaned_data["reason"])
            return redirect("staff:invoice", pk=payment.invoice_id)
        except InvoiceError as exc:
            error = str(exc)
    ctx = _detail_context(_invoice(payment.invoice_id))
    ctx["error"] = error or "A reason is required."
    return render(request, "staff/invoice_detail.html", ctx, status=409 if error else 400)


@require_POST
@staff_required
def dismiss(request, pk):
    declaration = get_object_or_404(Declaration, pk=pk)
    if (locked := _locked_if_migrated(request, declaration.invoice_id)) is not None:
        return locked
    form = OptionalReasonForm(request.POST)
    try:
        dismiss_declaration(declaration, request.user, form.cleaned_data["reason"].strip() if form.is_valid() else "")
    except InvoiceError as exc:
        ctx = _detail_context(_invoice(declaration.invoice_id))
        ctx["error"] = str(exc)
        return render(request, "staff/invoice_detail.html", ctx, status=409)
    return redirect("staff:invoice", pk=declaration.invoice_id)


def _person_error(request, message, status):
    return render(request, "staff/needs_person.html", _needs_person_context(message), status=status)


def _invoices_named(ref: str, org_ids: set[int]) -> list[Invoice]:
    """The invoices (at most two) whose number is `ref`, or whose id it is, within `org_ids` (the
    organizations the payment may be credited to); across every organization only when it
    belongs to none. Plaintext columns only."""
    scope = Invoice.objects.only(*INVOICE_PLAIN_FIELDS)
    if org_ids:
        scope = scope.filter(org_id__in=org_ids)
    named = Q(number=ref)
    if ref.isascii() and ref.isdigit() and len(ref) <= 18:
        named |= Q(pk=int(ref))
    return list(scope.filter(named).order_by("pk")[:2])


@require_POST
@staff_required
def assign(request, pk):
    payment = get_object_or_404(Payment.objects.only(*PAYMENT_PLAIN_FIELDS), pk=pk)
    form = AssignPaymentForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "An invoice, an amount and a note are required.", 400)
    ref = form.cleaned_data["invoice"].strip()
    named = _invoices_named(ref, payment_org_ids(payment))
    if not named:
        return _person_error(request, f"No invoice of this payment's organization has the number or id {ref}.", 400)
    if len(named) > 1:
        return _person_error(request, f"More than one invoice answers to {ref}. Use the number or the id of the "
                                      "one you mean, whichever names it alone.", 400)
    invoice = named[0]
    if (locked := _locked_if_migrated(request, invoice.pk, person=True)) is not None:
        return locked
    try:
        minor = parse_minor(form.cleaned_data["amount"], invoice.currency)
    except forms.ValidationError as exc:
        return _person_error(request, exc.messages[0], 400)
    try:
        assign_payment(payment, invoice, request.user, amount_minor=minor, note=form.cleaned_data["note"])
    except InvoiceError as exc:
        return _person_error(request, str(exc), 409)
    return redirect("staff:needs_person")


@require_POST
@staff_required
def refund(request, pk):
    payment = get_object_or_404(Payment.objects.only(*PAYMENT_PLAIN_FIELDS), pk=pk)
    if (locked := _locked_if_migrated(request, payment.invoice_id, person=True)) is not None:
        return locked
    form = RefundNoteForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A note is required to acknowledge a refund.", 409)
    try:
        acknowledge_refund(payment, request.user, form.cleaned_data["note"])
    except InvoiceError as exc:
        return _person_error(request, str(exc), 409)
    return redirect("staff:needs_person")


@require_POST
@staff_required
def keep_gateway_invalid(request, pk):
    payment = get_object_or_404(Payment.objects.only(*PAYMENT_PLAIN_FIELDS), pk=pk)
    if (locked := _locked_if_migrated(request, payment.invoice_id, person=True)) is not None:
        return locked
    form = RequiredNoteForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A note is required to keep a payment the gateway calls invalid.", 409)
    try:
        acknowledge_gateway_invalid(payment, request.user, form.cleaned_data["note"])
    except InvoiceError as exc:
        return _person_error(request, str(exc), 409)
    return redirect("staff:needs_person")


@require_POST
@staff_required
def keep_overpayment_view(request, pk):
    """Keep the money paid over the total instead of refunding it; a note says why (owner, 2026-11-09)."""
    if (locked := _locked_if_migrated(request, pk)) is not None:
        return locked
    invoice = get_object_or_404(Invoice.objects.only(*INVOICE_PLAIN_FIELDS), pk=pk)
    form = RequiredNoteForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A note is required to keep an overpayment.", 409)
    try:
        keep_overpayment(invoice, request.user, form.cleaned_data["note"])
    except InvoiceError as exc:
        return _person_error(request, str(exc), 409)
    return redirect("staff:invoice", pk=invoice.pk)


@require_POST
@staff_required
def accept_fx_leg(request, pk):
    """Accept the refused exchange rate waiting for a currency as its new reference (a person
    checked the ECB's rate). Audited with the pair and both rates."""
    leg = get_object_or_404(FxLegRate, pk=pk)
    form = RequiredNoteForm(request.POST)
    if not form.is_valid():
        return _person_error(request, "A note is required to accept an exchange rate.", 409)
    if not fx.accept_leg(leg, request.user, form.cleaned_data["note"]):
        return _person_error(request, f"No refused {leg.quote} rate is waiting to be accepted.", 409)
    return redirect("staff:needs_person")


@require_POST
@staff_required
def acknowledge_gap(request, pk):
    gap = get_object_or_404(ScanGap, pk=pk)
    form = NoteForm(request.POST)
    note = form.cleaned_data["note"].strip() if form.is_valid() else ""
    if not health.acknowledge_gap(gap, request.user, note):
        return _person_error(request, "Those blocks were already acknowledged.", 409)
    return redirect("staff:needs_person")


# --- Creating, editing, sending and re-linking invoices ---------------------------------------

NO_NAME = "—"


def _decorate_listed(invoice) -> None:
    """The contact's name and the total, read for this row alone: a value the vault cannot open
    shows as a dash and never takes the list down."""
    try:
        contact = Contact.objects.only("pk", "display_name", "legal_name").get(pk=invoice.contact_id)
        invoice.contact_name = contact.display_name or contact.legal_name or NO_NAME
    except UndecryptableValue:
        invoice.contact_name = NO_NAME
    invoice.total_text = format_money(stored_total(invoice), invoice.currency)


STATUS_TABS = [("", "All"), ("draft", "Draft"), ("sent", "Sent"), ("overdue", "Overdue"), ("declared", "Declared"),
               ("partially_paid", "Partially paid"), ("paid", "Paid"), ("overpaid", "Overpaid"), ("void", "Void")]


@staff_required
def invoice_list(request):
    status = request.GET.get("status", "")
    org = request.GET.get("organization", "")
    q = request.GET.get("q", "").strip()[:64]
    rows = Invoice.objects.select_related("org").only(*INVOICE_PLAIN_FIELDS, "org__slug").order_by("-pk")
    if status not in {s for s, _ in Invoice.STATUSES}:
        status = ""
    if org.isascii() and org.isdigit() and len(org) <= 9:
        rows = rows.filter(org_id=int(org))
    else:
        org = ""
    if q:
        rows = rows.filter(Q(number__icontains=q) | Q(external_reference__icontains=q))
    # The status tabs count what the other filters leave, one query for all of them.
    counts = dict(rows.order_by().values_list("status").annotate(n=Count("pk")))
    tabs = []
    for value, label in STATUS_TABS:
        params = urlencode([(k, v) for k, v in (("status", value), ("organization", org), ("q", q)) if v])
        tabs.append({"label": label, "url": f"?{params}", "current": value == status,
                     "count": sum(counts.values()) if not value else counts.get(value, 0)})
    if status:
        rows = rows.filter(status=status)
    names = org_names()

    def decorate(invoice):
        _decorate_listed(invoice)
        invoice.org_name = names.get(invoice.org_id, invoice.org.slug)

    return render(request, "staff/invoice_list.html", {
        "invoices": Section(rows, decorate), "status_tabs": tabs,
        "organizations": [(pk, name) for pk, name in names.items()],
        "status": status, "org": org, "q": q,
    })


def _issuer_for(org: Organization, us_flag: str) -> tuple[Organization | None, bool]:
    """Who would issue an invoice requested by `org` for a contact with this US answer."""
    try:
        return resolve_issuer(org, Contact(org=org, us_tax_resident=us_flag))
    except InvoiceError:
        return None, True


LINK_KINDS = {"revolut": "Revolut", "n26": "N26", "paypal": "PayPal", "wise": "Wise", "other": "Payment link"}
RAIL_KINDS = {"evm": "EVM chains", "tron": "Tron", "solana": "Solana", "ton": "TON", "btcpay": "BTCPay",
              "lightning": "Lightning"}


def method_choices(issuer: Organization, currency: str) -> list[tuple[str, str]]:
    """The offered-method keys of `issuer` for `currency`, with a label (plaintext columns only)."""
    banks = {f"bank:{b.pk}": f"Bank transfer · {b.label or f'account {b.pk}'} ({b.currency})"
             for b in BankAccount.objects.filter(org=issuer, active=True).only("pk", "label", "currency")}
    links = {f"fiat:{l.pk}": " · ".join(filter(None, (LINK_KINDS.get(l.kind, l.kind.title()), l.label)))
             for l in FiatLink.objects.filter(org=issuer, active=True).only("pk", "kind", "label")}
    rails = {f"rail:{r.pk}": " · ".join(filter(None, (r.label, RAIL_KINDS.get(r.kind, r.kind))))
             for r in RailAccount.objects.filter(org=issuer, active=True).only("pk", "label", "kind")}
    labels = {**banks, **links, **rails}
    return [(key, labels.get(key, key)) for key in default_methods(issuer, currency)]


def _stored_flag(org, email) -> str:
    """The US answer on file for the contact with this e-mail in `org`, or "" when there is none."""
    contact = find_contact(org, email) if org is not None and email else None
    return contact.us_tax_resident if contact is not None else ""


def _methods_context(org, us_flag, currency, selected=None, email=""):
    stored = _stored_flag(org, email)
    us_flag = effective_us_flag(stored, us_flag)
    issuer, by_rule = _issuer_for(org, us_flag) if org is not None else (None, False)
    choices = method_choices(issuer, currency) if issuer is not None and currency in CURRENCIES else []
    chosen = {k for k, _ in choices} if selected is None else set(selected)
    return {"issuer": issuer, "by_rule": by_rule, "requested_by": org, "currency": currency,
            "issuer_name": org_name(issuer) if issuer is not None else "",
            "requested_by_name": org_name(org) if org is not None else "",
            "stored_flag": stored if stored in ("yes", "no") else "",
            "methods": [(k, label, k in chosen) for k, label in choices]}


@staff_required
def invoice_methods(request):
    """The HTMX partial under the form: who would issue this, and which methods it would offer."""
    org = Organization.objects.filter(pk=request.GET.get("organization", "").strip() or 0).first() \
        if request.GET.get("organization", "").strip().isdigit() else None
    currency = request.GET.get("currency", "").upper()
    flag = request.GET.get("us_tax_resident", "")
    if org is None or currency not in CURRENCIES or flag not in ("", "yes", "no", "unknown"):
        return HttpResponse("Choose an organization, a currency and the US tax resident answer.", status=400)
    # Still blank: the answer on file for the e-mail counts, as "unknown" would.
    return render(request, "staff/_methods.html",
                  _methods_context(org, flag or "unknown", currency, email=request.GET.get("email", "").strip()))


def _form_page(request, form, formset, *, invoice=None, error=None, status=200):
    org = form["organization"].value()
    org = Organization.objects.filter(pk=org).first() if str(org or "").isdigit() else \
        (org if isinstance(org, Organization) else None)
    flag = form["us_tax_resident"].value() or ""
    currency = (form["currency"].value() or "EUR").upper()
    selected = request.POST.getlist("methods") if request.method == "POST" else \
        (invoice.offered_methods if invoice is not None else None)
    ctx = {"form": form, "formset": formset, "invoice": invoice, "error": error,
           **_methods_context(org, flag if flag in ("yes", "no", "unknown") else "unknown", currency, selected,
                              email=str(form["email"].value() or "").strip())}
    return render(request, "staff/invoice_form.html", ctx, status=status)


def _line_formset(request, currency):
    data = request.POST if request.method == "POST" else None
    return LineFormSet(data, prefix="lines", form_kwargs={"currency": currency})


def _currency_of(request, default="EUR") -> str:
    value = (request.POST.get("currency") or default).upper()
    return value if value in CURRENCIES else default


def _chosen_methods(request, form, org, flag, currency):
    """The methods ticked, checked against what the issuer actually has; None = the defaults."""
    chosen = request.POST.getlist("methods")
    issuer, _ = _issuer_for(org, flag)
    allowed = {k for k, _ in method_choices(issuer, currency)} if issuer is not None else set()
    if not chosen:
        if allowed:
            form.add_error(None, "Choose at least one payment method.")
        return []
    if not set(chosen) <= allowed:
        form.add_error(None, "A chosen payment method does not belong to the organization that will issue this.")
        return []
    return [k for k in chosen if k in allowed]


@require_http_methods(["GET", "POST"])
@staff_required
def invoice_new(request):
    if request.method == "GET":
        first = Organization.objects.order_by("slug").first()
        form = InvoiceForm(initial={"organization": first.pk if first else None, "kind": "invoice",
                                    "currency": "EUR", "due_days": 14})
        return _form_page(request, form, _line_formset(request, "EUR"))
    currency = _currency_of(request)
    form, formset = InvoiceForm(request.POST), _line_formset(request, currency)
    ok = form.is_valid() & formset.is_valid()
    methods = None
    if ok:
        cd = form.cleaned_data
        stored = _stored_flag(cd["organization"], cd["email"])
        methods = _chosen_methods(request, form, cd["organization"],
                                  effective_us_flag(stored, cd["us_tax_resident"]), cd["currency"])
        ok = not form.errors
    if not ok:
        return _form_page(request, form, formset, status=400)
    cd = form.cleaned_data
    fields = form.contact_fields()
    try:
        with transaction.atomic():
            contact, created = find_or_create_contact(cd["organization"], cd["email"], actor=request.user, **fields)
            if not created:
                apply_typed_fields(contact, fields, actor=request.user)
            invoice, _new = create_invoice(
                cd["organization"], contact, currency=cd["currency"], lines=formset.lines(), kind=cd["kind"],
                issue_date=cd["issue_date"], due_days=cd["due_days"], title=cd["title"].strip(),
                notes=cd["notes"].strip(), idempotency_key=cd["form_token"], offered_methods=methods,
                actor=request.user, contact_fields=fields,
            )
    except InvoiceError as exc:
        return _form_page(request, form, formset, error=str(exc), status=409)
    return redirect("staff:invoice", pk=invoice.pk)


def _not_a_draft(request, invoice):
    ctx = _detail_context(invoice)
    ctx["error"] = "Only a draft can be edited or deleted; this invoice was already sent."
    return render(request, "staff/invoice_detail.html", ctx, status=409)


def _initial_for(invoice):
    contact = invoice.contact
    address = contact.address if isinstance(contact.address, dict) else {}
    initial = {
        "organization": invoice.requested_by_org_id, "kind": invoice.kind, "email": primary_address(contact),
        "legal_name": contact.legal_name, "display_name": contact.display_name, "tax_ids": contact.tax_ids,
        "preferred_language": contact.preferred_language, "us_tax_resident": contact.us_tax_resident,
        "currency": invoice.currency, "issue_date": invoice.issue_date, "title": invoice.title, "notes": invoice.notes,
        "due_days": (invoice.due_date - invoice.issue_date).days if invoice.due_date else None,
    }
    initial.update({f"address_{part}": address.get(part, "") for part in ADDRESS_PARTS})
    return initial


@require_http_methods(["GET", "POST"])
@staff_required
def invoice_edit(request, pk):
    invoice = _invoice(pk)
    if invoice.status != "draft":
        return _not_a_draft(request, invoice)
    if request.method == "GET":
        form = InvoiceForm(initial=_initial_for(invoice), editing=True)
        initial_lines = [{
            "name": l.name, "description": l.description or "", "quantity": format(l.quantity.normalize(), "f"),
            "unit_price": major_text(l.unit_price_minor, invoice.currency),
            "tax_percent": format((Decimal(l.tax_rate_bps) / 100).normalize(), "f") if l.tax_rate_bps else "",
            "discount_percent": format((Decimal(l.discount_rate_bps) / 100).normalize(), "f")
            if l.discount_rate_bps else "",
        } for l in invoice.lines.all()]
        formset = LineFormSet(initial=initial_lines, prefix="lines", form_kwargs={"currency": invoice.currency})
        return _form_page(request, form, formset, invoice=invoice)
    currency = _currency_of(request, invoice.currency)
    data = request.POST.copy()
    initial = _initial_for(invoice)
    form = InvoiceForm(data, initial=initial, editing=True)
    formset = _line_formset(request, currency)
    ok = form.is_valid() & formset.is_valid()
    methods = None
    if ok:
        cd = form.cleaned_data
        flag = effective_us_flag(invoice.contact.us_tax_resident, cd["us_tax_resident"])
        issuer, _ = _issuer_for(invoice.requested_by_org, flag)
        if issuer is None or issuer.pk != invoice.org_id:
            form.add_error("us_tax_resident", "This answer would change who issues the invoice. Delete this draft "
                                              "and create a new one.")
        else:
            methods = _chosen_methods(request, form, invoice.requested_by_org, flag, cd["currency"])
        ok = not form.errors
    if not ok:
        return _form_page(request, form, formset, invoice=invoice, status=400)
    cd = form.cleaned_data
    try:
        with transaction.atomic():
            apply_typed_fields(invoice.contact, form.contact_fields(), actor=request.user)
            update_draft(invoice, kind=cd["kind"], currency=cd["currency"], issue_date=cd["issue_date"],
                         due_days=cd["due_days"], title=cd["title"].strip(), notes=cd["notes"].strip(),
                         lines=formset.lines(), offered_methods=methods, actor=request.user)
    except InvoiceError as exc:
        return _form_page(request, form, formset, invoice=invoice, error=str(exc), status=409)
    return redirect("staff:invoice", pk=invoice.pk)


@staff_required
@require_POST
def invoice_delete(request, pk):
    invoice = _invoice(pk)
    if (locked := _locked_if_migrated(request, invoice)) is not None:
        return locked
    try:
        with transaction.atomic():
            if lock_invoice(pk).status != "draft":
                raise InvoiceError("only draft invoices can be deleted")
            audit.record(request.user, "invoice.delete", invoice, {"kind": invoice.kind, "currency": invoice.currency})
            invoice.delete()
    except InvoiceError:
        return _not_a_draft(request, invoice)
    return redirect("staff:invoice_list")


@staff_required
@require_POST
def invoice_send(request, pk):
    invoice = _invoice(pk)
    if (locked := _locked_if_migrated(request, invoice)) is not None:
        return locked
    form = SendForm(request.POST)
    try:
        send_invoice(invoice, email_client=form.is_valid() and form.cleaned_data["email_client"],
                     actor=request.user)
    except InvoiceError as exc:
        ctx = _detail_context(_invoice(pk))
        ctx["error"] = ("This invoice was already sent." if ctx["invoice"].status != "draft" else str(exc))
        return render(request, "staff/invoice_detail.html", ctx, status=409)
    return redirect("staff:invoice", pk=pk)


@require_http_methods(["GET", "POST"])
@staff_required
def invoice_links(request, pk):
    """Reissue the capability links. GET asks; POST replaces them and shows the new ones on this
    response only: they are stored as hashes, never logged and never put in a URL."""
    invoice = _invoice(pk)
    if (locked := _locked_if_migrated(request, invoice)) is not None:
        return locked
    if invoice.status in ("draft", "void"):
        ctx = _detail_context(invoice)
        ctx["error"] = "Links can only be reissued for an invoice that has been sent."
        return render(request, "staff/invoice_detail.html", ctx, status=409)
    # The contact became a US tax resident after this invoice was issued: it must be voided and made
    # again by the US-resident issuer, never sent to the client again from this one.
    wrong_issuer = invoice.contact.is_us_tax_resident and not invoice.org.is_us_resident_issuer
    if request.method == "GET":
        return render(request, "staff/invoice_links.html",
                      {"invoice": invoice, "form": LinksForm(), "wrong_issuer": wrong_issuer})
    form = LinksForm(request.POST)
    # An invoice sent with client_letters=false is the caller's to write about: never mailed from here.
    email_client = form.is_valid() and form.cleaned_data["email_client"] and invoice.client_letters
    if email_client and wrong_issuer:
        return render(request, "staff/invoice_links.html", {
            "invoice": invoice, "form": form, "wrong_issuer": True,
            "error": "The contact is a US tax resident and this invoice is not from the US-resident issuer: new "
                     "links will not be e-mailed for it. Void it and create it again. Nothing was reissued."},
            status=409)
    from apps.links.services import order_recipient

    typed = order_recipient(invoice)  # a payment link's order: the address typed at checkout
    recipient = (typed[0] if typed else primary_address(invoice.contact)) if email_client else None
    if email_client and not recipient:
        return render(request, "staff/invoice_links.html", {
            "invoice": invoice, "form": form, "error": "The contact has no e-mail address to send the links to."},
            status=409)
    try:
        with transaction.atomic():
            view_plain, pay_plain = reissue_links(invoice, actor=request.user)
            if recipient:
                queue_invoice_letter(invoice, recipient, view_plain, pay_plain)
            # Staff made the old links dead: an integrator that stored them must hear. The API
            # endpoint does not emit this, because its caller has just stored the new links.
            webhook_events.invoice_event("invoice.links_reissued", invoice)
    except InvoiceError as exc:
        ctx = _detail_context(_invoice(pk))
        ctx["error"] = str(exc)
        return render(request, "staff/invoice_detail.html", ctx, status=409)
    return render(request, "staff/invoice_links.html", {
        "invoice": invoice, "reissued": True, "emailed": bool(recipient),
        "view_url": invoice.view_url(view_plain), "pay_url": invoice.pay_url(pay_plain),
    })


def _export_request(request):
    """(organization ids, from, to) from the query, or None for a bad range."""
    from apps.exports import csv as exports_csv

    window = exports_csv.parse_range(request.GET.get("from"), request.GET.get("to"))
    if window is None:
        return None
    ids = [int(v) for v in request.GET.getlist("organization") if v.isascii() and v.isdigit() and len(v) <= 9]
    if not ids:
        ids = list(Organization.objects.values_list("pk", flat=True))
    return ids, *window


def _export_download(request, kind):
    from django.http import StreamingHttpResponse

    from apps.exports import csv as exports_csv

    parsed = _export_request(request)
    if parsed is None:
        return HttpResponse("Choose two valid dates, at most 400 days apart.", status=400, content_type="text/plain")
    ids, date_from, date_to = parsed
    make = exports_csv.invoices_rows if kind == "invoices" else exports_csv.payments_rows
    response = StreamingHttpResponse(exports_csv.stream(make(ids, date_from, date_to)),
                                     content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{kind}-{date_from}-{date_to}.csv"'
    return response


@staff_required
def exports(request):
    today = timezone.localdate()
    return render(request, "staff/exports.html", {
        "organizations": list(org_names().items()),
        "date_from": today.replace(month=1, day=1).isoformat(), "date_to": today.isoformat(),
    })


@staff_required
def export_invoices(request):
    return _export_download(request, "invoices")


@staff_required
def export_payments(request):
    return _export_download(request, "payments")
