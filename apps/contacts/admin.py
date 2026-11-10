from django.contrib import admin

from apps.audit import services as audit
from apps.contacts.models import Contact, ContactEmail
from apps.staff.admin_stepup import StepUpAdminMixin
from apps.vault.fields import blind_index


class ContactEmailInline(admin.TabularInline):
    model = ContactEmail
    extra = 0
    fields = ("address", "primary")


@admin.register(Contact)
class ContactAdmin(StepUpAdminMixin, admin.ModelAdmin):
    """The e-mail addresses are a step-up change (audit SEC-3): letters, pay links and PDFs go there, so an
    address added, edited, made primary or removed is told to every superuser, by the contact's id only. The
    contact's other fields are not."""

    step_up_noun = "contact"
    step_up_fields = ()
    step_up_inlines = {ContactEmail: "e-mail address"}
    # Encrypted fields cannot be searched, filtered or ordered; search is by exact e-mail only.
    list_display = ("id", "org", "preferred_language", "us_tax_resident", "of_record", "created_at")
    list_filter = ("org", "us_tax_resident", "of_record")
    ordering = ("-created_at",)
    search_fields = ("emails__address_index",)
    inlines = [ContactEmailInline]

    def get_readonly_fields(self, request, obj=None):
        # Moving a contact to another organization would leave its invoices and e-mail rows behind;
        # `copy_for_org` is shown (whose copy this is, if anyone's) and set only by the issuing rule.
        # `of_record` likewise: an unpaid payment-link checkout's own contact becomes one when its order is paid.
        return ("org", "copy_for_org", "of_record") if obj is not None else ()

    def save_model(self, request, obj, form, change):
        old_flag = form.initial.get("us_tax_resident")
        super().save_model(request, obj, form, change)
        if not change:
            audit.record(request.user, "contact.create", obj, {"us_tax_resident": obj.us_tax_resident, "via": "admin"})
            return
        changed = sorted(form.changed_data)
        if changed:
            detail = {"fields": changed, "via": "admin"}
            if "us_tax_resident" in changed:
                detail["us_tax_resident"] = {"from": old_flag, "to": obj.us_tax_resident}
            audit.record(request.user, "contact.update", obj, detail)

    def get_search_results(self, request, queryset, search_term):
        if not search_term.strip():
            return queryset, False
        ids = ContactEmail.objects.filter(address_index=blind_index(search_term)).values("contact_id")
        return queryset.filter(pk__in=ids), False
