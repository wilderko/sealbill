"""Staff pages of payment links, under /links/ (English, like every staff page).

The list, a link's create and edit form, and its page: the URL to share (the token is kept encrypted for
this, 2.3a item 4), its coupons, its orders and the ones to fulfil. Every change is audited by the
service it goes through. Sharing (QR as PNG and SVG, a printable sheet, a button snippet) is `sharing.py`.
"""
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.invoices.money import totals
from apps.portal.qr import qr_data_uri
from apps.invoices.pdf import format_money
from apps.invoices.services import SETTLED
from apps.staff.orgnames import org_name, org_names
from apps.staff.views import Section, staff_required
from apps.vault.fields import UndecryptableValue

from . import methods as method_types
from . import services, sharing
from .forms import CouponForm, LinkForm, initial_for
from .models import Coupon, LinkAlert, LinkOrder, PaymentLink

PAID = SETTLED


def order_ref(invoice) -> str:
    return invoice.number or f"Payment request #{invoice.pk}"


def _status_words(link, now) -> str:
    reason = services.unavailable_reason(link, now=now)
    return {"inactive": "Inactive", "expired": "Expired", "used_up": "Used up"}.get(reason, "Active")


def _price_text(link) -> str:
    if not link.customer_chooses:
        return format_money(link.price_minor, link.currency)
    lo = format_money(link.min_minor, link.currency) if link.min_minor is not None else ""
    hi = format_money(link.max_minor, link.currency) if link.max_minor is not None else ""
    if lo and hi:
        return f"{lo} to {hi}, buyer chooses"
    if lo or hi:
        return f"{'At least ' + lo if lo else 'Up to ' + hi}, buyer chooses"
    return "Buyer chooses"


def _title(link) -> str:
    try:
        return link.title or f"Payment link #{link.pk}"
    except UndecryptableValue:
        return f"Payment link #{link.pk} (title cannot be decrypted)"


@staff_required
def link_list(request):
    now = timezone.now()
    names = org_names()
    rows = (PaymentLink.objects.order_by("-created_at", "-pk")
            .annotate(orders_total=Count("invoices", distinct=True),
                      orders_paid=Count("invoices", filter=Q(invoices__status__in=PAID), distinct=True),
                      to_fulfil=Count("invoices", distinct=True, filter=Q(
                          fulfillment=True, invoices__status__in=PAID,
                          invoices__link_order__fulfilled_at__isnull=True)
                          & ~Q(invoices__link_order__in=LinkOrder.objects.filter(services.held_for_us_review())))))

    def decorate(link):
        link.title_text = _title(link)
        link.price_text = _price_text(link)
        link.status_text = _status_words(link, now)
        link.org_name = names.get(link.org_id, "")

    return render(request, "staff/links/list.html", {"links": Section(rows, decorate)})


def _form_page(request, form, link=None, error=None, status=200):
    return render(request, "staff/links/form.html", {"form": form, "link": link, "error": error,
                                                     "has_image": link is not None and hasattr(link, "image")},
                  status=status)


@require_http_methods(["GET", "POST"])
@staff_required
def link_new(request):
    if request.method == "GET":
        first = org_names()
        form = LinkForm(initial={"organization": next(iter(first), None), "currency": "EUR", "max_quantity": 1,
                                 "active": True, "kind": "invoice", "methods": "default"})
        return _form_page(request, form)
    form = LinkForm(request.POST, request.FILES)
    if not form.is_valid():
        return _form_page(request, form, status=400)
    try:
        link, _plain = services.create_link(form.cleaned_data["organization"], actor=request.user,
                                            image=request.FILES.get("image"), **form.link_fields())
    except services.LinkError as exc:
        return _form_page(request, form, error=str(exc), status=400)
    return redirect("staff:link_detail", pk=link.pk)


@require_http_methods(["GET", "POST"])
@staff_required
def link_edit(request, pk):
    link = get_object_or_404(PaymentLink.objects.select_related("org"), pk=pk)
    if request.method == "GET":
        return _form_page(request, LinkForm(initial=initial_for(link), link=link), link)
    form = LinkForm(request.POST, request.FILES, initial=initial_for(link), link=link)
    if not form.is_valid():
        return _form_page(request, form, link, status=400)
    try:
        from django.db import transaction

        with transaction.atomic():
            services.update_link(link, actor=request.user, **form.link_fields())
            if form.cleaned_data.get("remove_image"):
                services.remove_image(link, actor=request.user)
            elif request.FILES.get("image"):
                services.set_image(link, request.FILES["image"], actor=request.user)
    except services.LinkError as exc:
        return _form_page(request, form, link, error=str(exc), status=400)
    return redirect("staff:link_detail", pk=link.pk)


def _decorate_order(order) -> None:
    invoice = order.invoice
    order.ref = order_ref(invoice)
    order.total_text = format_money(totals(invoice).total, invoice.currency)
    try:
        order.buyer = ((invoice.snapshot or {}).get("buyer") or {}).get("name") or ""
        order.email_text = order.email or ""
    except UndecryptableValue:
        order.buyer, order.email_text = "(cannot be decrypted)", ""
    order.is_paid = invoice.status in PAID
    order.held = order.is_paid and LinkOrder.objects.filter(services.held_for_us_review(), pk=order.pk).exists()


