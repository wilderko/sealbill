"""API v1. Views authorise, parse and serialise; the lifecycle rules live in the services."""
import hashlib
import hmac
import json
import logging
import re
from contextlib import contextmanager
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import CharField, Exists, F, OuterRef, Q, TextField, Value
from django.db.models.functions import Cast, Concat, Lower
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView
from rest_framework import status
from rest_framework.exceptions import APIException, Throttled, ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView, exception_handler as drf_exception_handler

from apps.api import serializers as s
from apps.api.permissions import INVOICES_WRITE, PAYMENTS_CONFIRM
from apps.audit import services as audit
from apps.contacts.models import Contact
from apps.contacts.services import find_or_create_contact, update_contact
from apps.invoices import migration as migration_lock, pdf, return_url
from apps.invoices.errors import InvoiceError
from apps.invoices.models import Invoice
from apps.invoices import services as invoices
from apps.keys import services as keys
from apps.keys.models import EncryptionKey
from apps.keys.validation import KeyRejected
from apps.payments import services as payments
from apps.orgs.models import BankAccount, FiatLink
from apps.payments.models import Declaration, Payment
from apps.portal.crypto import account_for, account_pairs
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.quotes.services import QUOTE_REQUEST_BUDGET_S, create_quote
from apps.rails import http
from apps.rails.models import RailAccount
from apps.vault.ratelimit import address_bucket, client_ip, rate_limited
from apps.vault.fields import UndecryptableValue
from apps.vault.registry import plaintext_columns
from apps.vault.vault import VaultSealed, vault
from apps.webhooks import delivery as webhook_delivery, events as webhook_events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)


METHOD = re.compile(r"(bank|fiat|rail):[0-9]{1,12}")


def idempotency_key(request):
    key = request.headers.get("Idempotency-Key") or None
    if key is not None and len(key) > 200:
        raise ValidationError({"Idempotency-Key": "at most 200 characters"})
    return key


CONTACT_FIELDS = (
    "legal_name", "display_name", "address", "tax_ids", "phone", "notes", "preferred_language", "us_tax_resident",
)


FINGERPRINT_VERSION = "v2:"


