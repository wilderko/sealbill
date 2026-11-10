"""The public page of a payment link, `/l/<token>/`: the offer and its form (GET), the checkout (POST).

The token is the only credential, looked up by SHA-256 and cut out of every logged path
(`apps.core.logfilters`, from `client_pages.CAPABILITY_PREFIXES`); every answer is uncacheable,
unindexed and kept from cross-origin referrers, and no page may frame it (the site-wide
`frame-ancestors 'none'`: no iframe embed, spec 2.3). Nothing is e-mailed from here: the buyer is sent
straight to the order's pay page.
"""
import functools

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.core.client_pages import harden
from apps.invoices import tokens
from apps.invoices.pdf import format_money
from apps.portal.views import identity
from apps.vault.fields import UndecryptableValue
from apps.vault.middleware import VaultGateMiddleware
from apps.vault.ratelimit import address_bucket, attempts_so_far, client_ip, count_attempt, rate_limited
from apps.vault.vault import VaultSealed

from . import services
from .forms import CHECKOUT_ERRORS, CheckoutForm

PAGE_LIMIT, PAGE_WINDOW_S = 60, 60  # page views per address and link, as on the pay page
MISS_LIMIT = 30  # unknown tokens: one bucket per address, so guessing costs the guesser
CHECKOUT_PER_ADDRESS, CHECKOUT_ADDRESS_WINDOW_S = 5, 600  # spec 2.3: 5 per 10 minutes per address
CHECKOUT_PER_LINK, CHECKOUT_LINK_WINDOW_S = 50, 3600  # and 50 completed checkouts per hour per link


def _language(request) -> str:
    codes = {code for code, _name in settings.LANGUAGES}
    lang = request.GET.get("lang")
    if lang in codes:
        return lang
    lang = translation.get_language_from_request(request)
    return lang if lang in codes else "en"


def _sealed_safe(view):
    @functools.wraps(view)
    def wrapper(request, token):
        try:
            return view(request, token)
        except VaultSealed:  # sealed after the gate let this request through
            return VaultGateMiddleware.unavailable(request)

    return wrapper


@require_http_methods(["GET", "POST"])
@_sealed_safe
def link_page(request, token):
    ip = address_bucket(client_ip(request))  # an IPv6 address counts by its /64 (final review I4)
    link = services.resolve_token(token)
    lang = _language(request)
    if link is None:
        limited = rate_limited("link-miss", ip, MISS_LIMIT, PAGE_WINDOW_S)
    else:
        limited = rate_limited("link-page", f"{ip}:{tokens.hash_token(token)[:16]}", PAGE_LIMIT, PAGE_WINDOW_S)
    with translation.override(lang):
        if limited:
            return harden(render(request, "portal/too_many.html", status=429))
        if link is None:
            raise Http404
        # Every POST counts toward the address; only a completed checkout toward the link (final review
        # m4: refused forms from anywhere must not use up a link's hour for its buyers). See `_serve`.
        if request.method == "POST" and (
                rate_limited("link-checkout-address", ip, CHECKOUT_PER_ADDRESS, CHECKOUT_ADDRESS_WINDOW_S)
                or attempts_so_far("link-checkout", str(link.pk)) >= CHECKOUT_PER_LINK):
            return harden(render(request, "portal/too_many.html",
                                 {"message": _("Too many attempts. Wait a few minutes and try again."),
                                  **identity(link.org)}, status=429))
        return harden(_serve(request, link, lang))


def _gone(request, link):
    return render(request, "links/unavailable.html", identity(link.org), status=410)


def _serve(request, link, lang):
    if services.unavailable_reason(link):
        return _gone(request, link)
    promo = (request.GET.get("promoCode") or "").strip()[:40]
    form = CheckoutForm(request.POST or None, link=link, initial={"coupon": promo})
    status, error = 200, ""
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                _invoice, pay = services.checkout(link, form.order(), language=lang)
                count_attempt("link-checkout", str(link.pk), CHECKOUT_LINK_WINDOW_S)
            except services.CheckoutError as exc:
                if exc.code == "unavailable":
                    return _gone(request, link)
                if exc.code in ("no_methods", "no_us_issuer"):
                    status = 503
                    # Two sentences, each its own msgid: xgettext does not look inside an f-string (final review m1).
                    error = " ".join((_("This payment link cannot be paid right now."),
                                      _("Please try again in a few minutes.")))
                elif exc.code in ("amount_low", "amount_high") and "amount" in form.fields:
                    form.add_error("amount", form.amount_message(exc.code))
                elif exc.code in CHECKOUT_ERRORS and CHECKOUT_ERRORS[exc.code][0] in form.fields:
                    field, message = CHECKOUT_ERRORS[exc.code]
                    form.add_error(field, message or form.fields[field].error_messages["max_value"])
                else:
                    error = _("This order cannot be placed. Check the details and try again.")
            else:
                suffix = f"?lang={lang}" if "lang" in request.GET else ""
                return redirect(f"/p/{pay}/{suffix}")
    return render(request, "links/page.html", {**_page(link), "form": form, "error": error,
                                               "page_lang": lang}, status=status)


def _page(link) -> dict:
    try:
        title, description = link.title, link.description
    except UndecryptableValue:
        title, description = "", ""
    ctx = {"link": link, "title": title, "description": description, "image": services.image_uri(link),
           **identity(link.org)}
    if link.customer_chooses:
        ctx["price"] = ""
    else:
        ctx["price"] = format_money(link.price_minor, link.currency)
    ctx["tax_percent"] = _percent(link.tax_rate_bps) if link.tax_rate_bps else ""
    # `?promoCode=` only fills in the field. Whether the code is good is said by the POST alone, which
    # counts against the checkout limits: a page that confirmed it would let anyone test codes at the
    # page-view rate.
    return ctx


def _percent(bps: int) -> str:
    """2050 basis points as "20.5 %", with the decimal separator of the page's language."""
    from decimal import Decimal

    from django.utils.formats import number_format

    value = Decimal(bps) / 100
    places = 0 if value == value.to_integral() else 2 if bps % 10 else 1
    return f"{number_format(value, decimal_pos=places, use_l10n=True)} %"
