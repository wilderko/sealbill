from django.contrib import admin, messages

from apps.vault.registry import plaintext_columns
from apps.webhooks import delivery
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

_ENDPOINT_FIELDS = plaintext_columns(WebhookEndpoint)


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    """Read-only, plaintext columns only: the URL and the secret are encrypted and never loaded or
    shown here. Endpoints are created and deleted through the audited API (`/api/v1/webhooks`),
    which checks the destination and hands out the secret once."""

    list_display = ("id", "org", "description", "events", "active", "created_at")
    list_filter = ("active",)
    fields = ("org", "description", "events", "active", "created_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("org").only(*_ENDPOINT_FIELDS, "org__slug")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "org", "invoice_id", "payment_id", "created_at")
    list_filter = ("type",)
    fields = ("id", "type", "org", "invoice_id", "payment_id", "data", "created_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("org").only(
            *plaintext_columns(WebhookEvent), "org__slug")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    """The delivery log. Never the URL: the endpoint is named by its id and description."""

    list_display = ("id", "event_type", "endpoint_id", "state", "attempts", "last_status_code", "last_error_kind",
                    "next_attempt_at", "sent_at", "created_at")
    list_filter = ("state",)
    ordering = ("-created_at", "-pk")
    fields = ("event_id", "endpoint_id", "state", "attempts", "last_status_code", "last_error_kind",
              "next_attempt_at", "sent_at", "staff_alerted_at", "created_at")
    readonly_fields = fields
    actions = ["retry_now"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("event").only(
            *plaintext_columns(WebhookDelivery), "event__type")

    @admin.display(description="event")
    def event_type(self, obj):
        return obj.event.type

    @admin.action(description="Retry now (fresh attempts)")
    def retry_now(self, request, queryset):
        n = delivery.retry_now(queryset, actor=request.user)
        self.message_user(request, f"{n} delivery(ies) queued again.", messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None

    def has_delete_permission(self, request, obj=None):
        return False
