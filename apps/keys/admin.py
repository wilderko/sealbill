from django.contrib import admin

from apps.keys.models import EncryptionKey
from apps.vault.fields import UndecryptableValue
from apps.vault.registry import plaintext_columns


@admin.register(EncryptionKey)
class EncryptionKeyAdmin(admin.ModelAdmin):
    """Read-only. A key reaches this table only through validation (the staff contact page,
    the API or the client's upload), never by typing material into a form. The list loads
    plaintext columns only, so one undecryptable row cannot break it."""

    list_display = ("id", "contact_ref", "kind", "state", "source", "expires_at", "created_at")
    list_filter = ("kind", "state", "source")  # plaintext columns only
    ordering = ("-created_at", "-pk")
    fields = ("contact_ref", "kind", "state", "source", "fingerprint_display", "expires_at", "last_error_kind",
              "failed_while", "confirm_sent_at", "confirmed_at", "replaces_ref", "cancelled_at", "created_at",
              "created_by")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).only(*plaintext_columns(EncryptionKey))

    @admin.display(description="contact")
    def contact_ref(self, obj):
        return f"Contact #{obj.contact_id}"  # the id only: the contact's own columns are encrypted

    @admin.display(description="replaces")
    def replaces_ref(self, obj):
        return f"Key #{obj.replaces_id}" if obj.replaces_id else "-"  # the id only: no material is loaded

    @admin.display(description="fingerprint")
    def fingerprint_display(self, obj):
        try:
            return obj.fingerprint
        except UndecryptableValue:
            return "(cannot be decrypted)"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
