"""Public pages reached through capability links: /i/<view token>/ and /p/<pay token>/.

The view page shows the invoice and its PDF, never payment details. The pay page shows the
same invoice plus where and how to pay, and carries the "I have paid" form. The token is the
only credential; it is looked up by SHA-256, never logged, and every response tells caches,
crawlers and cross-origin referrers to keep away from it.
(Referrer-Policy is same-origin, not no-referrer: with no-referrer browsers post forms with
`Origin: null`, which Django's CSRF check refuses.)
"""
import datetime
import functools
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_POST

from apps.core.i18n import activate_for
from apps.invoices import tokens
from apps.invoices.errors import InvoiceError
from apps.invoices import snapshot
from apps.invoices.currencies import minor_units
from apps.invoices import return_url
from apps.invoices.pdf import _context, _logo_uri, format_money, render_invoice_pdf
from apps.invoices.services import SETTLED
from apps.payments.matching import verify_declaration
from apps.payments.models import Payment
from apps.payments.services import TooManyDeclarations, declare_payment, remaining_minor, tell_staff_if_needed
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.quotes.services import QUOTE_REQUEST_BUDGET_S, create_quote
from apps.rails import http, uris
from apps.rails.networks import NETWORKS
from apps.vault.middleware import VaultGateMiddleware
from apps.vault.ratelimit import address_bucket, client_ip, rate_limited
from apps.vault.fields import UndecryptableValue
from apps.vault.vault import VaultSealed

from . import crypto
from .forms import DeclarationForm
from .qr import qr_data_uri

logger = logging.getLogger(__name__)
RATE_LIMIT = 60
RATE_WINDOW_S = 60
MISS_LIMIT = 30
QUOTE_LIMIT = 10
QUOTE_WINDOW_S = 600
# The link's own buckets, whoever sends the request (audit WEB-1, WEB-6): every "I have paid" is
# stored, audited and looked up on chain, and every PDF is rendered from scratch.
DECLARE_LIMIT = 5
DECLARE_WINDOW_S = 3600
PDF_LIMIT = 10
PDF_WINDOW_S = 600
PAYABLE = ("sent", "overdue", "partially_paid", "declared")


def _language(request, invoice=None) -> str:
    codes = {code for code, _name in settings.LANGUAGES}
    lang = request.GET.get("lang")
    if lang in codes:
        return lang
    if invoice is not None:
        if invoice.payment_link_id:  # a payment link's order: the language of its checkout, never the
            # contact's stored one (a public form never shows what a contact stores)
            from apps.links.models import LinkOrder

            order_lang = LinkOrder.objects.filter(invoice_id=invoice.pk).values_list("language", flat=True).first()
            if order_lang in codes:
                return order_lang
        return activate_for(invoice.contact)
    lang = translation.get_language_from_request(request)
    return lang if lang in codes else "en"


def _link_bucket(token: str) -> str:
    """What a link's rate limits are keyed on: never the token itself."""
    return tokens.hash_token(token)[:16]


