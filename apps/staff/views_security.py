"""The Security page and the passkey ceremonies (spec 2026-11-04 §3).

The ceremonies need the browser's WebAuthn API, so they are the one place staff pages run a script of
their own (`static/ui/passkeys.js`, a static file under the CSP nonce). Each is two requests:
- the script POSTs (CSRF token in `X-CSRFToken`) for the options, which carry a fresh challenge held
  in the session, and gets JSON back;
- the browser asks the authenticator, the script puts its answer in a hidden field and submits an
  ordinary form, which this module verifies and answers with a redirect or the page and its error.

Adding or removing a second factor needs a second factor given within the last ten minutes
(`passkeys.recently_verified`); an older session is sent to verify again. The last second factor of
an account cannot be removed. Every addition and removal is audited.
"""
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_POST

from . import passkeys
from .forms import PasskeyNameForm
from .models import StaffPasskey, StaffTOTPDevice
from .views import _harden, complete_second_factor, second_factor_limited, staff_required, verify_page

REGISTER_REFUSALS = {
    "no_challenge": "The passkey was not added: start again with “Add a passkey”.",
    "expired": "The passkey was not added: the request expired after five minutes. Start again.",
    "user_verification": "The passkey was not added: the authenticator did not check that it is you "
                         "(a PIN, a fingerprint or a face). Use one that does.",
    "duplicate": "That passkey is already registered.",
}
REGISTER_REFUSED = "The passkey was not added: the browser's answer did not verify. Start again."
LOGIN_REFUSALS = {
    "expired": "The passkey request expired after five minutes. Try again.",
}
LOGIN_REFUSED = "That passkey was not accepted."
LAST_FACTOR = ("That is the last second factor of this account, so it cannot be removed: add a passkey "
               "or an authenticator first.")


def _reverify_url(next_url: str | None = None) -> str:
    """The verify page, returning to `next_url` (the Security page by default)."""
    return reverse("staff:verify") + "?" + urlencode({"next": next_url or reverse("staff:security")})


def _security_page(request, *, form=None, error=None, status=200):
    user = request.user
    context = {
        "passkeys": StaffPasskey.objects.filter(user=user).order_by("created_at", "pk"),
        "authenticators": StaffTOTPDevice.objects.filter(user=user).order_by("created_at", "pk"),
        "fresh": passkeys.recently_verified(request),
        "form": form or PasskeyNameForm(),
        "error": error,
        "reverify_url": _reverify_url(),
    }
    return render(request, "staff/security.html", context, status=status)


@require_GET
@staff_required
def security(request):
    return _security_page(request)


@require_POST
@staff_required
def passkey_register_options(request):
    if not passkeys.recently_verified(request):
        return JsonResponse({"error": "Verify again first: adding a passkey needs a second factor from the "
                                      "last ten minutes.", "verify": _reverify_url()}, status=403)
    return HttpResponse(passkeys.registration_options(request), content_type="application/json")


@require_POST
@staff_required
def passkey_register(request):
    if not passkeys.recently_verified(request):
        passkeys.discard_challenge(request)
        return redirect(_reverify_url())
    form = PasskeyNameForm(request.POST)
    if not form.is_valid():
        passkeys.discard_challenge(request)  # the challenge was spent on this answer either way
        return _security_page(request, form=form, error="The passkey was not added: give it a name.", status=400)
    name = form.cleaned_data["name"]
    try:
        passkeys.register(request, name, request.POST.get("credential", ""))
    except passkeys.PasskeyRefused as exc:
        return _security_page(request, form=form, error=REGISTER_REFUSALS.get(exc.reason, REGISTER_REFUSED),
                              status=400)
    messages.success(request, f"Passkey “{name}” added. It can be used at the next sign-in.")
    return redirect("staff:security")


def _remove(request, model, pk, label):
    if not passkeys.recently_verified(request):
        return redirect(_reverify_url())
    try:
        removed = passkeys.remove(request.user, model, pk)
    except passkeys.LastSecondFactor:
        return _security_page(request, error=LAST_FACTOR, status=400)
    if not removed:
        raise Http404("No such device on this account.")
    messages.success(request, f"{label} removed.")
    return redirect("staff:security")


@require_POST
@staff_required
def passkey_remove(request, pk):
    return _remove(request, StaffPasskey, pk, "Passkey")


@require_POST
@staff_required
def totp_remove(request, pk):
    return _remove(request, StaffTOTPDevice, pk, "Authenticator")


def _signed_in_staff(request):
    """The second step's own gate: a password given (an authenticated session) by an active staff
    account. Verified or not: verifying again is how the Security page's window is reopened."""
    user = request.user
    return user.is_authenticated and user.is_active and user.is_staff


@require_POST
def passkey_login_options(request):
    if not _signed_in_staff(request):
        return _harden(JsonResponse({"error": "Sign in with your password first.", "login": reverse("staff:login")},
                                    status=403))
    options = passkeys.authentication_options(request)
    if options is None:
        return _harden(JsonResponse({"error": "No passkey is registered for this account."}, status=400))
    return _harden(HttpResponse(options, content_type="application/json"))


@require_POST
def passkey_login(request):
    if not request.user.is_authenticated:
        return redirect("staff:login")
    if not _signed_in_staff(request):
        return _harden(HttpResponseForbidden("Staff only."))
    if second_factor_limited(request):
        return verify_page(request, limited=True, status=429)
    try:
        passkey = passkeys.authenticate(request, request.POST.get("credential", ""))
    except passkeys.PasskeyRefused as exc:
        return verify_page(request, passkey_error=LOGIN_REFUSALS.get(exc.reason, LOGIN_REFUSED), status=400)
    return complete_second_factor(request, passkey)
