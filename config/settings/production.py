import os

from django.core.exceptions import ImproperlyConfigured

from config.admin_path import CLIENT_ROOTS, check_admin_path

from .base import *  # noqa: F401,F403

# Nothing in production may fall back to a development default.
for _name in ("SECRET_KEY", "ALLOWED_HOSTS", "DATABASE_URL", "SITE_URL", "VAULT_KEY_PATH"):
    if not os.environ.get(_name, "").strip():
        raise ImproperlyConfigured(f"{_name} must be set in the environment for production")

# Chain and price endpoints come from the environment and are not guarded like a URL staff type:
# they must be https, or plain http on this machine's loopback, where a self-hosted node answers
# (red-team A5). Plain http anywhere else hands the rates and the chain data to the network.
def _plain_http_off_loopback(url) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(str(url).strip())
    if parts.scheme == "https":
        return False
    return not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "::1", "localhost"))


_endpoints = {
    "EVM_RPC_URLS": [u for urls in (EVM_RPC_URLS or {}).values() for u in (urls or [])],  # noqa: F405
    "SOLANA_RPC_URLS": list(SOLANA_RPC_URLS or []),  # noqa: F405
    "TRONGRID_API_URL": [TRONGRID_API_URL],  # noqa: F405
    "TONCENTER_API_URL": [TONCENTER_API_URL],  # noqa: F405
    "FX_FRANKFURTER_URL": [FX_FRANKFURTER_URL],  # noqa: F405
}
for _name, _urls in _endpoints.items():
    if any(_plain_http_off_loopback(u) for u in _urls if str(u).strip()):
        raise ImproperlyConfigured(f"{_name} must use https (plain http only on 127.0.0.1, ::1 or localhost)")

# No default in production: the admin path is a deployment secret.
DJANGO_ADMIN_PATH = env("DJANGO_ADMIN_PATH", default="")  # noqa: F405
check_admin_path(DJANGO_ADMIN_PATH, CLIENT_ROOTS)

PRODUCTION = True
SECRET_KEY = env("SECRET_KEY")  # noqa: F405
PROXY_SECRET = env("PROXY_SECRET", default="")  # noqa: F405

# Refused at start, not only by `check --deploy`, which nothing runs at boot (red-team A2). The
# placeholders are what the example files ship. An empty PROXY_SECRET lets every account that
# reaches the socket pick the address the rate limits count, so it has to be asked for by name.
for _name, _value in (("SECRET_KEY", SECRET_KEY), ("PROXY_SECRET", PROXY_SECRET)):
    if "change-me" in str(_value or "").strip().lower():
        raise ImproperlyConfigured(f"{_name} is still the placeholder from the example file")
if not PROXY_SECRET.strip() and not env.bool("ALLOW_EMPTY_PROXY_SECRET", default=False):  # noqa: F405
    raise ImproperlyConfigured(
        "PROXY_SECRET must be set in production, and the reverse proxy must send it (README). "
        "Set ALLOW_EMPTY_PROXY_SECRET=true instead only where no other account can reach the app."
    )
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
