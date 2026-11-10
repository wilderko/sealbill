from django.contrib import admin
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit import services as audit

from .admin_stepup import StepUpAdminMixin
from .models import StaffPasskey, StaffTOTPDevice


class _OthersFactorMixin(StepUpAdminMixin):
    """Removing or unconfirming another staff member's second factor is a step-up change (audit SEC-3):
    told to every superuser. One's own is not, as on the Team page."""

    step_up_fields = ("confirmed",)

    def step_up_applies(self, request, obj):
        return obj.user_id != request.user.pk

    def step_up_label(self, obj):
        return f"{self.step_up_noun} “{obj.name}” of staff account {obj.user.get_username()} (#{obj.user_id})"

# django-otp's own admin shows the plaintext key (and a QR code of it). Staff devices live in
# StaffTOTPDevice; the old table is emptied by migration staff.0002 and nothing reads it.
if admin.site.is_registered(TOTPDevice):
    admin.site.unregister(TOTPDevice)


@admin.register(StaffTOTPDevice)
class StaffTOTPDeviceAdmin(_OthersFactorMixin, admin.ModelAdmin):
    """Never the secret: `create_staff_totp` shows it once, then only the vault can read it. Devices
    are made by that command, so the admin cannot add one; it can rename, unconfirm, reset the
    throttle or delete."""

    step_up_noun = "authenticator"

    list_display = ["user", "name", "confirmed", "created_at", "last_used_at"]
    list_filter = ["confirmed"]
    fields = ["user", "name", "confirmed", "created_at", "last_used_at", "throttling_failure_count",
              "throttling_failure_timestamp"]
    readonly_fields = ["user", "created_at", "last_used_at", "throttling_failure_timestamp"]

    def has_add_permission(self, request):
        return False


@admin.register(StaffPasskey)
class StaffPasskeyAdmin(_OthersFactorMixin, admin.ModelAdmin):
    """Passkeys are added by their owner on the Security page (a WebAuthn ceremony), so the admin
    cannot add one; it can rename one or delete a lost one (the recovery path beside
    `create_staff_totp --replace`). The public key and the counter are shown, never edited."""

    step_up_noun = "passkey"

    list_display = ["user", "name", "created_at", "last_used_at"]
    fields = ["user", "name", "credential_id", "sign_count", "transports", "created_at", "last_used_at"]
    readonly_fields = ["user", "credential_id", "sign_count", "transports", "created_at", "last_used_at"]

    def has_add_permission(self, request):
        return False

    # Audited like the Security page, by ids and names only, and marked as done in the admin (final
    # review F6). No last-second-factor rule here: deleting a lost passkey is the recovery path.
    def save_model(self, request, obj, form, change):
        old_name = StaffPasskey.objects.filter(pk=obj.pk).values_list("name", flat=True).first() if change else None
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if change and old_name != obj.name:
                audit.record(request.user, "passkey.rename", obj,
                             {"old_name": old_name, "name": obj.name, "user": obj.user_id, "via": "admin"})

    def delete_model(self, request, obj):
        with transaction.atomic():
            audit.record(request.user, "passkey.remove", obj, {"name": obj.name, "user": obj.user_id, "via": "admin"})
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            for passkey in queryset.only("pk", "name", "user_id"):
                audit.record(request.user, "passkey.remove", passkey,
                             {"name": passkey.name, "user": passkey.user_id, "via": "admin"})
            super().delete_queryset(request, queryset)
