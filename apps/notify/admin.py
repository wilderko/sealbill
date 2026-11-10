from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import reverse

from apps.notify import services
from apps.notify.models import OutboxMessage


@admin.register(OutboxMessage)
class OutboxMessageAdmin(admin.ModelAdmin):
    """Delivery state only. Recipients and contents are encrypted and never loaded here, so
    one undecryptable row cannot break the page and no address is shown."""

    list_display = ("id", "kind", "state", "attempts", "next_attempt_at", "created_at", "invoice_link")
    list_filter = ("state", "kind")  # plaintext columns only
    ordering = ("-created_at", "-pk")
    fields = ("kind", "state", "attempts", "created_at", "next_attempt_at", "sent_at", "staff_alerted_at",
              "webhook_alerted_at", "invoice_link", "language")
    readonly_fields = fields
    actions = ["retry_now"]

    def get_queryset(self, request):
        return super().get_queryset(request).only(*services.PLAINTEXT_FIELDS)

    @admin.display(description="invoice")
    def invoice_link(self, obj):
        if not obj.invoice_id:
            return "-"
        return format_html('<a href="{}">#{}</a>', reverse("staff:invoice", args=[obj.invoice_id]), obj.invoice_id)

    @admin.action(description="Retry now (fresh attempts)")
    def retry_now(self, request, queryset):
        n = services.retry_now(queryset, actor=request.user)
        self.message_user(request, f"{n} message(s) queued again.", messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None  # the list and its action; a message itself is never edited

    def has_delete_permission(self, request, obj=None):
        return False