def _harden(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex"
    return response


def portal(which):
    """Rate-limit, look the invoice up by token hash, render under the page language."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, token):
            try:
                return _serve(view, request, token)
            except VaultSealed:
                # Sealed after the gate let this request through (the gate answers the same page
                # before it): the snapshot, the contact and the lines all need the vault.
                return VaultGateMiddleware.unavailable(request)

        def _serve(view, request, token):
            ip = address_bucket(client_ip(request))  # an IPv6 address counts by its /64 (final review I4)
            invoice = tokens.resolve(token, which)
            if invoice is None:
                # Unknown tokens share ONE bucket per address, so guessing costs the caller
                # and creates no per-token counters.
                limited = rate_limited("portal-miss", ip, MISS_LIMIT, RATE_WINDOW_S)
            else:
                limited = rate_limited("portal", f"{ip}:{_link_bucket(token)}", RATE_LIMIT, RATE_WINDOW_S)
            if limited:
                with translation.override(_language(request, invoice)):
                    response = render(request, "portal/too_many.html", status=429)
                return _harden(response)
            if invoice is None:
                raise Http404
            lang = _language(request, invoice)
            with translation.override(lang):
                response = view(request, invoice, token, lang)
            return _harden(response)

        return wrapper

    return decorator


def _payment_phase(invoice) -> str:
    """What the client is told about money that has arrived but is not yet counted: "review"
    (a person is looking at it), "seen" (waiting for the chain), or "" for neither. Review wins:
    a payment in review must never read as "Awaiting payment"."""
    rows = list(Payment.objects.filter(invoice=invoice, state__in=("detected", "needs_review"))
                .values_list("state", "needs_staff_confirmation"))
    if any(state == "needs_review" or staff for state, staff in rows):
        return "review"
    return "seen" if rows else ""


def _state(invoice) -> dict:
    # An open declaration wins over the status: after "I have paid" on a partially paid
    # invoice, asking for the remainder again invites a double payment. It never unsettles an
    # invoice that is already paid, and a payment seen or in review says so in its own words.
    has_open = invoice.declarations.filter(state="open").exists()
    token_status = invoice.status
    phase = ""
    if invoice.status != "void" and invoice.status not in SETTLED:
        phase = _payment_phase(invoice)
        if has_open:
            token_status = "declared"
    return {"invoice": invoice, "is_void": invoice.status == "void", "token_status": token_status,
            "has_open_declaration": has_open, "payment_phase": phase}


def identity(org, name=None) -> dict:
    """Who the client is dealing with, for the head of every client page: the issuing
    organization's name and its logo (a data: URI, None without one). The system has no brand."""
    if name is None:
        try:
            name = org.legal_name
        except (VaultSealed, UndecryptableValue):
            name = None
    return {"org_name": name or org.slug, "org_logo": _logo_uri(org)}


def invoice_identity(invoice) -> dict:
    """The issuer as the invoice states it: the name frozen at send, the logo as it is now."""
    return identity(invoice.org, snapshot.for_render(invoice)["seller"].get("legal_name"))


def _page_context(request, invoice, token, lang) -> dict:
    ctx = _context(invoice, lang)
    ctx.update(_state(invoice))
    ctx.update({"org_name": ctx["seller"].get("legal_name") or invoice.org.slug, "org_logo": ctx["logo_uri"]})
    for m in ctx["methods"]:  # a link is offered only if it is a web address
        url = (m.get("url") or "").strip()
        m["safe_url"] = url if url.lower().startswith(("https://", "http://")) else ""
    ctx.update({"back_link": _back_link(invoice), "token": token, "page_lang": lang, "is_link_order": bool(invoice.payment_link_id),
                "lang_query": f"?lang={lang}" if "lang" in request.GET else ""})
    return ctx


def _back_link(invoice):
    """The Back button's target: the stored return_url, shown by its host alone. `host_of` checks the
    value's shape again here (https, a host, no credentials, port 443 or none, no whitespace, within the length limit), so a malformed value written
    by hand is never rendered as a link. It does not check the issuer's allow-list: that is done
    when the address is stored, and a host removed from the list later keeps its stored links."""
    host = return_url.host_of(invoice.return_url)
    if not host:
        return None
    return {"url": invoice.return_url, "host": host, "settled": invoice.status in SETTLED}


@require_GET
@portal("view")
def invoice_page(request, invoice, token, lang):
    template = "portal/void.html" if invoice.status == "void" else "portal/invoice.html"
    return render(request, template, _page_context(request, invoice, token, lang))


@require_GET
@portal("view")
def invoice_pdf(request, invoice, token, lang):
    if rate_limited("portal-pdf", _link_bucket(token), PDF_LIMIT, PDF_WINDOW_S):
        return render(request, "portal/too_many.html",
                      {"message": _("Too many attempts. Wait a few minutes and try again.")}, status=429)
    if invoice.status == "void":
        return render(request, "portal/void.html", _page_context(request, invoice, token, lang))
    name = (invoice.number or f"request-{invoice.pk}").replace("/", "-").replace('"', "")
    response = HttpResponse(render_invoice_pdf(invoice, lang), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice-{name}.pdf"'
    return response


def _render_pay(request, invoice, token, lang, form=None, status=200, quote_ctx=None, declare_error=None):
    ctx = _page_context(request, invoice, token, lang)
    ctx["form"] = form or DeclarationForm(networks=crypto.offered_networks(invoice))
    ctx["declare_error"] = declare_error
    ctx["crypto_options"] = crypto.options(invoice)
    ctx["quote_ctx"] = quote_ctx
    # What is still owed: the full total until a payment has been confirmed.
    ctx["amount_due"] = format_money(remaining_minor(invoice), invoice.currency)
    # The same amount as a plain number, for the bank transfer's copy button (a bank form wants digits).
    ctx["amount_due_plain"] = format(Decimal(remaining_minor(invoice)).scaleb(-minor_units(invoice.currency)), "f")
    ctx["can_pay"] = invoice.status in PAYABLE
    ctx["can_declare"] = invoice.status in ("sent", "overdue", "partially_paid", "declared")
    # The pay page's method tabs: bank transfers, online payment links, crypto.
    ctx["bank_methods"] = [m for m in ctx["methods"] if m.get("kind") == "bank"]
    ctx["link_methods"] = [m for m in ctx["methods"] if m.get("kind") != "bank"]
    template = "portal/void.html" if invoice.status == "void" else "portal/pay.html"
    return render(request, template, ctx, status=status)


@require_GET
@portal("pay")
def pay_page(request, invoice, token, lang):
    return _render_pay(request, invoice, token, lang)


@require_GET
@portal("pay")
def status(request, invoice, token, lang):
    return render(request, "portal/_status.html", _state(invoice))


QUOTE_ERROR_TEXT = {
    "not_payable": gettext_lazy("This invoice can no longer be paid online."),
    "method_not_offered": gettext_lazy("This payment method is not available for this invoice."),
    "asset_not_enabled": gettext_lazy("That network or token is not available for this invoice."),
    "nothing_due": gettext_lazy("Nothing is left to pay on this invoice."),
    "rates_unavailable": gettext_lazy("Exchange rates are unavailable right now. Try again in a minute."),
    "rates_diverge": gettext_lazy("The exchange rate cannot be fixed reliably right now. Try again in a minute."),
    # Every network of one address shares its amounts (audit MONEY-6): another network is no way out.
    "no_free_amount": gettext_lazy("Too many payments are open right now. Please try again in a few minutes."),
    # One invoice holds at most QUOTES_PER_INVOICE quotes, each kept a day after it closes (residual R1).
    "too_many_quotes": gettext_lazy(
        "There have been too many payment attempts on this invoice today. Please try again tomorrow, or contact us."),
    "rail_unavailable": gettext_lazy("This payment method is temporarily unavailable. Try another one, or try again shortly."),
}
QUOTE_ERROR_STATUS = {"not_payable": 409, "nothing_due": 409, "no_free_amount": 409, "too_many_quotes": 409,
                      "rates_unavailable": 503,
                       "rates_diverge": 503, "rail_unavailable": 503}


# The schemes the rails write into a quote's payment URI: ethereum: (EVM chains), solana:, ton://,
# lightning:, and what BTCPay hands back for BTC, XMR and ZEC (bitcoin:, monero:, zcash:). A gateway's
# answer is not trusted to be one of them: anything else (javascript:, data:, a web page) is no link.
WALLET_SCHEMES = frozenset({"ethereum", "solana", "ton", "bitcoin", "lightning", "monero", "zcash"})


def wallet_link(uri: str) -> str:
    """`uri` when it opens a wallet (its scheme is one the rails produce), else ""."""
    scheme, sep, _rest = (uri or "").partition(":")
    return uri if sep and scheme.lower() in WALLET_SCHEMES else ""


def web_link(url: str) -> str:
    """`url` when it is a web address (a gateway's checkout page), else ""."""
    return url if (url or "").lower().startswith(("https://", "http://")) else ""


def _quote_view(quote: Quote) -> dict:
    """What the quote partial shows. Amounts are exact: the base units, written out in whole units."""
    decimals = crypto.decimals_for(quote.network, quote.asset)
    qr_text = quote.payment_uri or quote.address or quote.bolt11 or quote.checkout_url
    now = timezone.now()
    return {
        "quote": quote, "amount": uris.decimal_amount(int(quote.amount_base_units), decimals),
        "asset": quote.asset, "network_name": crypto.network_name(quote.network),
        "qr": qr_data_uri(qr_text) if qr_text else "", "expires_iso": quote.expires_at.isoformat(),
        "wallet_link": wallet_link(quote.payment_uri), "checkout_link": web_link(quote.checkout_url),
        "expires_utc": quote.expires_at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "expires_at": quote.expires_at,
        # The validity bar: how long the amount was held for, and how much of that is left.
        "valid_total_s": max(1, int((quote.expires_at - quote.issued_at).total_seconds())),
        "valid_left_s": max(0, int((quote.expires_at - now).total_seconds())),
    }


@require_POST
@portal("pay")
def quote(request, invoice, token, lang):
    """The only place a quote is created: an explicit press of a button, never a page view or a
    poll, because every quote holds a unique amount for about a day."""
    hx = request.headers.get("HX-Request") == "true"
    ctx, code = {}, 200
    # One bucket per address, across every invoice (final review I3): each order through a public link
    # is a new pay token, so a bucket per token let one address hold amounts without end.
    if rate_limited("portal-quote", address_bucket(client_ip(request)), QUOTE_LIMIT, QUOTE_WINDOW_S):
        ctx, code = {"error": _("Too many attempts. Wait a few minutes and try again.")}, 429
    else:
        try:
            account = crypto.account_for(invoice, request.POST.get("method", ""))
            with http.request_budget(QUOTE_REQUEST_BUDGET_S):  # three sync workers: never wait long here
                created = create_quote(invoice, account, request.POST.get("network", ""),
                                       request.POST.get("asset", ""), refresh=request.POST.get("refresh") == "1")
            ctx = _quote_view(created)
        except QuoteError as exc:
            ctx, code = {"error": QUOTE_ERROR_TEXT[exc.code]}, QUOTE_ERROR_STATUS.get(exc.code, 400)
    ctx.update({"token": token, "lang_query": f"?lang={lang}" if "lang" in request.GET else ""})
    if hx:
        # htmx swaps only successful answers; the sentence is the content either way.
        return render(request, "portal/_quote.html", ctx, status=200)
    return _render_pay(request, invoice, token, lang, status=code, quote_ctx=ctx)


EVM_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
VERIFY_BUDGET_S = 3
VERIFY_EVERY_S = 300


def _verify_now(invoice, declaration, network, tx_hash) -> None:
    """One short look at the named transaction while the client waits. The declaration is
    already saved, so nothing here may fail the request: whatever is slow, skipped or broken is
    left to the 300 s `declarations_verify` job."""
    if NETWORKS[network].family == "evm" and not EVM_HASH.match(tx_hash):
        return  # junk never goes out to a public RPC; staff still see the declaration
    if not cache.add(f"portal-verify:{invoice.pk}", 1, VERIFY_EVERY_S):
        return
    try:
        with http.request_budget(VERIFY_BUDGET_S):
            verify_declaration(declaration)
    except Exception as exc:  # noqa: BLE001 - after the save nothing may become a 500
        logger.info("in-request declaration check did not finish (%s)", type(exc).__name__)


ALREADY_DECLARED = gettext_lazy(
    "We already have your notice of payment and are checking it. You do not need to send it again.")


@require_POST
@portal("pay")
def declare(request, invoice, token, lang):
    if rate_limited("portal-declare", _link_bucket(token), DECLARE_LIMIT, DECLARE_WINDOW_S):
        return _render_pay(request, invoice, token, lang, status=429, declare_error=ALREADY_DECLARED)
    form = DeclarationForm(request.POST, networks=crypto.offered_networks(invoice))
    if not form.is_valid():
        return _render_pay(request, invoice, token, lang, form=form, status=400)
    network = form.cleaned_data["network"].strip()
    tx_hash = form.cleaned_data["tx_hash"].strip()
    try:
        declaration = declare_payment(invoice, network=network, tx_hash=tx_hash,
                                      note=form.cleaned_data["note"].strip())
    except TooManyDeclarations:
        return _render_pay(request, invoice, token, lang, status=409, declare_error=ALREADY_DECLARED)
    except InvoiceError:
        invoice.refresh_from_db()
        return _render_pay(request, invoice, token, lang, status=409)
    if tx_hash and network in NETWORKS:
        _verify_now(invoice, declaration, network, tx_hash)
        try:  # found, but the money is another invoice's: staff are told now, not in half an hour
            tell_staff_if_needed(declaration)
        except Exception as exc:  # noqa: BLE001 - after the save nothing may become a 500; the job asks again
            logger.info("declaration letter check did not finish (%s)", type(exc).__name__)
    suffix = f"?lang={lang}" if "lang" in request.GET else ""
    return redirect(f"/p/{token}/{suffix}")
