"""Step-up changes made through the hidden Django admin are told to every superuser (audit SEC-3, review S5
I-1), as the staff pages tell them (`stepup.announce`).

The admin's own gate (`apps.staff.apps`) already refuses any change without a second factor from the last ten
minutes; this mixin is the other half. It names things by label and id and lists the fields saved, never a
value: an IBAN, a wallet address, a credential or a client's e-mail address must not reach a plaintext letter.
The link in the letter is the staff audit log, not the admin, whose path is kept unguessable.
"""
from django.urls import reverse


def _announce(request, change: str, what: str) -> None:
    from apps.staff import stepup

    stepup.announce(request.user, change, what, reverse("staff:audit_list"))


class StepUpAdminMixin:
    """Put before the ModelAdmin it extends. `step_up_noun` names the model in the letter; `step_up_fields`
    narrows which changed fields count (None: every change and every creation counts); `step_up_inlines`
    maps an inline's model to its noun, and its additions, changes and removals are told too."""

    step_up_noun = ""
    step_up_fields: tuple | None = None
    step_up_inlines: dict = {}

    def step_up_label(self, obj) -> str:
        return f"{self.step_up_noun} #{obj.pk}"

    def step_up_extra(self, obj, fields) -> str:
        return ""

    def step_up_applies(self, request, obj) -> bool:
        return True

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        fields = sorted(form.changed_data)
        if self.step_up_fields is None:
            counts = bool(fields) or not change
        else:
            counts = any(f in self.step_up_fields for f in fields)
        if counts and self.step_up_applies(request, obj):
            verb = "Changed" if change else "Created"
            _announce(request, f"{self.step_up_noun} {verb.lower()} in the admin",
                      f"{verb} {self.step_up_label(obj)} in the Django admin. Fields saved: "
                      f"{', '.join(fields) or 'none'}.{self.step_up_extra(obj, fields)}")

    def delete_model(self, request, obj):
        label = self.step_up_label(obj) if self.step_up_applies(request, obj) else None  # before the pk goes
        super().delete_model(request, obj)
        if label:
            _announce(request, f"{self.step_up_noun} deleted in the admin", f"Deleted {label} in the Django admin.")

    def delete_queryset(self, request, queryset):
        labels = [self.step_up_label(o) for o in queryset if self.step_up_applies(request, o)]
        super().delete_queryset(request, queryset)
        if labels:
            _announce(request, f"{self.step_up_noun} deleted in the admin",
                      f"Deleted {', '.join(labels)} in the Django admin.")

    def save_formset(self, request, form, formset, change):
        noun = self.step_up_inlines.get(formset.model)
        if noun is None:
            return super().save_formset(request, form, formset, change)
        live = [f for f in formset.forms if f.has_changed() or f.instance.pk]
        added = sum(1 for f in live if not f.instance.pk and f.has_changed() and not f.cleaned_data.get("DELETE"))
        changed = [f"#{f.instance.pk} ({', '.join(sorted(f.changed_data))})" for f in live
                   if f.instance.pk and f.has_changed() and not f.cleaned_data.get("DELETE")]
        removed = [f"#{f.instance.pk}" for f in live if f.instance.pk and f.cleaned_data.get("DELETE")]
        result = super().save_formset(request, form, formset, change)
        parts = ([f"added {added}"] if added else []) + ([f"changed {', '.join(changed)}"] if changed else []) \
            + ([f"removed {', '.join(removed)}"] if removed else [])
        if parts:
            _announce(request, f"{noun} changed in the admin",
                      f"{noun.capitalize()}s of {self.step_up_label(form.instance)} in the Django admin: "
                      f"{'; '.join(parts)}.")
        return result
