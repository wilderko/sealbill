"""The Settings area of the staff pages: the index, and the editors it links to."""
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.orgs.models import BankAccount, FiatLink, Organization

from . import stepup
from .forms_settings import BankAccountForm, FiatLinkForm, OrganizationForm
from .views import staff_required

# The organization's fields that decide who is billed by whom and where a client is sent: a change to
# either needs the step-up (audit SEC-3). The rest of the form stays open to staff.
STEP_UP_ORG_FIELDS = ("is_us_resident_issuer", "return_url_hosts")


def audited_save(request, form, prefix: str, extra_fields=()):
    """Save a bound, valid ModelForm and record `<prefix>.create` or `<prefix>.update` in one
    transaction, naming the fields that changed and never their values (an IBAN or an address must
    not be copied into the log). Returns `(object, changed)`: `changed` is False for an edit that
    changed nothing, so the page can say "No changes."."""
    creating = form.instance.pk is None
    fields = sorted({*form.changed_data, *extra_fields})
    if not creating and not fields:
        return form.instance, False  # an edit that changes nothing is neither saved nor audited
    with transaction.atomic():
        obj = form.save()
        audit.record(request.user, f"{prefix}.{'create' if creating else 'update'}", obj,
                     {"fields": fields, "via": "staff"})
    return obj, True


@staff_required
def settings_index(request):
    return render(request, "staff/settings/index.html")


@staff_required
def org_list(request):
    return render(request, "staff/settings/org_list.html", {"orgs": Organization.objects.order_by("slug")})


def _org_editor(request, org):
    old_logo = org.logo.name if org and org.logo else ""  # before validation moves the new file onto org
    form = OrganizationForm(request.POST or None, request.FILES or None, instance=org)
    if request.method == "POST" and form.is_valid():
        sensitive = [name for name in STEP_UP_ORG_FIELDS if name in form.changed_data]
        if sensitive and (refused := stepup.refusal(request, request.path)):
            return refused
        with transaction.atomic():
            if org is not None:
                # Written by issuing invoices, never by this form: carry the live counter over, as a
                # stale read of it would otherwise be saved back and number a second invoice the same.
                form.instance.next_number = (Organization.objects.select_for_update(no_key=True)
                                             .values_list("next_number", flat=True).get(pk=org.pk))
            saved, changed = audited_save(request, form, "organization")
            if changed and sensitive:
                stepup.announce(request.user, "organization issuer or return hosts changed",
                                _org_change_text(saved, sensitive, form.changed_data, creating=org is None),
                                reverse("staff:org_edit", args=[saved.pk]))
            if old_logo and saved.logo.name != old_logo:
                storage = saved.logo.storage
                def drop_old():  # only once the new one is committed, and only if no row still names the file
                    if not Organization.objects.filter(logo=old_logo).exists():
                        storage.delete(old_logo)
                transaction.on_commit(drop_old)
        messages.success(request, f"Saved {saved.slug}." if changed else "No changes.")
        return redirect(reverse("staff:org_list"))
    status = 400 if request.method == "POST" and "logo" in form.errors else 200
    ctx = {"form": form, "org": org}
    if org:
        ctx["banks"] = org.bank_accounts.order_by("-active", "currency", "label", "pk")
        ctx["fiats"] = org.fiat_links.order_by("-active", "kind", "label", "pk")
    return render(request, "staff/settings/org_form.html", ctx, status=status)


def _org_change_text(org, sensitive, changed, *, creating) -> str:
    parts = []
    if "is_us_resident_issuer" in sensitive:
        parts.append(f"the US-resident issuer flag is now {'on' if org.is_us_resident_issuer else 'off'}")
    if "return_url_hosts" in sensitive:
        hosts = (org.settings or {}).get("return_url_hosts") or []
        parts.append(f"the return hosts are now {', '.join(hosts) if hosts else 'none'}")
    verb = "Created" if creating else "Changed"
    return (f"{verb} organization {org.slug} (#{org.pk}): {'; '.join(parts)}. "
            f"Fields saved: {', '.join(sorted(changed))}.")


