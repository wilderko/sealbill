"""The viewer's theme: a `theme` cookie, rendered by the server as `data-theme` on <html>.

Three themes, `black`, `white` and `dim`. Without the cookie (or with any other value) the page has
no `data-theme` and the stylesheet follows the device's light or dark setting. The server renders
the attribute, so a chosen theme never flashes the other one first.

The switcher is a plain form that POSTs to /theme/ (CSRF, a same-origin path to return to), so it
works without JavaScript; `static/ui/ui.js` sets the same cookie in place when it can.
"""
import re

from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.client_pages import CAPABILITY_LETTERS

COOKIE = "theme"
THEMES = ("black", "white", "dim")
FOLLOW_DEVICE = "auto"  # the switcher's fourth button: forget the choice
MAX_AGE = 365 * 24 * 3600
_CAPABILITY_PAGE = re.compile(rf"^/[{CAPABILITY_LETTERS}]/[A-Za-z0-9_-]+/")
FALLBACK = "/"


def current(request) -> str:
    value = request.COOKIES.get(COOKIE, "")
    return value if value in THEMES else ""


def _same_origin_path(request, candidate: str) -> str:
    """`candidate` when it is a path on this site, else ""."""
    if not candidate.startswith("/") or candidate.startswith(("//", "/\\")):
        return ""
    ok = url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()},
                                         require_https=request.is_secure())
    return candidate if ok else ""


def _back(request) -> str:
    """Where the switcher returns to: this page, when a GET can show it again. A page rendered by a
    POST (a refused form, a fresh quote) cannot be asked for by GET, so the page the form was on
    (the same-origin Referer) is used, else the capability link's own page."""
    if request.method in ("GET", "HEAD"):
        return request.get_full_path()
    referer = request.META.get("HTTP_REFERER", "")
    host = request.get_host()
    for scheme in ("https://", "http://"):
        prefix = f"{scheme}{host}"
        if referer.startswith(prefix + "/"):
            path = _same_origin_path(request, referer[len(prefix):])
            if path:
                return path
    match = _CAPABILITY_PAGE.match(request.path)
    return match.group(0) if match else FALLBACK


def context(request) -> dict:
    """Template context: `theme` for <html data-theme>, `theme_next` for the switcher's form."""
    return {"theme": current(request), "theme_next": _back(request)}


@require_POST
def set_theme(request):
    choice = request.POST.get("theme", "")
    if choice not in THEMES and choice != FOLLOW_DEVICE:
        return HttpResponseBadRequest("Unknown theme.", content_type="text/plain")
    response = HttpResponseRedirect(_same_origin_path(request, request.POST.get("next", "")) or FALLBACK)
    if choice == FOLLOW_DEVICE:
        response.delete_cookie(COOKIE, path="/", samesite="Lax")
    else:
        # Not HttpOnly: ui.js writes the same cookie when the switch happens in place.
        response.set_cookie(COOKIE, choice, max_age=MAX_AGE, path="/", samesite="Lax",
                            secure=request.is_secure(), httponly=False)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response