def detail_context(link, *, coupon_form=None, error=None):
    now = timezone.now()
    try:
        url = link.url_for(link.token) if link.token else ""
    except UndecryptableValue:
        url = ""
    orders = (LinkOrder.objects.filter(invoice__payment_link=link).select_related("invoice", "fulfilled_by")
              .order_by("-created_at", "-pk"))
    to_fulfil = services.to_fulfil(orders) if link.fulfillment else orders.none()
    coupons = []
    for coupon in link.coupons.order_by("code"):
        coupon.uses = services.coupon_uses_taken(coupon, now=now)
        coupon.percent_text = f"{coupon.percent_bps / 100:.2f}".rstrip("0").rstrip(".")
        coupon.state_text = ("Off" if not coupon.active else "Expired" if coupon.valid_until and coupon.valid_until <= now
                             else "Used up" if coupon.max_uses is not None and coupon.uses >= coupon.max_uses else "On")
        coupon.share_url = f"{url}?promoCode={coupon.code}" if url else ""
        coupons.append(coupon)
    custom = link.custom_fields or []
    return {
        "link": link, "title": _title(link), "url": url, "org_name": org_name(link.org),
        "status_text": _status_words(link, now), "price_text": _price_text(link),
        "tax_text": f"{link.tax_rate_bps / 100:.2f}".rstrip("0").rstrip(".") if link.tax_rate_bps else "",
        "uses": services.uses_taken(link, now=now), "image": services.image_uri(link),
        "method_text": ", ".join(method_types.label(t) for t in link.method_types) if link.methods == "custom"
        else "The issuer's defaults",
        "qr_preview": qr_data_uri(url) if url else "",
        "snippet": sharing.button_snippet(url, _title(link)) if url else "",
        "custom": custom, "coupons": coupons, "coupon_form": coupon_form or CouponForm(),
        "orders": Section(orders, _decorate_order), "to_fulfil": Section(to_fulfil, _decorate_order),
        "error": error,
    }


@staff_required
def link_detail(request, pk):
    link = get_object_or_404(PaymentLink.objects.select_related("org"), pk=pk)
    return render(request, "staff/links/detail.html", detail_context(link))


def _shared_url(pk):
    """(link, its URL) or 404: a link whose stored token cannot be opened has nothing to hand out."""
    link = get_object_or_404(PaymentLink.objects.select_related("org"), pk=pk)
    try:
        url = link.url_for(link.token) if link.token else ""
    except UndecryptableValue:
        url = ""
    if not url:
        raise Http404("no URL")
    return link, url


def _download(body: bytes, content_type: str, name: str, disposition: str = "attachment") -> HttpResponse:
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'{disposition}; filename="{name}"'
    response["Cache-Control"] = "no-store"  # the URL is the capability
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_required
def link_qr_png(request, pk):
    link, url = _shared_url(pk)
    return _download(sharing.qr_png(url), "image/png", sharing.filename(link, "png"))


@staff_required
def link_qr_svg(request, pk):
    link, url = _shared_url(pk)
    return _download(sharing.qr_svg(url), "image/svg+xml", sharing.filename(link, "svg"))


@staff_required
def link_sheet_pdf(request, pk):
    link, url = _shared_url(pk)
    return _download(sharing.sheet_pdf(_title(link), url), "application/pdf", sharing.filename(link, "pdf"),
                     disposition="inline")


@require_POST
@staff_required
def link_rotate(request, pk):
    link = get_object_or_404(PaymentLink.objects.only("pk"), pk=pk)
    services.rotate_token(link, actor=request.user)
    return redirect("staff:link_detail", pk=pk)


@require_POST
@staff_required
def coupon_add(request, pk):
    link = get_object_or_404(PaymentLink.objects.select_related("org"), pk=pk)
    form = CouponForm(request.POST)
    if form.is_valid():
        cd = form.cleaned_data
        try:
            services.add_coupon(link, code=cd["code"], percent_bps=cd["percent"], name=cd["name"],
                                max_uses=cd.get("max_uses"), valid_until=cd.get("valid_until"),
                                internal_note=cd["internal_note"], actor=request.user)
            return redirect("staff:link_detail", pk=pk)
        except services.LinkError as exc:
            form.add_error("code", str(exc))
    return render(request, "staff/links/detail.html", detail_context(link, coupon_form=form), status=400)


@require_POST
@staff_required
def coupon_toggle(request, pk):
    coupon = get_object_or_404(Coupon, pk=pk)
    services.set_coupon_active(coupon, not coupon.active, actor=request.user)
    return redirect("staff:link_detail", pk=coupon.link_id)


@require_POST
@staff_required
def order_fulfil(request, pk):
    order = get_object_or_404(LinkOrder.objects.select_related("invoice"), pk=pk)
    try:
        services.mark_fulfilled(order, request.user)
    except services.LinkError as exc:
        link = PaymentLink.objects.select_related("org").get(pk=order.invoice.payment_link_id)
        return render(request, "staff/links/detail.html", detail_context(link, error=str(exc)), status=409)
    back = request.POST.get("next", "")
    if back == "invoice":
        return redirect("staff:invoice", pk=order.invoice_id)
    return redirect("staff:link_detail", pk=order.invoice.payment_link_id)


@require_POST
@staff_required
def alert_resolve(request, pk):
    alert = get_object_or_404(LinkAlert, pk=pk)
    try:
        services.resolve_alert(alert, request.user, request.POST.get("note", ""))
    except services.LinkError:
        pass  # resolved meanwhile: nothing left to do
    return redirect("staff:needs_person")
