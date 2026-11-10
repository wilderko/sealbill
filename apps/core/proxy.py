"""Only the reverse proxy reaches the application, when PROXY_SECRET is set.

The socket Apache connects to is open to Apache's group, and on a shared host other processes run
as that user too (php-fpm as www-data). They could send their own X-Forwarded-For, the address the
rate limits are keyed on (vault unlock, staff login, the pay page). Apache sets X-Pay-Proxy-Secret on
every request it forwards (`RequestHeader set` replaces whatever the client sent); a request without
the right value is refused before anything else runs. PROXY_SECRET empty (development, tests, Docker):
nothing is checked.
"""
import hmac

from django.conf import settings
from django.http import HttpResponseForbidden

HEADER = "HTTP_X_PAY_PROXY_SECRET"


class ProxySecretMiddleware:
    """First in MIDDLEWARE: a refused request reaches no other middleware, view or rate limit."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        secret = settings.PROXY_SECRET
        if not secret:
            return self.get_response(request)
        # Taken out of the request: no view, log line or error report can show it.
        given = request.META.pop(HEADER, "")
        if not hmac.compare_digest(given.encode("utf-8", "replace"), secret.encode("utf-8", "replace")):
            return HttpResponseForbidden("Forbidden\n", content_type="text/plain")
        return self.get_response(request)
