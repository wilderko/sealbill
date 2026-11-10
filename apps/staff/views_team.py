"""The Team page (spec 2026-11-05 §4, §9 item 5): every staff account, its last sign-in and its
confirmed second factors by name, and removal of another person's factor (a lost phone).

Removing a factor needs a second factor given within the last ten minutes, as on the Security page.
Removing another person's factor is a superuser's step-up power (audit SEC-3): it needs a superuser,
and every superuser is told by e-mail. Another person's last factor may go: that is how a locked-out colleague is recovered, and the page
then says to enrol a new one on the server. One's own last factor may not. Nothing secret is read
here: no TOTP key, no config URL, no credential id or public key. The models' managers defer the
TOTP key, and the lists below are `only()` the display columns.
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.audit import services as audit

from . import passkeys, stepup
from .models import StaffPasskey, StaffTOTPDevice
from .views import _harden, staff_required
from .views_security import LAST_FACTOR, _reverify_url

KINDS = {"totp": (StaffTOTPDevice, "totp_device.remove", "Authenticator"),
         "passkey": (StaffPasskey, "passkey.remove", "Passkey")}


def _members():
    User = get_user_model()
    users = list(User.objects.filter(is_staff=True).order_by("username"))
    factors = {u.pk: [] for u in users}
    for kind, model in (("totp", StaffTOTPDevice), ("passkey", StaffPasskey)):
        for d in model.objects.filter(user__in=users, confirmed=True).only(
                "id", "user_id", "name", "created_at", "last_used_at").order_by("created_at", "pk"):
            factors[d.user_id].append({"kind": kind, "pk": d.pk, "name": d.name,
                                       "created_at": d.created_at, "last_used_at": d.last_used_at})
    return [{"user": u, "factors": factors[u.pk], "is_self": False} for u in users]


def _page(request, *, error=None, status=200):
    members = _members()
    for m in members:
        m["is_self"] = m["user"].pk == request.user.pk
    return render(request, "staff/settings/team.html", {
        "members": members, "fresh": passkeys.recently_verified(request),
        "reverify_url": _reverify_url(reverse("staff:team")), "error": error}, status=status)


@staff_required
@require_GET
def team(request):
    return _page(request)


@staff_required
@require_POST
def team_factor_remove(request):
    if not passkeys.recently_verified(request):
        return redirect(_reverify_url(reverse("staff:team")))
    kind = request.POST.get("kind", "")
    raw_pk = request.POST.get("pk", "")
    if kind not in KINDS or not raw_pk.isdecimal():
        raise Http404("No such device.")
    pk = int(raw_pk)
    model, action, label = KINDS[kind]
    User = get_user_model()
    refused = False
    with transaction.atomic():
        device = model.objects.filter(pk=pk, confirmed=True, user__is_staff=True).select_related("user").first()
        if device is None:
            raise Http404("No such device.")
        owner = device.user
        # Another person's factor is a superuser's to remove (audit SEC-3): it locks that person out. The
        # recent second factor was checked above. One's own factor stays every staff member's to manage.
        if owner.pk != request.user.pk and not request.user.is_superuser:
            return _harden(HttpResponseForbidden(stepup.SUPERUSER_ONLY))
        # One change to an account's second factors at a time: lock the account and its factors
        # before counting, so two removals at once cannot both see the other one still there.
        User._default_manager.select_for_update(no_key=True).filter(pk=owner.pk).first()
        list(StaffTOTPDevice.objects.select_for_update(no_key=True).filter(user=owner).values_list("pk", flat=True))
        list(StaffPasskey.objects.select_for_update(no_key=True).filter(user=owner).values_list("pk", flat=True))
        device = model.objects.filter(pk=pk, confirmed=True, user=owner).first()
        if device is None:
            raise Http404("No such device.")
        own = owner.pk == request.user.pk
        if own and passkeys.second_factor_count(owner) <= 1:
            refused = True
        else:
            name = device.name
            audit.record(request.user, action, device, {"name": name, "user": owner.get_username(),
                                                        "user_id": owner.pk, "via": "staff"})
            device.delete()
            left = passkeys.second_factor_count(owner)
            if not own:
                stepup.announce(request.user, "staff second factor removed",
                                f"Removed {label.lower()} “{name}” from staff account {owner.get_username()} "
                                f"(#{owner.pk}); {left} second factor(s) left.", reverse("staff:team"))
    if refused:
        return _page(request, error=LAST_FACTOR, status=400)
    if left == 0 and not own:
        messages.warning(request, f"{label} “{name}” removed. {owner.get_username()} now has no second factor "
                                  f"and cannot sign in: run payinv-manage create_staff_totp "
                                  f"{owner.get_username()} --replace on the server.")
    else:
        messages.success(request, f"{label} “{name}” removed from {owner.get_username()}.")
    return redirect("staff:team")
