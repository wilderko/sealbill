"""The old /staff/ prefix: the staff pages moved to the site root, and bookmarks, e-mails already
sent and open sessions still carry the old paths."""
import re

from django.http import Http404, HttpResponsePermanentRedirect


def staff_prefix_redirect(request, rest: str | None = None):
    """GET/HEAD /staff/<rest> -> 301 /<rest>, query kept. Other methods get 404: a POST cannot be
    carried across a redirect safely, and a form on a stale page should fail loudly.

    The target is always a single-slash path on this host: leading slashes and backslashes
    (which browsers read as slashes) are collapsed, so //evil.example can never be produced."""
    rest = rest or ""
    if request.method not in ("GET", "HEAD"):
        raise Http404
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in rest):
        raise Http404
    target = "/" + re.sub(r"^[/\\]+", "", rest)
    query = request.META.get("QUERY_STRING", "")
    if query:
        target += "?" + query
    return HttpResponsePermanentRedirect(target)
