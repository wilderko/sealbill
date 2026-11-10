from django.contrib import admin

from apps.invoices.models import Invoice, LineItem


class LineItemInline(admin.TabularInline):
    model = LineItem
    extra = 0

    def _draft(self, obj):
        return obj is None or obj.status == "draft"

    def has_add_permission(self, request, obj=None):
        return self._draft(obj)

    def has_change_permission(self, request, obj=None):
        return self._draft(obj)

    def has_delete_permission(self, request, obj=None):
        return self._draft(obj)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "org", "kind", "currency", "status", "issue_date", "due_date")
    list_filter = ("org", "kind", "status")
    search_fields = ("number", "external_reference")
    inlines = [LineItemInline]
    lifecycle_readonly = (
        "number", "status", "view_token_hash", "pay_token_hash", "sent_at", "voided_at",
        "paid_at", "voided_by", "void_reason", "issued_by_rule",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return obj is not None and obj.status == "draft"

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.status != "draft":
            return [f.name for f in self.model._meta.concrete_fields]
        return list(self.lifecycle_readonly) + ["org", "contact", "currency", "requested_by_org"]
