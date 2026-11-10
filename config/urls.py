from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from apps.core.theme import set_theme
from apps.core.views import healthz
from apps.staff.legacy import staff_prefix_redirect

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("theme/", set_theme, name="theme"),
    path("", include("apps.vault.urls")),
    path("", include("apps.portal.urls")),
    path("", include("apps.links.urls")),
    path("", include("apps.keys.urls")),
    path("", include("apps.api.urls")),
    path("", include("apps.rails.urls")),
    re_path(r"^staff(?:/(?P<rest>.*))?$", staff_prefix_redirect),
    path(settings.DJANGO_ADMIN_PATH, admin.site.urls),
    path("", include("apps.staff.urls")),
]