def request_fingerprint(d) -> str:
    """A keyed hash of the validated request, so a reused key with another body is caught.

    Every inline contact field counts: a replay that now says `us_tax_resident: yes` must
    not get back the invoice another issuer made for the earlier request. The hash is an HMAC
    under a key derived from the vault, and versioned ("v2:"): the request names the client, and
    the stored value must not let anyone holding only the database confirm who that is. Needs
    the vault (VaultSealed is a 503, like every API request while sealed)."""
    if d.get("contact"):
        inline = d["contact"]
        contact = {"email": inline["email"].strip().lower(), **{f: inline.get(f) for f in CONTACT_FIELDS}}
    else:
        contact = f"id:{d.get('contact_id')}"
    canonical = {
        "contact": contact, "currency": d["currency"].upper(), "kind": d["kind"],
        "lines": [
            [l["name"], l.get("description") or "", str(Decimal(l["quantity"]).quantize(Decimal("0.01"))),
             l["unit_price_minor"], l["tax_rate_bps"], l["discount_rate_bps"], l["discount_amount_minor"]]
            for l in d["lines"]
        ],
        "due_days": d.get("due_days"), "issue_date": str(d["issue_date"]) if d.get("issue_date") else None,
        "offered_methods": None if d.get("offered_methods") is None else list(d["offered_methods"]),
        "title": d["title"], "notes": d["notes"], "external_reference": d.get("external_reference") or None,
    }
    if d.get("method_kinds"):  # likewise
        canonical["method_kinds"] = sorted(set(d["method_kinds"]))
    if d.get("return_url"):  # only when given, so a fingerprint stored before the field still matches
        canonical["return_url"] = d["return_url"]
    key = vault.derive(b"request-fingerprint")
    digest = hmac.new(key, json.dumps(canonical, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    return FINGERPRINT_VERSION + digest


class BusinessError(APIException):
    def __init__(self, detail, status_code, code):
        self.status_code = status_code
        self.default_code = code
        super().__init__(detail=str(detail), code=code)


@contextmanager
def business_errors(conflict=status.HTTP_409_CONFLICT):
    """InvoiceError and ValueError are refusals the client can act on, never a 500."""
    try:
        yield
    except payments.NeedsPerson as exc:  # a person decides it on the staff pages; no token is one (SEC-2)
        raise BusinessError(exc, status.HTTP_409_CONFLICT, exc.code)
    except InvoiceError as exc:
        raise BusinessError(exc, conflict, "invoice_error")
    except ValueError as exc:
        raise BusinessError(exc, status.HTTP_400_BAD_REQUEST, "invalid_request")
    except OverflowError:  # a date or number beyond what Python can hold: still the caller's input
        raise BusinessError("a date or number is out of range", status.HTTP_400_BAD_REQUEST, "invalid_request")


def exception_handler(exc, context):
    if isinstance(exc, VaultSealed):
        return Response({"error": "vault_sealed"}, status=503)
    if isinstance(exc, UndecryptableValue):
        logger.error("API request hit an undecryptable stored value: %s", exc)
        return Response(
            {"error": "undecryptable", "detail": "a stored value cannot be decrypted; staff have been alerted"},
            status=500,
        )
    if isinstance(exc, Http404):
        return Response({"error": "not_found", "detail": "Not found."}, status=404)
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    code = getattr(exc, "default_code", None) or "error"
    if isinstance(exc, ValidationError):
        code = "validation_error"
    elif isinstance(exc, APIException) and hasattr(exc, "get_codes"):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else code
    detail = response.data.get("detail") if isinstance(response.data, dict) and list(response.data) == ["detail"] else response.data
    response.data = {"error": str(code), "detail": detail}
    return response


class Page(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


def paged(view, request, queryset, serializer):
    """One page of rows. A row whose stored values cannot be opened is reported, never fatal.

    Rows are fetched one by one because the decryption happens while the row is read.
    """
    page = Page()
    pks = page.paginate_queryset(
        queryset.prefetch_related(None).select_related(None).values_list("pk", flat=True), request, view=view
    )
    items = []
    for pk in pks:
        try:
            items.append(serializer(queryset.get(pk=pk)).data)
        except UndecryptableValue:
            logger.error("API list: %s #%s has a value that cannot be decrypted", queryset.model.__name__, pk)
            items.append({"id": pk, "error": "undecryptable"})
    return page.get_paginated_response(items)


def visible_invoices(token):
    return (
        Invoice.objects.filter(Q(requested_by_org=token.org) | Q(org=token.org))
        .select_related("org", "requested_by_org", "contact", "coupon")
        .prefetch_related("lines")
        .order_by("-pk")
    )


def org_contacts(token):
    """The token organization's clients of record. A contact an unpaid payment-link checkout made is not one:
    no list, lookup, read or change reaches it (audit SEC-1)."""
    return Contact.objects.filter(org=token.org, of_record=True).prefetch_related("emails").order_by("-pk")


def _address_match(*, org=None):
    """Rail accounts that `payment_org_ids` would count for the payment of the outer query: an
    active account of the same family (the payment's `rail`) holding the address it was sent to
    and enabling its network. Compared in SQL: addresses are stored normalized, and EVM ones are
    lowercase, so the payment's address is lowercased for EVM only. `org` narrows to one."""
    needle = Concat(Value('"'), OuterRef("network"), Value('"'), output_field=CharField())
    accounts = (
        RailAccount.objects.filter(active=True, kind=OuterRef("rail")).exclude(address="")
        .annotate(nets=Cast("networks", TextField()), needle=needle)
        .filter(Q(address=OuterRef("to_address")) | Q(kind="evm", address=Lower(OuterRef("to_address"))),
                nets__contains=F("needle"))
    )
    return accounts.filter(org=org) if org is not None else accounts


def visible_payments(token):
    """A payment with no invoice (crypto in the review queue) is seen by an admin-scope token
    only, and only when the token's organization may be credited with it (`payment_org_ids`:
    the wallet it reached is that organization's, or shared with it). A payment tied to no
    organization at all (no wallet of ours reached) may be credited by any, as in `assign_payment`.
    One SQL query: the three cases of `payment_org_ids` are written as conditions on the row."""
    visible = Q(invoice__in=visible_invoices(token).values("pk"))
    if token.has_scope("admin"):
        mine = Q(rail_account__org_id=token.org_id) | Q(
            Exists(_address_match(org=token.org_id)))
        tied_to_none = Q(rail_account__isnull=True) & ~Q(Exists(_address_match()))
        visible |= Q(invoice__isnull=True) & (mine | tied_to_none)
    return Payment.objects.filter(visible).order_by("-pk")


def visible_declarations(token):
    return Declaration.objects.filter(invoice__in=visible_invoices(token).values("pk")).order_by("-pk")


class ApiView(APIView):
    scopes: dict = {}

    @property
    def token(self):
        return self.request.auth

    @property
    def actor(self):
        return self.request.user


# ---------------------------------------------------------------- me
class Me(ApiView):
    @extend_schema(responses=s.MeSerializer, operation_id="me",
                   description="Who this token is: its organization, name and scopes. Scope `read`.")
    def get(self, request):
        t = self.token
        return Response({"organization": t.org.slug, "token": t.name, "scopes": list(t.scopes)})


# ---------------------------------------------------------------- contacts
NAME_SEARCH_PER_MINUTE = 30  # per token: every search decrypts the organization's contact names
NAME_SEARCH_LEN = (2, 100)


class ContactList(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(responses=s.paginated(s.ContactOutSerializer), operation_id="contacts_list",
                   parameters=[OpenApiParameter("email", str, description="Exact address (case-insensitive); zero or one result."),
                               OpenApiParameter("name", str, description="2-100 characters; every word must be a word of the legal or display name (case and accents ignored); at most 20 results. Not with `email`."),
                               OpenApiParameter("limit", int), OpenApiParameter("offset", int)])
    def get(self, request):
        rows = org_contacts(self.token)
        email = (request.query_params.get("email") or "").strip()
        name = request.query_params.get("name")
        if name is not None:
            from apps.contacts.services import search_by_name

            name = name.strip()
            if email:
                raise ValidationError({"name": "give either name or email, not both"})
            if not 2 <= len(name) <= NAME_SEARCH_LEN[1]:
                raise ValidationError({"name": "must be 2 to 100 characters"})
            if rate_limited("api-contact-name", f"token-{self.token.pk}", NAME_SEARCH_PER_MINUTE, 60):
                raise Throttled(wait=60)
            matches = search_by_name(self.token.org, name)
            return paged(self, request, rows.filter(pk__in=[c.pk for c in matches]), s.ContactOutSerializer)
        if email:
            from apps.contacts.services import find_contact

            found = find_contact(self.token.org, email)
            rows = rows.filter(pk=found.pk) if found else rows.none()
        return paged(self, request, rows, s.ContactOutSerializer)

    @extend_schema(request=s.ContactCreateSerializer, responses={201: s.ContactOutSerializer, 200: s.ContactOutSerializer},
                   description="Creates the contact, or returns the existing one with that e-mail (200).")
    def post(self, request):
        ser = s.ContactCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        email = data.pop("email")
        with business_errors(status.HTTP_400_BAD_REQUEST):
            contact, created = find_or_create_contact(self.token.org, email, actor=self.actor, **data)
        return Response(s.ContactOutSerializer(contact).data, status=201 if created else 200)


class ContactDetail(ApiView):
    scopes = {"PATCH": INVOICES_WRITE}

    def obj(self, pk):
        return get_object_or_404(org_contacts(self.token), pk=pk)

    @extend_schema(responses=s.ContactOutSerializer)
    def get(self, request, pk):
        return Response(s.ContactOutSerializer(self.obj(pk)).data)

    @extend_schema(request=s.ContactFieldsSerializer, responses=s.ContactOutSerializer,
                   description="Changes the given fields. They reach the copy the US-resident issuer keeps of the "
                               "contact for this organization too, and no other contact (address parts merge there; "
                               "\"unknown\" never lowers its US answer).")
    def patch(self, request, pk):
        from apps.contacts.services import update_contact_and_copies

        contact = self.obj(pk)
        ser = s.ContactFieldsSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        update_contact_and_copies(contact, dict(ser.validated_data), actor=self.actor)
        return Response(s.ContactOutSerializer(contact).data)


# ---------------------------------------------------------------- encryption keys
def visible_keys(token):
    """Keys of the token organization's contacts; the material is never loaded."""
    return (EncryptionKey.objects.filter(contact__org=token.org)
            .only(*plaintext_columns(EncryptionKey), "fingerprint").order_by("-pk"))


class ContactKeys(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    def contact(self, pk):
        return get_object_or_404(org_contacts(self.token), pk=pk)

    @extend_schema(responses=s.paginated(s.KeyOutSerializer), operation_id="contact_keys_list",
                   parameters=[OpenApiParameter("limit", int), OpenApiParameter("offset", int)])
    def get(self, request, pk):
        contact = self.contact(pk)
        return paged(self, request, visible_keys(self.token).filter(contact=contact), s.KeyOutSerializer)

    @extend_schema(
        request=s.KeyInSerializer,
        responses={201: s.KeyOutSerializer, 400: s.ErrorSerializer, 503: s.ErrorSerializer},
        description=(
            "Adds a PGP public key or S/MIME certificate to the contact. It is active at once and replaces the "
            "contact's previous key; e-mail to the contact is then encrypted to it. A refused key is 400 "
            "`key_rejected` with the reason in `detail` (too_large, pkcs12_refused, secret_key, not_a_key, "
            "several_keys, cannot_encrypt, expired, not_yet_valid, revoked, email_mismatch, unsupported_key). "
            "503 `key_check_unavailable` means the key could not be checked right now: nothing was stored, retry."
        ),
    )
    def post(self, request, pk):
        contact = self.contact(pk)
        ser = s.KeyInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            key = keys.add_key(contact, ser.validated_data["key"], source="api", actor=self.actor)
        except KeyRejected as exc:
            raise BusinessError(exc.code, status.HTTP_400_BAD_REQUEST, "key_rejected")
        except keys.KeyCheckUnavailable:
            raise BusinessError("the key could not be checked right now; nothing was stored, retry shortly",
                                status.HTTP_503_SERVICE_UNAVAILABLE, "key_check_unavailable")
        return Response(s.KeyOutSerializer(visible_keys(self.token).get(pk=key.pk)).data, status=201)


class KeyRevoke(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(request=None, responses=s.KeyOutSerializer,
                   description="Withdraws the key: it is never used again. Revoking a revoked key changes nothing.")
    def post(self, request, pk):
        key = get_object_or_404(visible_keys(self.token), pk=pk)
        keys.revoke_key(key, self.actor)
        return Response(s.KeyOutSerializer(visible_keys(self.token).get(pk=pk)).data)


# ---------------------------------------------------------------- invoices
class InvoiceList(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(
        responses=s.paginated(s.InvoiceOutSerializer), operation_id="invoices_list",
        parameters=[OpenApiParameter("external_reference", str), OpenApiParameter("status", str),
                    OpenApiParameter("limit", int), OpenApiParameter("offset", int)],
    )
    def get(self, request):
        rows = visible_invoices(self.token)
        for field in ("external_reference", "status"):
            if request.query_params.get(field):
                rows = rows.filter(**{field: request.query_params[field]})
        return paged(self, request, rows, s.InvoiceOutSerializer)

    @extend_schema(
        request=s.InvoiceCreateSerializer,
        responses={201: s.InvoiceOutSerializer, 200: s.InvoiceOutSerializer, 400: s.ErrorSerializer},
        parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=False)],
        description=(
            "Creates a draft. `Idempotency-Key` (header) and `external_reference` (body) are scoped to the "
            "requesting organization; a repeat returns the existing invoice with 200. A US tax resident "
            "contact moves the invoice to the US-resident issuer (`issued_by_rule`, `issuer_org`)."
        ),
    )
    def post(self, request):
        ser = s.InvoiceCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        org = self.token.org
        # Checked before the idempotent replay: a replay must not hide a return_url this organization no longer allows.
        if d["return_url"] and not return_url.check(d["return_url"], org):
            raise BusinessError("return_url must be an https address on a host this organization allows",
                                status.HTTP_400_BAD_REQUEST, "return_url_not_allowed")
        if d.get("method_kinds") and d.get("offered_methods") is not None:
            raise BusinessError("give `method_kinds` or `offered_methods`, not both",
                                status.HTTP_400_BAD_REQUEST, "methods_conflict")
        key = idempotency_key(request)
        reference = d.get("external_reference") or None
        fingerprint = request_fingerprint(d)
        # A replay is recognised before any contact is touched.
        found = invoices.find_existing(org, key, reference)
        if found is not None:
            return self.replay(found, fingerprint)
        with business_errors(status.HTTP_400_BAD_REQUEST):
            with transaction.atomic():
                contact = self.contact_for(org, d, self.actor)
                invoice, created = invoices.create_invoice(
                    org, contact, currency=d["currency"], lines=[dict(l) for l in d["lines"]], kind=d["kind"],
                    issue_date=d.get("issue_date"), due_days=d.get("due_days"), title=d["title"], notes=d["notes"],
                    external_reference=reference, idempotency_key=key,
                    offered_methods=self.checked_methods(org, contact, d), request_fingerprint=fingerprint,
                    actor=self.actor, return_url=d["return_url"],
                )
        if not created:  # lost a race with an identical request
            return self.replay(invoice, fingerprint)
        invoice = visible_invoices(self.token).get(pk=invoice.pk)
        return Response(s.InvoiceOutSerializer(invoice).data, status=201)

    def replay(self, invoice, fingerprint):
        if invoice.request_fingerprint and invoice.request_fingerprint != fingerprint:
            raise BusinessError(
                "this Idempotency-Key / external_reference was used for a different request",
                status.HTTP_409_CONFLICT, "idempotency_mismatch",
            )
        return Response(s.InvoiceOutSerializer(visible_invoices(self.token).get(pk=invoice.pk)).data, status=200)

    @staticmethod
    def contact_for(org, d, actor=None):
        if d.get("contact_id"):  # a client of record only: never an unpaid checkout's contact (audit SEC-1)
            contact = Contact.objects.filter(org=org, pk=d["contact_id"], of_record=True).first()
            if contact is None:
                raise ValidationError({"contact_id": "no such contact"})
            return contact
        fields = dict(d["contact"])
        contact, created = find_or_create_contact(org, fields.pop("email"), actor=actor, **fields)
        inline = fields.get("us_tax_resident")
        if not created and inline and inline != contact.us_tax_resident:
            if inline == "yes":
                update_contact(contact, {"us_tax_resident": "yes"}, actor=actor)
            elif contact.us_tax_resident == "yes":
                raise BusinessError(
                    "the stored contact is a US tax resident; PATCH the contact first",
                    status.HTTP_409_CONFLICT, "contact_conflict",
                )
        return contact

    @staticmethod
    def checked_methods(org, contact, d):
        """None when omitted (the service then offers the issuer's defaults); else the checked list."""
        methods = d.get("offered_methods")
        if d.get("method_kinds"):
            issuer, _ = invoices.resolve_issuer(org, contact)
            prefixes = {"bank": "bank:", "fiat_link": "fiat:", "rail": "rail:"}
            wanted = tuple(prefixes[k] for k in d["method_kinds"])
            resolved = [m for m in invoices.default_methods(issuer, d["currency"]) if m.startswith(wanted)]
            if not resolved:
                raise BusinessError("the issuing organization has no active method of these kinds",
                                    status.HTTP_409_CONFLICT, "no_methods")
            return resolved
        if methods is None:
            return None
        if not methods:
            return []
        issuer, _ = invoices.resolve_issuer(org, contact)
        currency = d["currency"].upper()
        for m in methods:
            if not METHOD.fullmatch(m):
                raise BusinessError(f"invalid payment method {m!r}", 400, "invalid_offered_method")
            kind, _, raw = m.partition(":")
            model = {"bank": BankAccount, "fiat": FiatLink, "rail": RailAccount}[kind]
            # A crypto rail takes any invoice currency (a quote converts), so only its owner is checked.
            fields = {"bank": ("pk", "currency"), "fiat": ("pk", "currencies"), "rail": ("pk",)}[kind]
            found = model.objects.filter(pk=int(raw), org=issuer, active=True).only(*fields).first()
            if found is None:
                raise BusinessError(f"payment method {m!r} is not available", 400, "invalid_offered_method")
            if kind == "bank" and found.currency.upper() != currency:
                raise BusinessError(f"payment method {m!r} is a {found.currency} account, not {currency}", 400,
                                    "invalid_offered_method")
        return methods


def refuse_if_migrated(invoice_id):
    """A migrated invoice is history: any mutation of it, or of its payments, is a 409 with the code
    `invoice_migrated`. Fails closed when the migration data cannot be read."""
    if invoice_id is None:
        return
    reason = migration_lock.refusal(invoice_id)
    if reason is not None:
        raise BusinessError(reason, status.HTTP_409_CONFLICT, "invoice_migrated")


class InvoiceMixin:
    def invoice(self, pk):
        return get_object_or_404(visible_invoices(self.token), pk=pk)

    def mutable_invoice(self, pk):
        """The invoice for a mutation: found as `invoice` finds it, then refused when it is history."""
        get_object_or_404(visible_invoices(self.token).defer("metadata"), pk=pk)
        refuse_if_migrated(pk)
        return self.invoice(pk)

    def fresh(self, pk):
        return visible_invoices(self.token).get(pk=pk)


class InvoiceDetail(InvoiceMixin, ApiView):
    @extend_schema(responses=s.InvoiceOutSerializer)
    def get(self, request, pk):
        return Response(s.InvoiceOutSerializer(self.invoice(pk)).data)


class InvoiceSend(InvoiceMixin, ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(
        request=s.SendSerializer, responses=s.InvoiceWithLinksSerializer,
        description="Moves a draft to `sent`, assigns its number and returns `view_url` and `pay_url` once. "
                    "`client_letters: false` means no letter about this invoice goes to the client from this system.",
    )
    def post(self, request, pk):
        invoice = self.mutable_invoice(pk)
        ser = s.SendSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with business_errors():
            invoice, view_plain, pay_plain = invoices.send_invoice(
                invoice, email_client=ser.validated_data["email_client"],
                client_letters=ser.validated_data["client_letters"], actor=self.actor
            )
        return self.links(pk, invoice, view_plain, pay_plain)

    def links(self, pk, invoice, view_plain, pay_plain):
        invoice = self.fresh(pk)
        data = s.InvoiceOutSerializer(invoice).data
        data["view_url"] = invoice.view_url(view_plain)
        data["pay_url"] = invoice.pay_url(pay_plain)
        return Response(data)


class InvoiceReissueLinks(InvoiceSend):
    @extend_schema(request=None, responses=s.InvoiceWithLinksSerializer,
                   description="Replaces both links of a sent invoice; the old ones stop working. Returned once.")
    def post(self, request, pk):
        invoice = self.mutable_invoice(pk)
        with business_errors():
            view_plain, pay_plain = invoices.reissue_links(invoice, actor=self.actor)
        return self.links(pk, invoice, view_plain, pay_plain)


class InvoiceVoid(InvoiceMixin, ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(request=s.VoidSerializer, responses=s.InvoiceOutSerializer)
    def post(self, request, pk):
        invoice = self.mutable_invoice(pk)
        ser = s.VoidSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data["reason"]
        with business_errors():
            with transaction.atomic():
                invoices.void_invoice(invoice, reason, self.actor)
                audit.record(self.actor, "invoice.void", invoice, {"reason": reason, "via": f"api:{self.token.name}"})
        return Response(s.InvoiceOutSerializer(self.fresh(pk)).data)


class InvoicePdf(InvoiceMixin, ApiView):
    @extend_schema(responses={(200, "application/pdf"): bytes})
    def get(self, request, pk):
        invoice = self.invoice(pk)
        return HttpResponse(pdf.render_invoice_pdf(invoice, invoice.contact.language), content_type="application/pdf")


class InvoicePayments(InvoiceMixin, ApiView):
    scopes = {"POST": PAYMENTS_CONFIRM}

    @extend_schema(request=s.ManualPaymentSerializer, responses={201: s.PaymentOutSerializer, 200: s.PaymentOutSerializer},
                   parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=False)],
                   description="Staff saw the money arrive: records a confirmed payment and updates the invoice. Only a person "
                               "records a manual payment, on the staff pages: every token gets 409 `needs_person` and "
                               "nothing is written. A replay of a key already used returns that payment (200); another "
                               "body is 409.")
    def post(self, request, pk):
        invoice = self.mutable_invoice(pk)
        ser = s.ManualPaymentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        key = idempotency_key(request) or ""
        if key:
            existing = Payment.objects.filter(invoice=invoice, idempotency_key=key).first()
            if existing is not None:
                return self.replay(existing, d)
        try:
            with business_errors():
                payment = payments.record_manual_payment(
                    invoice, amount_minor=d["amount_minor"], rail=d["rail"], note=d["note"], user=self.actor,
                    idempotency_key=key,
                )
        except IntegrityError:  # a concurrent request with the same key won
            existing = Payment.objects.filter(invoice=invoice, idempotency_key=key).first()
            if not key or existing is None:
                raise
            return self.replay(existing, d)
        return Response(s.PaymentOutSerializer(payment).data, status=201)

    @staticmethod
    def replay(payment, d):
        if (payment.amount_minor, payment.rail, payment.evidence_note or "") != (d["amount_minor"], d["rail"], d["note"]):
            raise BusinessError(
                "this Idempotency-Key was used for a different payment", status.HTTP_409_CONFLICT, "idempotency_mismatch"
            )
        return Response(s.PaymentOutSerializer(payment).data, status=200)


# ---------------------------------------------------------------- quotes
def visible_quotes(token):
    return (Quote.objects.filter(invoice__in=visible_invoices(token).values("pk"))
            .select_related("rail_account").order_by("-pk"))


@contextmanager
def quote_errors():
    """A refused quote is a 409 with its code, except an unreachable rate source (503: retry)."""
    try:
        yield
    except QuoteError as exc:
        raise BusinessError(exc.code.replace("_", " "), 503 if exc.code == "rates_unavailable" else 409, exc.code)


class InvoiceQuotes(InvoiceMixin, ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(responses=s.paginated(s.QuoteOutSerializer), operation_id="invoice_quotes_list",
                   parameters=[OpenApiParameter("limit", int), OpenApiParameter("offset", int)])
    def get(self, request, pk):
        invoice = self.invoice(pk)
        return paged(self, request, visible_quotes(self.token).filter(invoice=invoice), s.QuoteOutSerializer)

    @extend_schema(
        request=s.QuoteRequestSerializer,
        responses={201: s.QuoteOutSerializer, 200: s.QuoteOutSerializer, 409: s.ErrorSerializer, 503: s.ErrorSerializer},
        description=(
            "Asks what to send, on which network, to pay what the invoice still owes. A chain quote names a "
            "unique amount on the rail's shared address; pay exactly `amount_base_units` before `expires_at`. "
            "An open quote for the same method, network and asset is returned as it is (200) unless `refresh`. "
            "Refusals are 409 with the quote error code in `error` (not_payable, method_not_offered, "
            "asset_not_enabled, nothing_due, rates_diverge, no_free_amount, too_many_quotes, rail_unavailable); "
            "too_many_quotes: the invoice already holds 20 quotes not yet released (one is released a day after it "
            "closes). `rates_unavailable` is 503: retry shortly."
        ),
    )
    def post(self, request, pk):
        invoice = self.mutable_invoice(pk)
        ser = s.QuoteRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        before = Quote.objects.filter(invoice_id=invoice.pk).order_by("-pk").values_list("pk", flat=True).first() or 0
        with quote_errors():
            account = account_for(invoice, d["method"])
            with http.request_budget(QUOTE_REQUEST_BUDGET_S):  # a web request: never wait long on a source
                quote = create_quote(invoice, account, d["network"], d["asset"], refresh=d["refresh"])
        quote = visible_quotes(self.token).get(pk=quote.pk)
        return Response(s.QuoteOutSerializer(quote).data, status=201 if quote.pk > before else 200)


class QuoteDetail(ApiView):
    @extend_schema(responses=s.QuoteOutSerializer)
    def get(self, request, pk):
        return Response(s.QuoteOutSerializer(get_object_or_404(visible_quotes(self.token), pk=pk)).data)


# ---------------------------------------------------------------- payment methods
class PaymentMethodList(ApiView):
    @extend_schema(
        responses={200: s.PaymentMethodsSerializer, 400: s.ErrorSerializer}, operation_id="payment_methods_list",
        parameters=[OpenApiParameter("currency", str, required=True),
                    OpenApiParameter("contact_email", str, required=False)],
        description=(
            "What an invoice in `currency` would offer when `offered_methods` is omitted, and which organization "
            "would issue it: give `contact_email` and a US tax resident contact names the US-resident issuer."
        ),
    )
    def get(self, request):
        from apps.contacts.services import find_contact
        from apps.invoices.currencies import check_issuable

        currency = (request.query_params.get("currency") or "").strip().upper()
        if not currency:
            raise ValidationError({"currency": "required"})
        with business_errors():
            check_issuable(currency)
            email = (request.query_params.get("contact_email") or "").strip()
            contact = find_contact(self.token.org, email) if email else None
            issuer, by_rule = invoices.resolve_issuer(self.token.org, contact) if contact else (self.token.org, False)
        methods = []
        for key in invoices.default_methods(issuer, currency):
            kind, _, raw = key.partition(":")
            if kind == "bank":
                acct = BankAccount.objects.get(pk=int(raw))
                methods.append({"id": key, "kind": "bank", "label": acct.label or acct.bank_name,
                                "currencies": [acct.currency.upper()]})
            elif kind == "rail":
                rail = RailAccount.objects.get(pk=int(raw))
                methods.append({"id": key, "kind": "rail", "label": rail.label, "currencies": [currency],
                                "options": [{"network": n, "asset": a} for n, a in account_pairs(rail)]})
            else:
                link = FiatLink.objects.only("pk", "kind", "label", "currencies").get(pk=int(raw))
                methods.append({"id": key, "kind": "fiat", "label": link.label or link.kind,
                                "currencies": [str(c).upper() for c in link.currencies or []]})
        return Response({"currency": currency, "issuer_org": issuer.slug, "issued_by_rule": by_rule,
                         "methods": methods})


# ---------------------------------------------------------------- payments
class PaymentList(ApiView):
    @extend_schema(responses=s.paginated(s.PaymentOutSerializer), operation_id="payments_list",
                   parameters=[OpenApiParameter("limit", int), OpenApiParameter("offset", int),
                               OpenApiParameter("invoice", int, description="Only this invoice's payments.")])
    def get(self, request):
        payments = visible_payments(self.token)
        invoice = request.query_params.get("invoice")
        if invoice is not None:
            try:
                wanted = int(invoice)
            except ValueError:
                raise ValidationError({"invoice": "must be an integer"})
            # An id no row can have (outside the database's bigint) names nothing; SQLite would raise.
            payments = payments.filter(invoice_id=wanted) if -2**63 <= wanted < 2**63 else payments.none()
        return paged(self, request, payments, s.PaymentOutSerializer)


class PaymentAction(ApiView):
    scopes = {"POST": PAYMENTS_CONFIRM}
    request_serializer = s.NoteSerializer

    def post(self, request, pk):
        payment = get_object_or_404(visible_payments(self.token), pk=pk)
        refuse_if_migrated(payment.invoice_id)
        ser = self.request_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with business_errors():
            result = self.act(payment, ser.validated_data)
        return Response(s.PaymentOutSerializer(result).data)


class PaymentConfirm(PaymentAction):
    @extend_schema(request=s.NoteSerializer, responses={200: s.PaymentOutSerializer, 409: s.ErrorSerializer},
                   description="detected/needs_review -> confirmed. A payment waiting for a person "
                               "(`needs_staff_confirmation` true) is confirmed only by a person on the staff pages: "
                               "409 `needs_person` for every token, and nothing changes.")
    def post(self, request, pk):
        return super().post(request, pk)

    def act(self, payment, d):
        return payments.confirm_payment(payment, self.actor, d["note"])


class PaymentReject(PaymentAction):
    @extend_schema(request=s.NoteSerializer, responses=s.PaymentOutSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def act(self, payment, d):
        return payments.reject_payment(payment, self.actor, d["note"])


class PaymentReverse(PaymentAction):
    request_serializer = s.ReverseSerializer

    @extend_schema(request=s.ReverseSerializer, responses=s.PaymentOutSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def act(self, payment, d):
        return payments.reverse_payment(payment, self.actor, d["reason"])


class PaymentAcknowledgeRefund(PaymentAction):
    @extend_schema(request=s.NoteSerializer, responses=s.PaymentOutSerializer,
                   description="Records that the amount in `refund_due_base_units` was refunded, or that nobody will "
                               "refund it. THIS CHANGES MONEY STATE; a note is required. Once only (409 after).")
    def post(self, request, pk):
        return super().post(request, pk)

    def act(self, payment, d):
        return payments.acknowledge_refund(payment, self.actor, d["note"])


class PaymentAssign(ApiView):
    scopes = {"POST": PAYMENTS_CONFIRM}

    @extend_schema(
        request=s.AssignSerializer, responses={200: s.PaymentOutSerializer, 409: s.ErrorSerializer},
        parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)],
        description=(
            "Decides that a payment waiting in the review queue (`state` needs_review; a chain transfer must also read `final` true) belongs to "
            "`invoice_id`, an invoice of the token's own organization, and credits it `amount_minor`. THIS MOVES "
            "MONEY STATE. `Idempotency-Key` is required: a replay returns the same payment (200), another body "
            "is 409 idempotency_mismatch (also when the key was already used on another payment of the "
            "organization). A payment with no invoice is visible to `admin` tokens only, and only when the "
            "token's organization may be credited with it. Refused (409) unless the organization owns the "
            "account the money reached (accepted even if that account is now inactive), or holds another "
            "active account with that address on that network. A payment waiting for a person "
            "(`needs_staff_confirmation` true) is assigned only by a person on the staff pages: 409 `needs_person`."
        ),
    )
    def post(self, request, pk):
        key = idempotency_key(request)
        if not key:
            raise ValidationError({"Idempotency-Key": "required"})
        payment = get_object_or_404(visible_payments(self.token), pk=pk)
        ser = s.AssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        invoice = get_object_or_404(visible_invoices(self.token).filter(org=self.token.org), pk=d["invoice_id"])
        refuse_if_migrated(invoice.pk)
        if payment.idempotency_key == key:
            same = ((payment.state, payment.invoice_id, payment.amount_minor, payment.evidence_note or "")
                    == ("confirmed", invoice.pk, d["amount_minor"], (d["note"] or "").strip()))
            if not same:
                raise self.mismatch()
            return Response(s.PaymentOutSerializer(payment).data)
        if Payment.objects.filter(invoice__org=self.token.org, idempotency_key=key).exclude(pk=payment.pk).exists():
            raise self.mismatch()
        try:
            with business_errors():
                with transaction.atomic():
                    result = payments.assign_payment(payment, invoice, self.actor, amount_minor=d["amount_minor"],
                                                     note=d["note"])
                    if not result.idempotency_key:
                        Payment.objects.filter(pk=result.pk).update(idempotency_key=key)
                        result.idempotency_key = key
        except IntegrityError:  # a concurrent request with the same key won
            raise self.mismatch()
        return Response(s.PaymentOutSerializer(result).data)

    @staticmethod
    def mismatch():
        return BusinessError("this Idempotency-Key was used for a different request", status.HTTP_409_CONFLICT,
                             "idempotency_mismatch")


# ---------------------------------------------------------------- declarations
class DeclarationList(ApiView):
    @extend_schema(responses=s.paginated(s.DeclarationOutSerializer), operation_id="declarations_list",
                   parameters=[OpenApiParameter("limit", int), OpenApiParameter("offset", int)])
    def get(self, request):
        return paged(self, request, visible_declarations(self.token), s.DeclarationOutSerializer)


class DeclarationDismiss(ApiView):
    scopes = {"POST": PAYMENTS_CONFIRM}

    @extend_schema(request=s.ReasonSerializer, responses=s.DeclarationOutSerializer)
    def post(self, request, pk):
        declaration = get_object_or_404(visible_declarations(self.token), pk=pk)
        refuse_if_migrated(declaration.invoice_id)
        ser = s.ReasonSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with business_errors():
            result = payments.dismiss_declaration(declaration, self.actor, ser.validated_data["reason"])
        return Response(s.DeclarationOutSerializer(result).data)


# ---------------------------------------------------------------- webhooks
ADMIN = "admin"
PAGE_PARAMS = [OpenApiParameter("limit", int), OpenApiParameter("offset", int)]


def org_endpoints(token):
    """The token organization's webhook endpoints, and no other organization's."""
    return WebhookEndpoint.objects.filter(org=token.org).defer("secret").order_by("-pk")


class WebhookList(ApiView):
    scopes = {"GET": ADMIN, "POST": ADMIN}

    @extend_schema(responses=s.paginated(s.WebhookOutSerializer), operation_id="webhooks_list",
                   parameters=PAGE_PARAMS)
    def get(self, request):
        return paged(self, request, org_endpoints(self.token), s.WebhookOutSerializer)

    @extend_schema(
        request=s.WebhookCreateSerializer, responses={201: s.WebhookCreatedSerializer, 400: s.ErrorSerializer},
        description=(
            "Registers an endpoint for the token's organization and returns its signing `secret` ONCE. The URL "
            "must be https and its host must resolve to public addresses only; otherwise 400 `webhook_refused` "
            "with `scheme`, `private_address`, `unresolvable`, `bad_url`, `port` (only 443 is allowed) or `endpoint_limit` "
            "(10 active endpoints per organization) in `detail`, and nothing is stored."
        ),
    )
    def post(self, request):
        ser = s.WebhookCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            endpoint, secret = webhook_delivery.create_endpoint(
                self.token.org, d["url"], events=d.get("events"), description=d["description"], actor=self.actor)
        except webhook_delivery.WebhookRefused as exc:
            raise BusinessError(exc.code, status.HTTP_400_BAD_REQUEST, "webhook_refused")
        except ValueError as exc:
            raise BusinessError(exc, status.HTTP_400_BAD_REQUEST, "invalid_request")
        return Response(s.WebhookCreatedSerializer(endpoint, context={"secret": secret}).data, status=201)


class WebhookDetail(ApiView):
    scopes = {"GET": ADMIN, "DELETE": ADMIN}

    def obj(self, pk):
        return get_object_or_404(org_endpoints(self.token), pk=pk)

    @extend_schema(responses=s.WebhookOutSerializer)
    def get(self, request, pk):
        return Response(s.WebhookOutSerializer(self.obj(pk)).data)

    @extend_schema(request=None, responses={204: None},
                   description="Deletes the endpoint, its secret and its delivery log; nothing more is sent to it.")
    def delete(self, request, pk):
        endpoint = get_object_or_404(org_endpoints(self.token).only("pk", "org_id"), pk=pk)
        webhook_delivery.delete_endpoint(endpoint, actor=self.actor)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WebhookDeliveries(ApiView):
    scopes = {"GET": ADMIN}

    @extend_schema(responses=s.paginated(s.WebhookDeliveryOutSerializer), operation_id="webhook_deliveries_list",
                   parameters=PAGE_PARAMS, description="The endpoint's delivery log, newest first.")
    def get(self, request, pk):
        endpoint = get_object_or_404(org_endpoints(self.token).only("pk", "org_id"), pk=pk)
        rows = WebhookDelivery.objects.filter(endpoint=endpoint).select_related("event").order_by("-pk")
        return paged(self, request, rows, s.WebhookDeliveryOutSerializer)


class WebhookPing(ApiView):
    scopes = {"POST": ADMIN}

    @extend_schema(request=None, responses={202: s.WebhookDeliveryOutSerializer, 409: s.ErrorSerializer},
                   description="Queues a `ping` event for this endpoint alone; the worker sends it within about 15 "
                               "seconds. Follow it in /deliveries.")
    def post(self, request, pk):
        endpoint = get_object_or_404(org_endpoints(self.token).only("pk", "org_id", "active"), pk=pk)
        if not endpoint.active:
            raise BusinessError("the endpoint is inactive", status.HTTP_409_CONFLICT, "endpoint_inactive")
        event = webhook_events.emit("ping", org=self.token.org, endpoint=endpoint, data={"endpoint_id": endpoint.pk})
        row = (WebhookDelivery.objects.select_related("event").filter(event=event, endpoint=endpoint).first()
               if event is not None else None)
        if row is None:
            raise BusinessError("the ping could not be queued; retry shortly", status.HTTP_503_SERVICE_UNAVAILABLE,
                                "ping_unavailable")
        return Response(s.WebhookDeliveryOutSerializer(row).data, status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------- payment links
def org_links(token):
    from apps.links.models import PaymentLink

    return PaymentLink.objects.filter(org=token.org).select_related("org").order_by("-pk")


def _link_fields(validated) -> dict:
    """The serializer's data as the service takes it: custom questions as plain dicts."""
    data = dict(validated)
    if data.get("custom_fields") is not None:
        data["custom_fields"] = [dict(q) for q in data["custom_fields"]]
    return data


class PaymentLinkList(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(responses=s.paginated(s.PaymentLinkOutSerializer), operation_id="payment_links_list",
                   parameters=[OpenApiParameter("limit", int), OpenApiParameter("offset", int)],
                   description="Your organization's payment links, newest first. The list never carries a link's URL.")
    def get(self, request):
        return paged(self, request, org_links(self.token), s.PaymentLinkOutSerializer)

    @extend_schema(request=s.PaymentLinkFieldsSerializer, responses={201: s.PaymentLinkDetailSerializer},
                   operation_id="payment_links_create",
                   description="Creates a payment link of your organization, under the staff form's rules. "
                               "The answer carries the `url` to share.")
    def post(self, request):
        from apps.links import services as links

        ser = s.PaymentLinkFieldsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with business_errors(status.HTTP_400_BAD_REQUEST):
            link, _plain = links.create_link(self.token.org, actor=self.actor, channel="api", **_link_fields(ser.validated_data))
        return Response(s.PaymentLinkDetailSerializer(link).data, status=201)


class PaymentLinkDetail(ApiView):
    scopes = {"PATCH": INVOICES_WRITE}

    def obj(self, pk):
        return get_object_or_404(org_links(self.token), pk=pk)

    @extend_schema(responses=s.PaymentLinkDetailSerializer, operation_id="payment_links_retrieve",
                   description="One link, with its `url`: the only place the URL is given out besides create and rotate.")
    def get(self, request, pk):
        return Response(s.PaymentLinkDetailSerializer(self.obj(pk)).data)

    @extend_schema(request=s.PaymentLinkFieldsSerializer, responses=s.PaymentLinkDetailSerializer,
                   operation_id="payment_links_update",
                   description="Changes the fields given. `{\"active\": false}` deactivates the link: its page then "
                               "answers that it is no longer available. Orders already made are untouched.")
    def patch(self, request, pk):
        from apps.links import services as links

        link = self.obj(pk)
        ser = s.PaymentLinkFieldsSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        with business_errors(status.HTTP_400_BAD_REQUEST):
            links.update_link(link, actor=self.actor, channel="api", **_link_fields(ser.validated_data))
        return Response(s.PaymentLinkDetailSerializer(self.obj(pk)).data)


class PaymentLinkRotate(ApiView):
    scopes = {"POST": INVOICES_WRITE}

    @extend_schema(request=None, responses=s.PaymentLinkDetailSerializer, operation_id="payment_links_rotate",
                   description="Replaces the link's URL. The old URL stops working at once. Returns the link with the new `url`.")
    def post(self, request, pk):
        from apps.links import services as links

        link = get_object_or_404(org_links(self.token), pk=pk)
        links.rotate_token(link, actor=self.actor, channel="api")
        return Response(s.PaymentLinkDetailSerializer(link).data)


class _Export(ApiView):
    """CSV for the books: what this token may see, in a streamed download. 400 `bad_range` for a
    missing or malformed date, a backwards range, or more than 400 days."""

    kind = ""

    def rows(self, date_from, date_to):
        raise NotImplementedError

    def get(self, request):
        from django.http import StreamingHttpResponse

        from apps.exports import csv as exports_csv

        window = exports_csv.parse_range(request.query_params.get("from"), request.query_params.get("to"))
        if window is None:
            return Response({"error": "bad_range", "detail": "from and to are YYYY-MM-DD, at most 400 days apart."},
                            status=status.HTTP_400_BAD_REQUEST)
        response = StreamingHttpResponse(exports_csv.stream(self.rows(*window)), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{self.kind}-{window[0]}-{window[1]}.csv"'
        response["Cache-Control"] = "no-store"
        return response


_EXPORT_DOC = dict(
    parameters=[OpenApiParameter("from", str, required=True, description="YYYY-MM-DD"),
                OpenApiParameter("to", str, required=True, description="YYYY-MM-DD, at most 400 days after from")],
    responses={(200, "text/csv"): OpenApiTypes.STR},
)


class InvoiceExport(_Export):
    kind = "invoices"

    def rows(self, date_from, date_to):
        from apps.exports import csv as exports_csv

        return exports_csv.invoices_rows(None, date_from, date_to, queryset=visible_invoices(self.token))

    @extend_schema(operation_id="exports_invoices", **_EXPORT_DOC)
    def get(self, request):
        return super().get(request)


class PaymentExport(_Export):
    kind = "payments"

    def rows(self, date_from, date_to):
        from apps.exports import csv as exports_csv

        return exports_csv.payments_rows(None, date_from, date_to, queryset=visible_payments(self.token))

    @extend_schema(operation_id="exports_payments", **_EXPORT_DOC)
    def get(self, request):
        return super().get(request)


@csrf_exempt
def not_found(request, *args, **kwargs):
    """Any /api/v1/... path no route matched: JSON like every other API error, never an HTML page."""
    return JsonResponse({"error": "not_found", "detail": "Not found."}, status=404)


def llms_txt(request):
    from django.conf import settings

    text = render_to_string("api/llms.txt", {"site_url": settings.SITE_URL.rstrip("/")})
    return HttpResponse(text, content_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# The public schema and the interactive docs
# ---------------------------------------------------------------------------
SCHEMA_PER_MINUTE = 30
_SCHEMA_CACHE: dict = {}  # the generated schema changes only with a deploy, so it is built once per process
_SCHEMA_CACHE_MAX = 16


class SchemaThrottle(BaseThrottle):
    """30 a minute per address on /api/v1/schema and /api/v1/docs, counted in the shared cache."""

    def allow_request(self, request, view):
        return not rate_limited("api-schema", address_bucket(client_ip(request)), SCHEMA_PER_MINUTE, 60)

    def wait(self):
        return 60


class ThrottledSchemaView(SpectacularAPIView):
    throttle_classes = [SchemaThrottle]

    def _get_schema_response(self, request):
        key = (request.GET.get("lang"), request.GET.get("version"), request.GET.get("format"))
        cached = _SCHEMA_CACHE.get(key)
        if cached is None:
            response = super()._get_schema_response(request)
            if len(_SCHEMA_CACHE) < _SCHEMA_CACHE_MAX:
                _SCHEMA_CACHE[key] = (response.data, dict(response.items()))
            return response
        data, headers = cached
        return Response(data=data, headers=headers)


class ThrottledDocsView(SpectacularSwaggerSplitView):
    throttle_classes = [SchemaThrottle]
