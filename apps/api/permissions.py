from django.core.cache import caches
from rest_framework import permissions
from rest_framework.throttling import SimpleRateThrottle

from apps.api.models import ApiToken

READ = "read"
INVOICES_WRITE = "invoices:write"
PAYMENTS_CONFIRM = "payments:confirm"


class HasScope(permissions.BasePermission):
    """The view names the scope each HTTP method needs in `scopes`; GET defaults to `read`."""

    def has_permission(self, request, view):
        token = request.auth
        if not isinstance(token, ApiToken):
            return False
        scopes = getattr(view, "scopes", {})
        # HEAD and OPTIONS run the view's GET handler, so they need what GET needs.
        method = "GET" if request.method in ("HEAD", "OPTIONS") else request.method
        needed = scopes.get(method) or (READ if method == "GET" else None)
        return bool(needed) and token.has_scope(needed)


class TokenThrottle(SimpleRateThrottle):
    """120 requests a minute per token, counted in the shared `ratelimit` cache."""

    scope = "token"
    THROTTLE_RATES = {"token": "120/min"}

    def __init__(self):
        self.cache = caches["ratelimit"]
        super().__init__()

    def get_rate(self):
        return self.THROTTLE_RATES["token"]

    def get_cache_key(self, request, view):
        if not isinstance(request.auth, ApiToken):
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.auth.pk}
