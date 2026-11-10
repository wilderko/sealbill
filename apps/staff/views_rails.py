"""Rail accounts on the staff pages: list, create, edit, activate. Accounts are deactivated, never
deleted: a client may still pay a quote on one."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.orgs.models import Organization
from apps.rails import credentials as rail_credentials
from apps.rails import http
from apps.rails.models import CHAIN_KINDS, RailAccount, WatcherHealth
from apps.rails.networks import NETWORKS

from . import stepup
from .forms_settings import RailAccountForm, url_host
from .views import staff_required
from .views_settings import audited_save


def _watcher_names(acct) -> list[str]:
    if acct.kind in CHAIN_KINDS:
        names = []
        for net in acct.networks or []:
            family = NETWORKS[net].family if net in NETWORKS else acct.kind
            names += [f"scan:{family}:{net}:{acct.pk}", f"ingest:{family}:{net}:{acct.pk}"]
        return names
    return [f"{acct.kind}:{acct.pk}"]


def _health(accounts) -> dict[int, str]:
    rows = {w.name: w for w in WatcherHealth.objects.all()}
    out = {}
    for acct in accounts:
        found = [rows[n] for n in _watcher_names(acct) if n in rows]
        if any(w.failing_since for w in found):
            out[acct.pk] = "failing"
        elif found:
            out[acct.pk] = "ok"
        else:
            out[acct.pk] = "no data"
    return out


def _save_keeping_credentials(request, form):
    """Save an edit of a rail account without ever writing `credentials` back from a stale read: a
    credential save or webhook registration may have committed while the form was open, and the
    form's save writes every column. The row is locked and its current credentials carried over.

    A credential belongs to the service it was issued by. When the host of the URL changes, the stored
    credentials are cleared and the account is deactivated in the same transaction: otherwise pressing
    Register or running a check would send the old key to whoever answers at the new address."""
    with transaction.atomic():
        current = RailAccount.objects.select_for_update(no_key=True).get(pk=form.instance.pk)
        form.instance.credentials = current.credentials
        extra = []
        before, after = (current.config or {}).get("url"), (form.instance.config or {}).get("url")
        if current.credentials and before and url_host(before) != url_host(after):
            form.instance.credentials = None
            extra.append("credentials")
            if form.instance.active or current.active:
                form.instance.active = False
                extra.append("active")
            request._credentials_cleared = True
        return audited_save(request, form, "railaccount", extra_fields=extra)


def _rail_text(acct) -> str:
    """A rail account by its label, id, kind and organization: never an address or a credential."""
    return f"rail account “{acct.label or '(no label)'}” (#{acct.pk}, {acct.kind}) of organization {acct.org.slug}"


def _announce(request, change, what, acct):
    stepup.announce(request.user, change, what, reverse("staff:rail_edit", args=[acct.pk]))


@staff_required
def rail_list(request):
    accounts = list(RailAccount.objects.select_related("org").order_by("org__slug", "kind", "label", "pk"))
    health = _health(accounts)
    rows = [{"a": a, "health": health[a.pk], "has_credentials": bool(a.credentials), "takes_credentials": a.kind not in CHAIN_KINDS} for a in accounts]
    return render(request, "staff/settings/rail_list.html", {"rows": rows})


@staff_required
def rail_new(request):
    kind = request.GET.get("kind", "")
    org = Organization.objects.filter(pk=request.GET.get("org") or 0).first() if str(request.GET.get("org", "")).isdigit() else None
    if kind not in RailAccount.Kind.values or org is None:
        return render(request, "staff/settings/rail_form.html", {
            "chooser": True, "orgs": Organization.objects.order_by("slug"), "kinds": RailAccount.Kind.choices,
            "kind": kind, "org_pk": request.GET.get("org", "")})
    if request.method == "POST" and (refused := stepup.refusal(request, request.get_full_path())):
        return refused
    obj = RailAccount(org=org, kind=kind)
    form = RailAccountForm(kind, request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved, _changed = audited_save(request, form, "railaccount")
            _announce(request, "rail account created", f"Created {_rail_text(saved)}.", saved)
        messages.success(request, "Saved the rail account.")
        return redirect(reverse("staff:rail_list"))
    return render(request, "staff/settings/rail_form.html", {"form": form, "acct": None, "org": org, "kind": kind})


@staff_required
def rail_edit(request, pk):
    acct = get_object_or_404(RailAccount.objects.select_related("org"), pk=pk)
    if request.method == "POST" and (refused := stepup.refusal(request, request.path)):
        return refused
    form = RailAccountForm(acct.kind, request.POST or None, instance=acct)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved, changed = _save_keeping_credentials(request, form)
            if changed:
                cleared = " The URL moved to another host, so its credentials were cleared and it was deactivated." \
                    if getattr(request, "_credentials_cleared", False) else ""
                _announce(request, "rail account changed",
                          f"Changed {_rail_text(saved)}. Fields saved: {', '.join(sorted(form.changed_data)) or 'none'}."
                          f"{cleared}", saved)
        if getattr(request, "_credentials_cleared", False):
            messages.warning(request, "The URL now points at another host, so the stored credentials were cleared "
                                      "and the account was deactivated. Set the credentials again, then activate it.")
            return redirect(reverse("staff:rail_credentials", args=[acct.pk]))
        messages.success(request, "Saved the rail account." if changed else "No changes.")
        return redirect(reverse("staff:rail_list"))
    return render(request, "staff/settings/rail_form.html", {"form": form, "acct": acct, "org": acct.org, "kind": acct.kind,
        "has_credentials_page": acct.kind in ("btcpay", "lightning")})


@staff_required
@require_POST
def rail_toggle(request, pk):
    if refused := stepup.refusal(request, reverse("staff:rail_edit", args=[pk])):
        return refused
    acct = get_object_or_404(RailAccount.objects.select_related("org"), pk=pk)
    was_active = acct.active
    acct.active = not acct.active
    try:
        with transaction.atomic():
            acct.save(update_fields=["active"])
            audit.record(request.user, "railaccount.update", acct, {"fields": ["active"], "via": "staff"})
            state = "Activated" if acct.active else "Deactivated"
            _announce(request, f"rail account {state.lower()}", f"{state} {_rail_text(acct)}.", acct)
    except ValidationError as exc:
        acct.active = was_active
        messages.error(request, " ".join(exc.messages))
        return redirect(reverse("staff:rail_edit", args=[pk]))
    messages.success(request, f"{'Activated' if acct.active else 'Deactivated'} {acct}.")
    return redirect(reverse("staff:rail_list"))


# Per call. The worst webhook path is five calls (permissions, list, POST unavailable, re-list, DELETE):
# about 25 s by call count. `requests` applies its timeout per connect and per read, so a slow trickle
# can in principle exceed it; gunicorn's 30 s is the backstop.
WEBHOOK_BUDGET_S = 5
CREDENTIAL_BUDGET_S = 15  # below gunicorn's 30 s timeout: a locked Alby Hub must answer, not be killed
UI_WORDING = (
    ("run again with --lnbits-key-is-invoice-key", "tick 'This is the Invoice/read key' and save again"),
    ("set config.url and config.store_id first", "set the BTCPay URL and store id on the account first"),
)


def _ui_message(text: str) -> str:
    for cli, ui in UI_WORDING:
        text = text.replace(cli, ui)
    return text


def _credential_rows(acct):
    """(name, is set) per credential field. Never a value."""
    stored = acct.credentials or {}
    return [{"name": n, "is_set": bool(stored.get(n))} for n in rail_credentials.fields_for(acct)]


def _credentials_page(request, acct, error=""):
    try:
        rows = _credential_rows(acct)
    except rail_credentials.CredentialRefused as exc:
        if acct.kind in CHAIN_KINDS:
            raise Http404 from None
        rows, error = [], error or _ui_message(exc.message)
    backend = (acct.config or {}).get("backend") if acct.kind == "lightning" else None
    return render(request, "staff/settings/rail_credentials.html", {
        "acct": acct, "rows": rows, "error": error, "lnbits": backend == "lnbits",
        "can_register_webhook": acct.kind == "btcpay" and bool((acct.credentials or {}).get("api_key"))})


@staff_required
@sensitive_post_parameters("api_key", "webhook_secret", "nwc_url", "macaroon", "rune", "invoice_key")
def rail_credentials_view(request, pk):
    acct = get_object_or_404(RailAccount.objects.select_related("org"), pk=pk)
    if acct.kind in CHAIN_KINDS:
        raise Http404
    if request.method != "POST":
        return _credentials_page(request, acct)
    if refused := stepup.refusal(request, request.path):
        return refused
    try:
        names = rail_credentials.fields_for(acct)
        given = {n: (request.POST.get(n) or "").strip() for n in names}
        with http.request_budget(CREDENTIAL_BUDGET_S):
            saved = rail_credentials.set_credentials(
                acct, given, actor=request.user, lnbits_vouched=bool(request.POST.get("lnbits_vouched")))
    except rail_credentials.CredentialRefused as exc:
        return _credentials_page(request, acct, _ui_message(exc.message))
    if saved:  # names only, never a value
        _announce(request, "rail credentials set", f"Set the credentials {', '.join(saved)} of {_rail_text(acct)}.", acct)
    messages.success(request, f"Saved {', '.join(saved)}." if saved else "Nothing changed.")
    return redirect(reverse("staff:rail_credentials", args=[pk]))


@staff_required
@require_POST
def rail_btcpay_webhook(request, pk):
    if refused := stepup.refusal(request, reverse("staff:rail_credentials", args=[pk])):
        return refused
    acct = get_object_or_404(RailAccount.objects.select_related("org"), pk=pk, kind="btcpay")
    try:
        with http.request_budget(WEBHOOK_BUDGET_S):
            rail_credentials.register_btcpay_webhook(acct, actor=request.user)
    except rail_credentials.CredentialRefused as exc:
        messages.error(request, _ui_message(exc.message))
    else:
        _announce(request, "BTCPay webhook registered",
                  f"Registered the BTCPay webhook of {_rail_text(acct)} and stored its secret.", acct)
        messages.success(request, "Registered the webhook in BTCPay and stored its secret.")
    return redirect(reverse("staff:rail_credentials", args=[pk]))
