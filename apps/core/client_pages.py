"""Every answer on a page reached through a capability link (/i/, /p/, /k/, /l/), whoever produced it.

The views harden their own responses, but some answers never reach a view: the 503 the vault gate
serves while sealed, the 405 of a method decorator, a 404, a CSRF 403. Each of them carries the
token in its URL, so each tells caches, crawlers and cross-origin referrers to keep away from it.
(Referrer-Policy is same-origin, never no-referrer: with no-referrer browsers post forms with
`Origin: null`, which Django's CSRF check refuses.)
"""

# The one list of them: the log filter (`apps.core.logfilters`) and the theme switch (`apps.core.theme`)
# are built from it, and a test holds the Apache configuration's two patterns to it.
CAPABILITY_PREFIXES = ("/i/", "/p/", "/k/", "/l/")
CAPABILITY_LETTERS = "".join(prefix.strip("/") for prefix in CAPABILITY_PREFIXES)


def harden(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex"
    return response


class CapabilityPageHeadersMiddleware:
    """Must sit outside VaultGateMiddleware, so that the sealed-vault answer passes through it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(CAPABILITY_PREFIXES):
            harden(response)
        return response
