from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from . import unlock_attempts
from .ratelimit import address_bucket, client_ip, rate_limited
from .vault import vault

# Every address together (audit 2026-11-11): the per-address limit alone multiplies with the number of
# addresses someone can send from. Whoever fills it can keep the page from unlocking, never the server:
# `manage.py unseal_vault` has no limit, and the refusal says so. An unseal starts the count over.
UNLOCK_LIMIT_ALL, UNLOCK_WINDOW_ALL_S = 20, 900


@require_http_methods(["GET", "POST"])
def unlock(request):
    error = False
    if request.method == "POST":
        if vault.is_unsealed():  # nothing to unlock: no PBKDF2, no password check
            return redirect("staff:needs_person")
        # Per address, an IPv6 one per /64: a host is handed a whole /64 to rotate through.
        if rate_limited("unlock", address_bucket(client_ip(request)), 5, 300):
            return HttpResponse(_("Too many attempts. Try again later."), status=429)
        if rate_limited(unlock_attempts.ALL_BUCKET, unlock_attempts.ALL_IDENT, UNLOCK_LIMIT_ALL, UNLOCK_WINDOW_ALL_S):
            return HttpResponse(
                _("Too many unlock attempts right now. Unlock from the server with manage.py unseal_vault, or try again later."),  # noqa: E501
                status=429)
        if vault.unseal(request.POST.get("password", "")):
            unlock_attempts.unsealed()
            return redirect("staff:needs_person")
        unlock_attempts.wrong_password()
        error = True
    return render(request, "vault/unlock.html", {"error": error, "unsealed": vault.is_unsealed()},
                  status=401 if error else 200)
