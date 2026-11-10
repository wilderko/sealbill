from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from . import unlock_attempts
from .ratelimit import address_bucket, client_ip, rate_limited
from .vault import vault


@require_http_methods(["GET", "POST"])
def unlock(request):
    error = False
    if request.method == "POST":
        if vault.is_unsealed():  # nothing to unlock: no PBKDF2, no password check
            return redirect("staff:needs_person")
        # Per address, an IPv6 one per /64: a host is handed a whole /64 to rotate through.
        if rate_limited("unlock", address_bucket(client_ip(request)), 5, 300):
            return HttpResponse(_("Too many attempts. Try again later."), status=429)
        if vault.unseal(request.POST.get("password", "")):
            unlock_attempts.unsealed()
            return redirect("staff:needs_person")
        unlock_attempts.wrong_password()
        error = True
    return render(request, "vault/unlock.html", {"error": error, "unsealed": vault.is_unsealed()},
                  status=401 if error else 200)