@staff_required
def org_new(request):
    return _org_editor(request, None)


@staff_required
def org_edit(request, pk):
    return _org_editor(request, get_object_or_404(Organization, pk=pk))


def _account_noun(obj) -> str:
    return "bank account" if isinstance(obj, BankAccount) else "payment link"


def _account_text(obj) -> str:
    """A bank account or payment link by its label, id and organization: never an IBAN or a URL."""
    kind = obj.currency if isinstance(obj, BankAccount) else obj.kind
    return f"{_account_noun(obj)} “{obj.label or '(no label)'}” (#{obj.pk}, {kind}) of organization {obj.org.slug}"


def _account_editor(request, org, obj, form_class, prefix, template, noun):
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and (refused := stepup.refusal(request, request.path)):
        return refused
    if request.method == "POST" and form.is_valid():
        if obj is None:
            form.instance.org = org
        creating = obj is None
        with transaction.atomic():
            saved, changed = audited_save(request, form, prefix)
            if changed:
                fields = sorted(form.changed_data)
                stepup.announce(request.user, f"{noun} {'created' if creating else 'changed'}",
                                f"{'Created' if creating else 'Changed'} {_account_text(saved)}. "
                                f"Fields saved: {', '.join(fields) or 'none'}.",
                                reverse("staff:org_edit", args=[org.pk]))
        messages.success(request, f"Saved the {noun}." if changed else "No changes.")
        return redirect(reverse("staff:org_edit", args=[org.pk]))
    return render(request, template, {"form": form, "org": org, "obj": obj})


def _toggle(request, obj, prefix):
    if refused := stepup.refusal(request, reverse("staff:org_edit", args=[obj.org_id])):
        return refused
    with transaction.atomic():
        obj.active = not obj.active
        obj.save(update_fields=["active"])
        audit.record(request.user, f"{prefix}.update", obj, {"fields": ["active"], "via": "staff"})
        state = "Activated" if obj.active else "Deactivated"
        stepup.announce(request.user, f"{_account_noun(obj)} {state.lower()}", f"{state} {_account_text(obj)}.",
                        reverse("staff:org_edit", args=[obj.org_id]))
    messages.success(request, f"{'Activated' if obj.active else 'Deactivated'} {obj}.")
    return redirect(reverse("staff:org_edit", args=[obj.org_id]))


@staff_required
def bank_new(request, org_pk):
    org = get_object_or_404(Organization, pk=org_pk)
    return _account_editor(request, org, None, BankAccountForm, "bankaccount", "staff/settings/bank_form.html", "bank account")


@staff_required
def bank_edit(request, pk):
    obj = get_object_or_404(BankAccount.objects.select_related("org"), pk=pk)
    return _account_editor(request, obj.org, obj, BankAccountForm, "bankaccount", "staff/settings/bank_form.html", "bank account")


@staff_required
@require_POST
def bank_toggle(request, pk):
    return _toggle(request, get_object_or_404(BankAccount.objects.select_related("org"), pk=pk), "bankaccount")


@staff_required
def fiat_new(request, org_pk):
    org = get_object_or_404(Organization, pk=org_pk)
    return _account_editor(request, org, None, FiatLinkForm, "fiatlink", "staff/settings/fiat_form.html", "payment link")


@staff_required
def fiat_edit(request, pk):
    obj = get_object_or_404(FiatLink.objects.select_related("org"), pk=pk)
    return _account_editor(request, obj.org, obj, FiatLinkForm, "fiatlink", "staff/settings/fiat_form.html", "payment link")


@staff_required
@require_POST
def fiat_toggle(request, pk):
    return _toggle(request, get_object_or_404(FiatLink.objects.select_related("org"), pk=pk), "fiatlink")
