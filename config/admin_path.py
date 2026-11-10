"""Validation of DJANGO_ADMIN_PATH. Pure: imports no Django app, so settings and tests can use it."""
from django.core.exceptions import ImproperlyConfigured

# Mirrors apps.staff.views.CLIENT_ROOTS (a test keeps the two equal); settings cannot import the app.
CLIENT_ROOTS = ("healthz", "theme", "unlock", "i", "p", "k", "l", "api", "hooks", "llms.txt")
MIN_LENGTH = 24


def check_admin_path(value: str, client_roots: tuple[str, ...]) -> None:
    """Refuse an admin path that is guessable or that would shadow a client-facing route."""
    if not value:
        raise ImproperlyConfigured("DJANGO_ADMIN_PATH must be set in production")
    if value == "django-admin/":
        raise ImproperlyConfigured("DJANGO_ADMIN_PATH must not be the default 'django-admin/'")
    if value.startswith("change-me"):
        raise ImproperlyConfigured("DJANGO_ADMIN_PATH is still the placeholder from the example file")
    if len(value) < MIN_LENGTH:
        raise ImproperlyConfigured(f"DJANGO_ADMIN_PATH must be at least {MIN_LENGTH} characters")
    if not value.endswith("/"):
        raise ImproperlyConfigured("DJANGO_ADMIN_PATH must end with '/'")
    if value.startswith("/") or ".." in value or "//" in value:
        raise ImproperlyConfigured("DJANGO_ADMIN_PATH must not start with '/' or contain '..' or '//'")
    first = value.split("/", 1)[0]
    if first in client_roots or first in ("staff", "admin"):
        raise ImproperlyConfigured(f"DJANGO_ADMIN_PATH must not start with the route '{first}'")
