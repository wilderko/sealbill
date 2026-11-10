"""Content-Security-Policy with a per-response nonce.

Every response gets a fresh nonce (`request.csp_nonce`, `{{ csp_nonce }}` in templates).
Scripts and styles come from this origin only; inline `on*=` attributes and `style=`
attributes are not allowed. The admin gets the same script policy; only its styles may be inline.
"""
import secrets

from django.conf import settings

POLICY = (
    "default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def _may_use_admin(request):
    """Only a signed-in, verified superuser gets the relaxed policy: the admin path answers 404 to
    everyone else, and a distinct header would tell a guesser the path is real."""
    user = getattr(request, "user", None)
    try:
        return bool(user is not None and user.is_authenticated and user.is_active and user.is_superuser
                    and user.is_verified())
    except AttributeError:
        return False


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            policy = POLICY.format(nonce=request.csp_nonce)
            if request.path.startswith("/" + settings.DJANGO_ADMIN_PATH) and _may_use_admin(request):
                policy = policy.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")
            response["Content-Security-Policy"] = policy
        return response


def csp_nonce(request):
    return {"csp_nonce": getattr(request, "csp_nonce", "")}
