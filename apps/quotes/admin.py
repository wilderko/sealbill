from django.contrib import admin

from apps.quotes.models import Quote


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    """Read-only: a quote's amount is what a client was told to send, so nobody edits one."""

    list_display = ("pk", "invoice", "rail_account", "network", "asset", "state", "issued_at", "expires_at")
    list_filter = ("state", "network", "asset")
    ordering = ("-issued_at", "-pk")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
