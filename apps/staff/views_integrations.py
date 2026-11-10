"""API tokens and webhook endpoints on the staff pages. A token or a webhook secret is shown once,
in the response to the POST that made it, and nowhere else: no redirect, no list or detail page."""
from urllib.parse import urlsplit

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.api.models import SCOPES, ApiToken, record_change
from apps.orgs.models import Organization
from apps.vault.ratelimit import rate_limited
from apps.webhooks import delivery as webhook_delivery
from apps.webhooks import events as webhook_events
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint

from . import stepup
from .views import staff_required

REFUSALS = {
    "scheme": "The address must start with https://.",
    "private_address": "The address resolves to a private or reserved network.",
    "unresolvable": "The host name could not be resolved.",
    "bad_url": "That is not a usable web address.",
    "port": "Only port 443 is allowed.",
    "endpoint_limit": "This organization already has the maximum of 10 active endpoints.",
}


def _org(request):
    pk = request.POST.get("org", "")
    return Organization.objects.filter(pk=int(pk)).first() if pk.isdecimal() else None


def _token_page(request, error="", status=200, form=None):
    rows = ApiToken.objects.select_related("org").order_by("org__slug", "-pk")
    return render(request, "staff/settings/token_list.html", {
        "rows": rows, "orgs": Organization.objects.order_by("slug"), "scopes": SCOPES, "error": error,
        "form": form or {"scopes": ["read"]}}, status=status)


@staff_required
def token_list(request):
    return _token_page(request)


def _scopes_text(scopes) -> str:
    return ", ".join(scopes or []) or "none"


@staff_required
@sensitive_post_parameters()
@require_POST
def token_new(request):
    if refused := stepup.refusal(request, reverse("staff:token_list")):
        return refused
    org = _org(request)
    name = request.POST.get("name", "").strip()
    scopes = [s for s in dict.fromkeys(request.POST.getlist("scopes"))]
    form = {"org": org.pk if org else "", "name": name, "scopes": scopes}
    if org is None:
        return _token_page(request, "Choose an organization.", 400, form)
    if not name or len(name) > 100:
        return _token_page(request, "Give the token a name of up to 100 characters.", 400, form)
    if not scopes:
        return _token_page(request, "Choose at least one scope.", 400, form)
    try:
        with transaction.atomic():
            token, plain = ApiToken.issue(org, name, scopes, actor=request.user, channel="staff")
            stepup.announce(request.user, "API token created",
                            f"Created API token “{token.name}” (#{token.pk}) for organization {org.slug} with the "
                            f"scopes {_scopes_text(token.scopes)}.", reverse("staff:token_list"))
    except ValueError as exc:
        return _token_page(request, str(exc), 400, form)
    return render(request, "staff/settings/token_created.html", {"token": token, "plain": plain})


@staff_required
@require_POST
def token_scopes(request, pk):
    if refused := stepup.refusal(request, reverse("staff:token_list")):
        return refused
    token = get_object_or_404(ApiToken.objects.select_related("org"), pk=pk)
    scopes = list(dict.fromkeys(request.POST.getlist("scopes")))
    if token.revoked_at:
        messages.error(request, "A revoked token cannot be changed.")
    elif not scopes or any(s not in SCOPES for s in scopes):
        messages.error(request, "Choose at least one of the known scopes; nothing was changed.")
    elif set(scopes) != set(token.scopes or []):
        before = list(token.scopes or [])
        with transaction.atomic():
            token.scopes = scopes
            token.save(update_fields=["scopes"])
            record_change(request.user, "api_token.scopes", token, {"scopes": scopes, "channel": "staff"})
            stepup.announce(request.user, "API token scopes changed",
                            f"Changed the scopes of API token “{token.name}” (#{token.pk}, organization "
                            f"{token.org.slug}) from {_scopes_text(before)} to {_scopes_text(scopes)}.",
                            reverse("staff:token_list"))
        messages.success(request, f"Scopes of {token.name} saved.")
    return redirect(reverse("staff:token_list"))


@staff_required
@require_POST
def token_revoke(request, pk):
    if refused := stepup.refusal(request, reverse("staff:token_list")):
        return refused
    token = get_object_or_404(ApiToken.objects.select_related("org"), pk=pk)
    if token.revoked_at is None:
        with transaction.atomic():
            token.revoke(actor=request.user, channel="staff")
            stepup.announce(request.user, "API token revoked",
                            f"Revoked API token “{token.name}” (#{token.pk}, organization {token.org.slug}).",
                            reverse("staff:token_list"))
        messages.success(request, f"Revoked {token.name}.")
    return redirect(reverse("staff:token_list"))


