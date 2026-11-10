from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils import translation

from apps.core.client_pages import harden

from .vault import vault

EXACT = ("/healthz", "/api/v1/schema", "/api/v1/docs", "/llms.txt")
# /theme/ only sets the viewer's theme cookie: the unlock page and the sealed answer carry the switcher.
PREFIXES = ("/unlock/", "/static/", "/theme/")


class VaultGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if vault.is_unsealed():
            return self.get_response(request)
        path = request.path
        if path.startswith(PREFIXES) or path.rstrip("/") in EXACT:
            return self.get_response(request)
        if path.startswith(("/api/", "/hooks/")):
            return JsonResponse({"error": "vault_sealed"}, status=503)
        if path.startswith(("/i/", "/p/", "/k/", "/l/")):  # client pages: never the staff unlock form
            return self.unavailable(request)
        return redirect("/unlock/")

    @staticmethod
    def unavailable(request):
        """The client pages' answer while sealed: "temporarily unavailable", 503, uncacheable."""
        # The gate runs before LocaleMiddleware, so choose the language here.
        codes = {code for code, _ in settings.LANGUAGES}
        lang = request.GET.get("lang")
        if lang not in codes:
            lang = translation.get_language_from_request(request)
        with translation.override(lang):
            html = render_to_string("vault/unavailable.html", {"LANGUAGE_CODE": lang}, request=request)
        # The URL carries a capability token: no cache may keep this, no crawler index it.
        return harden(HttpResponse(html, status=503, content_type="text/html"))
