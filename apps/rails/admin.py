from django.contrib import admin

from apps.rails.models import FxLegRate, RailAccount, WatcherHealth
from apps.staff.admin_stepup import StepUpAdminMixin


@admin.register(RailAccount)
class RailAccountAdmin(StepUpAdminMixin, admin.ModelAdmin):
    """Every change is a step-up change (audit SEC-3): told to every superuser, by field name only."""

    step_up_noun = "rail account"

    def step_up_label(self, obj):
        return f"rail account “{obj.label or '(no label)'}” (#{obj.pk}, {obj.kind}) of organization {obj.org.slug}"

    list_display = ("kind", "label", "org", "address", "active")
    list_filter = ("kind", "active")
    exclude = ("credentials",)  # encrypted; set with `manage.py set_rail_credentials <id>`, never shown here


@admin.register(WatcherHealth)
class WatcherHealthAdmin(admin.ModelAdmin):
    list_display = ("name", "last_ok_at", "failing_since", "consecutive_failures", "last_error_kind")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FxLegRate)
class FxLegRateAdmin(admin.ModelAdmin):
    """The last accepted Frankfurter rate per pair, and the refused one waiting for a person. Read
    only: a real move is accepted on Needs a person (TOTP, a note, audited), never here."""

    list_display = ("base", "quote", "rate", "source_date", "pending_rate", "pending_source_date", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
