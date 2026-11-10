from django import forms
from django.contrib import admin

from apps.api.models import SCOPES, ApiToken, record_change
from apps.staff.admin_stepup import StepUpAdminMixin


class ApiTokenForm(forms.ModelForm):
    class Meta:
        model = ApiToken
        fields = "__all__"

    def clean_scopes(self):
        scopes = self.cleaned_data.get("scopes")
        # A string would pass `in` checks by substring ("admin" in "xadminx"), so only a list of known names.
        if not isinstance(scopes, list) or not all(isinstance(s, str) and s in SCOPES for s in scopes):
            raise forms.ValidationError("Scopes must be a JSON list of: " + ", ".join(SCOPES))
        return scopes


@admin.register(ApiToken)
class ApiTokenAdmin(StepUpAdminMixin, admin.ModelAdmin):
    """Tokens are created with `manage.py create_api_token` (the plain token is shown once). A change to the
    scopes or the revocation is a step-up change (audit SEC-3): a fresh second factor, and every superuser told."""

    step_up_noun = "API token"
    step_up_fields = ("scopes", "revoked_at")

    def step_up_label(self, obj):
        return f"API token “{obj.name}” (#{obj.pk}, organization {obj.org.slug})"

    def step_up_extra(self, obj, fields):
        revoked = " It is revoked." if obj.revoked_at else ""
        return f" Its scopes are now {', '.join(obj.scopes or []) or 'none'}.{revoked}"

    form = ApiTokenForm
    list_display = ("name", "org", "scopes", "last_used_at", "revoked_at")
    list_filter = ("org",)
    readonly_fields = ("org", "name", "token_hash", "service_user", "created_at", "last_used_at")
    fields = ("org", "name", "scopes", "token_hash", "service_user", "created_at", "last_used_at", "revoked_at")

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        detail = {"scopes": list(obj.scopes), "channel": "admin"}
        if "scopes" in form.changed_data:
            record_change(request.user, "api_token.scopes", obj, detail)
        if "revoked_at" in form.changed_data and obj.revoked_at is not None:
            record_change(request.user, "api_token.revoke", obj, detail)
