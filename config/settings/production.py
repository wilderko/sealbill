import os

from django.core.exceptions import ImproperlyConfigured

from config.admin_path import CLIENT_ROOTS, check_admin_path

from .base import *  # noqa: F401,F403

# Nothing in production may fall back to a development default.
for _name in ("SECRET_KEY", "ALLOWED_HOSTS", "DATABASE_URL", "SITE_URL", "VAULT_KEY_PATH"):
    if not os.environ.get(_name, "").strip():
        raise ImproperlyConfigured(f"{_name} must be set in the environment for production")

# No default in production: the admin path is a deployment secret.
DJANGO_ADMIN_PATH = env("DJANGO_ADMIN_PATH", default="")  # noqa: F405
check_admin_path(DJANGO_ADMIN_PATH, CLIENT_ROOTS)

PRODUCTION = True
SECRET_KEY = env("SECRET_KEY")  # noqa: F405
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")  # noqa: F405
DATABASES = {"default": env.db("DATABASE_URL")}  # noqa: F405
DEBUG = False
CSRF_TRUSTED_ORIGINS = [SITE_URL]  # noqa: F405

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Throw-away GPG keyrings and OpenSSL temp files live beside the vault key, on the same tmpfs
# (the service's RuntimeDirectory). The code creates the directory 0700 on first use.
GPG_TMP_DIR = env("GPG_TMP_DIR", default=os.path.join(os.path.dirname(VAULT_KEY_PATH), "gpg"))  # noqa: F405

SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False  # other sites share the parent domain
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="localhost")  # noqa: F405
EMAIL_PORT = env.int("EMAIL_PORT", default=25)  # noqa: F405
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=30)  # noqa: F405

# Errors and our own INFO to stdout (journald / docker logs). The format carries no request
# details, and capability tokens are cut out of logged paths. Messages from apps.* never
# contain addresses or exception text by design.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_tokens": {"()": "apps.core.logfilters.RedactCapabilityTokens"}},
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler", "level": "INFO", "formatter": "plain", "filters": ["redact_tokens"],
        },
    },
    "loggers": {
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        # urllib3 logs each new connection's host at DEBUG (webhook receivers, chain endpoints), and
        # its WARNING "Failed to parse headers (url=...)" carries the full URL: errors only.
        "urllib3": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
