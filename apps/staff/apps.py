from django.apps import AppConfig
from django.contrib import admin
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache


class StaffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.staff"

    def ready(self):
        # The admin is for superusers who passed the second factor. Anyone else gets a 404, so the
        # path says nothing about itself; the one exception is a superuser who has yet to verify. A change
        # (any request but GET, HEAD or OPTIONS) also needs a second factor from the last ten minutes.
        def has_permission(request):
            user = request.user
            return bool(user.is_active and user.is_superuser and user.is_verified())

        @never_cache
        def verify_redirect(request):
            return redirect(reverse("staff:verify") + "?" + urlencode({"next": request.get_full_path()}))

        def refuse(request):
            user = request.user
            if user.is_authenticated and user.is_active and user.is_superuser:
                return verify_redirect(request)
            raise Http404  # nothing added: exactly what a path that does not exist answers

        original_admin_view = admin.site.admin_view
        reading = ("GET", "HEAD", "OPTIONS")

        def needs_step_up(request) -> bool:
            """A change through the admin needs what the staff pages' step-up powers need: a second factor
            from the last ten minutes (audit SEC-3, review S5 I-1). Reading does not, nor signing out."""
            from . import passkeys

            if request.method in reading or passkeys.recently_verified(request):
                return False
            return request.path != reverse("admin:logout")

        def admin_view(view, cacheable=False):
            inner = original_admin_view(view, cacheable)

            def wrapped(request, *args, **kwargs):
                if not admin.site.has_permission(request):
                    return refuse(request)
                if needs_step_up(request):
                    from django.contrib import messages

                    from .stepup import CONFIRM_FIRST

                    messages.warning(request, CONFIRM_FIRST)
                    return verify_redirect(request)  # nothing is saved; the page is there again once verified
                return inner(request, *args, **kwargs)

            return wrapped

        def login(request, extra_context=None):
            return refuse(request)

        admin.site.has_permission = has_permission
        admin.site.admin_view = admin_view
        admin.site.login = login
