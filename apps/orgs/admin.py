from django.contrib import admin

from apps.audit import services as audit
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.staff.admin_stepup import StepUpAdminMixin


class AuditedAdmin(admin.ModelAdmin):
    """Every save and delete is an audit entry naming the fields that changed. Never their values:
    an IBAN, an address or a payment link must not be copied into the log."""

    audit_prefix = ""

    def _audit(self, request, obj, action, fields=()):
        audit.record(request.user, f"{self.audit_prefix}.{action}", obj, {"fields": sorted(fields), "via": "admin"})

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._audit(request, obj, "update" if change else "create", form.changed_data)

    def delete_model(self, request, obj):
        self._audit(request, obj, "delete")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self._audit(request, obj, "delete")
        super().delete_queryset(request, queryset)

    def save_formset(self, request, form, formset, change):
        # The inlines of an organization (bank accounts, fiat links) are audited as their own models.
        updated = {f.instance.pk: sorted(f.changed_data) for f in formset.forms
                  if f.instance.pk and f.has_changed() and not f.cleaned_data.get("DELETE")}
        removed = [f.instance for f in formset.forms if f.instance.pk and f.cleaned_data.get("DELETE")]
        for obj in removed:
            audit.record(request.user, f"{_PREFIX[type(obj)]}.delete", obj, {"fields": [], "via": "admin"})
        instances = formset.save()
        for obj in instances:
            if obj.pk in updated:
                fields, action = updated[obj.pk], "update"
            else:
                fields = next((sorted(f.changed_data) for f in formset.forms if f.instance is obj), [])
                action = "create"
            audit.record(request.user, f"{_PREFIX[type(obj)]}.{action}", obj, {"fields": fields, "via": "admin"})


_PREFIX = {Organization: "organization", BankAccount: "bankaccount", FiatLink: "fiatlink"}


class BankAccountInline(admin.StackedInline):
    model = BankAccount
    extra = 0


class FiatLinkInline(admin.StackedInline):
    model = FiatLink
    extra = 0


def _account_label(obj) -> str:
    noun = "bank account" if isinstance(obj, BankAccount) else "payment link"
    kind = obj.currency if isinstance(obj, BankAccount) else obj.kind
    return f"{noun} “{obj.label or '(no label)'}” (#{obj.pk}, {kind}) of organization {obj.org.slug}"


@admin.register(Organization)
class OrganizationAdmin(StepUpAdminMixin, AuditedAdmin):
    """The US-resident issuer flag, the settings (which hold the return hosts) and the bank and fiat inlines are
    step-up changes (audit SEC-3): told to every superuser. The rest of the organization is not."""

    audit_prefix = "organization"
    step_up_noun = "organization"
    step_up_fields = ("is_us_resident_issuer", "settings")
    step_up_inlines = {BankAccount: "bank account", FiatLink: "payment link"}

    def step_up_label(self, obj):
        return f"organization {obj.slug} (#{obj.pk})"

    def step_up_extra(self, obj, fields):
        hosts = (obj.settings or {}).get("return_url_hosts") if isinstance(obj.settings, dict) else None
        return (f" The US-resident issuer flag is {'on' if obj.is_us_resident_issuer else 'off'}; the return hosts "
                f"are {', '.join(hosts) if hosts else 'none'}.")
    list_display = ("slug", "default_language", "is_us_resident_issuer", "next_number")
    readonly_fields = ("next_number",)  # a reset counter would reissue numbers
    inlines = [BankAccountInline, FiatLinkInline]


@admin.register(BankAccount)
class BankAccountAdmin(StepUpAdminMixin, AuditedAdmin):
    audit_prefix = "bankaccount"
    step_up_noun = "bank account"

    def step_up_label(self, obj):
        return _account_label(obj)
    list_display = ("org", "currency", "label", "active")


@admin.register(FiatLink)
class FiatLinkAdmin(StepUpAdminMixin, AuditedAdmin):
    audit_prefix = "fiatlink"
    step_up_noun = "payment link"

    def step_up_label(self, obj):
        return _account_label(obj)
    list_display = ("org", "kind", "label", "active")
