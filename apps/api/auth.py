import hmac
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from apps.api.models import ApiToken, hash_token

TOUCH_EVERY = timedelta(minutes=1)


class BearerTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate_header(self, request):
        return self.keyword

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        parts = header.split()
        if not parts or parts[0].lower() != self.keyword.lower():
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")
        digest = hash_token(parts[1])
        token = ApiToken.objects.select_related("org", "service_user").filter(token_hash=digest).first()
        # The lookup is by hash; the comparison is repeated in constant time as a belt.
        if token is None or not hmac.compare_digest(token.token_hash, digest) or token.revoked_at is not None or token.service_user_id is None:
            raise exceptions.AuthenticationFailed("Invalid or revoked token.")
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at >= TOUCH_EVERY:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
            token.last_used_at = now
        return token.service_user, token


class BearerScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.auth.BearerTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "description": "API token created with `manage.py create_api_token`."}