# ---------------------------------------------------------------- webhooks
def _host(endpoint) -> str:
    """The host only: a receiver's token often sits in the path or the query."""
    try:
        return urlsplit(endpoint.url).hostname or "(unreadable)"
    except ValueError:
        return "(unreadable)"


def _webhook_page(request, error="", status=200, form=None):
    rows = [{"e": e, "host": _host(e)} for e in
            WebhookEndpoint.objects.defer("secret").select_related("org").order_by("org__slug", "-pk")]
    return render(request, "staff/settings/webhook_list.html", {
        "rows": rows, "orgs": Organization.objects.order_by("slug"), "event_types": webhook_events.EVENT_TYPES,
        "error": error, "form": form or {}}, status=status)


@staff_required
def webhook_list(request):
    return _webhook_page(request)


@staff_required
@sensitive_post_parameters()
@require_POST
def webhook_new(request):
    org = _org(request)
    url = request.POST.get("url", "").strip()
    description = request.POST.get("description", "").strip()[:200]
    events = request.POST.getlist("events")
    form = {"org": org.pk if org else "", "url": url, "description": description, "events": events}
    if org is None:
        return _webhook_page(request, "Choose an organization.", 400, form)
    if len(url) > 2000:
        return _webhook_page(request, "The address is longer than 2000 characters.", 400, form)
    try:
        endpoint, plain = webhook_delivery.create_endpoint(
            org, url, events=events, description=description, actor=request.user)
    except webhook_delivery.WebhookRefused as exc:
        return _webhook_page(request, f"Refused ({exc.code}): {REFUSALS.get(exc.code, 'not an allowed destination.')}",
                             400, form)
    except ValueError as exc:
        return _webhook_page(request, str(exc), 400, form)
    return render(request, "staff/settings/webhook_created.html", {
        "endpoint": endpoint, "host": _host(endpoint), "plain": plain})


@staff_required
def webhook_detail(request, pk):
    endpoint = get_object_or_404(WebhookEndpoint.objects.defer("secret").select_related("org"), pk=pk)
    deliveries = list(WebhookDelivery.objects.filter(endpoint=endpoint).select_related("event").order_by("-pk")[:50])
    return render(request, "staff/settings/webhook_detail.html", {
        "endpoint": endpoint, "host": _host(endpoint), "deliveries": deliveries,
        "has_failed": any(d.state == "failed" for d in deliveries)})


@staff_required
@require_POST
def webhook_delete(request, pk):
    endpoint = get_object_or_404(WebhookEndpoint.objects.only("pk", "org_id"), pk=pk)
    webhook_delivery.delete_endpoint(endpoint, actor=request.user)
    messages.success(request, "Deleted the endpoint and its delivery log. The events are kept.")
    return redirect(reverse("staff:webhook_list"))


@staff_required
@require_POST
def webhook_ping(request, pk):
    endpoint = get_object_or_404(WebhookEndpoint.objects.only("pk", "org_id", "active"), pk=pk)
    if not endpoint.active:
        messages.error(request, "The endpoint is inactive.")
    elif rate_limited("staff-webhook-ping", str(pk), 6, 60):
        messages.error(request, "Too many pings for this endpoint; wait a minute and try again.")
    else:
        event = webhook_events.emit("ping", org=endpoint.org_id, endpoint=endpoint, data={"endpoint_id": endpoint.pk})
        queued = event is not None and WebhookDelivery.objects.filter(event=event, endpoint=endpoint).exists()
        if queued:
            messages.success(request, "A ping is queued; the worker sends it within about 15 seconds.")
        else:
            messages.error(request, "The ping could not be queued; retry shortly.")
    return redirect(reverse("staff:webhook_detail", args=[pk]))


@staff_required
@require_POST
def webhook_retry(request, pk):
    """Queue this endpoint's failed deliveries again: one when `delivery` names it, else all."""
    endpoint = get_object_or_404(WebhookEndpoint.objects.only("pk"), pk=pk)
    rows = WebhookDelivery.objects.filter(endpoint=endpoint)
    only = request.POST.get("delivery", "")
    if only.isdecimal():
        rows = rows.filter(pk=int(only))
    count = webhook_delivery.retry_now(rows.filter(state="failed"), actor=request.user)
    messages.success(request, f"Queued {count} delivery(ies) again.")
    return redirect(reverse("staff:webhook_detail", args=[pk]))
