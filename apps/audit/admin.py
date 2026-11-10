from django.contrib import admin

from apps.audit.models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    # actor_username, not actor: it names who acted without an account too ("system", "client").
    list_display = ("created_at", "actor_username", "action", "model_label", "object_pk")
    list_filter = ("action", "model_label")  # plaintext columns only

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
