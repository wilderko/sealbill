import hashlib
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.orgs.models import Organization

SCOPES = ("read", "invoices:write", "payments:confirm", "admin")


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


class ApiToken(models.Model):
    """A bearer credential for one organization. Only the SHA-256 of the token is stored.

    `service_user` is an inactive account named `api:<name>` that the services record as the
    actor, so the audit trail and `voided_by` / `confirmed_by` name the token that acted.
    """

    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    service_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.org.slug}:{self.name}"

    def has_scope(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    @classmethod
    def issue(cls, org, name: str, scopes, *, actor=None, channel: str = "api") -> tuple["ApiToken", str]:
        if isinstance(scopes, (str, bytes)):
            raise ValueError("scopes must be a list")
        scopes = list(scopes)
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
        plain = secrets.token_urlsafe(32)
        User = get_user_model()
        with transaction.atomic():
            base = f"api:{name}"[:140]
            username, n = base, 1
            while User.objects.filter(**{User.USERNAME_FIELD: username}).exists():
                n += 1
                username = f"{base}#{n}"
            user = User(**{User.USERNAME_FIELD: username}, is_active=False)
            user.set_unusable_password()
            user.save()
            token = cls.objects.create(
                org=org, name=name, token_hash=hash_token(plain), scopes=scopes, service_user=user
            )
            record_change(actor, "api_token.create", token, {"scopes": list(scopes), "channel": channel})
        return token, plain

    def revoke(self, *, actor=None, channel: str = "api") -> None:
        with transaction.atomic():
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])
            record_change(actor, "api_token.revoke", self, {"scopes": list(self.scopes), "channel": channel})


def record_change(actor, action: str, token: "ApiToken", detail: dict) -> None:
    """Audit a token's creation, scope change or revocation: its scopes and the channel it went
    through, never the token or its hash. A person is recorded as the actor; a command has none
    and is recorded as the system, which also works while the vault is sealed."""
    if getattr(actor, "pk", None):
        audit.record(actor, action, token, detail)
    else:
        audit.record_system(action, token, detail)
