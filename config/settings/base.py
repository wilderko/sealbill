from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = False
# True under config.settings.production only: `check --deploy` and `healthcheck` then judge
# PROXY_SECRET and SECRET_KEY (apps.core.checks).
PRODUCTION = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "apps.core",
    "apps.vault",
    "apps.orgs",
    "apps.contacts",
    "apps.keys",
    "apps.invoices",
    "apps.notify",
    "apps.payments",
    "apps.rails",
    "apps.quotes",
    "apps.audit",
    "apps.staff",
    "apps.portal",
    "apps.api",
    "apps.webhooks",
    "apps.exports",
    "apps.links",
]

MIDDLEWARE = [
    "apps.core.proxy.ProxySecretMiddleware",  # first: without the proxy's secret nothing else runs
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # static files from STATIC_ROOT, no web-server config
    "apps.core.csp.ContentSecurityPolicyMiddleware",
    "apps.core.client_pages.CapabilityPageHeadersMiddleware",  # outside the vault gate: its 503 too
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.vault.middleware.VaultGateMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.csp.csp_nonce",
                "apps.core.theme.context",
                "apps.staff.context.navigation",
                "apps.portal.context.issuer",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("sk", "Slovenčina"),
    ("cs", "Čeština"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("fr", "Français"),
    ("pl", "Polski"),
    ("ko", "한국어"),
    ("ru", "Русский"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = env("STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))
# The pages' own CSS and scripts (static/portal/) belong to no app, so the app finder never sees them.
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.api.auth.BearerTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["apps.api.permissions.HasScope"],
    "DEFAULT_THROTTLE_CLASSES": ["apps.api.permissions.TokenThrottle"],
    "EXCEPTION_HANDLER": "apps.api.views.exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

# OpenAPI 3.1; the docs UI is served from drf-spectacular-sidecar (no CDN, no inline script).
SPECTACULAR_SETTINGS = {
    "TITLE": "Sealbill API",
    "DESCRIPTION": "Invoices, contacts and payments. Agents: see /llms.txt.",
    "VERSION": "1.0.0",
    "OAS_VERSION": "3.1.0",
    "SWAGGER_UI_SETTINGS": {"deepLinking": True, "validatorUrl": None},
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "COMPONENT_SPLIT_REQUEST": True,
}

# Vault: the key lives only in RAM and in this tmpfs file, never in the environment.
VAULT_KEY_PATH = env("VAULT_KEY_PATH", default="/run/vault/key")
VAULT_CONFIG_PATH = env("VAULT_CONFIG_PATH", default=str(BASE_DIR / "vault" / "vault.json"))

SITE_URL = env("SITE_URL", default="http://localhost:8000")
# Set (production behind Apache): every request must carry it in X-Pay-Proxy-Secret, which only the
# proxy sends (apps.core.proxy). Empty: nothing is checked (development, tests, Docker).
PROXY_SECRET = env("PROXY_SECRET", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="invoices@localhost")
# The largest PDF attached as it is; an encrypted letter gets half (notify.services.attachment_budget).
EMAIL_ATTACHMENT_BUDGET_BYTES = 7 * 1024 * 1024
STAFF_NOTIFY_EMAILS = env.list("STAFF_NOTIFY_EMAILS", default=[])
# One reminder to the addressee of an invoice this system mails itself, this many days after it was
# sent and still unpaid (apps.invoices.reminders). 0 or None: no reminders.
INVOICE_REMINDER_DAYS = env.int("INVOICE_REMINDER_DAYS", default=30)
# Language of the note stored on the payment of an invoice imported from Zaprite / Request Finance: "en" or "sk".
MIGRATION_NOTE_LANGUAGE = env("MIGRATION_NOTE_LANGUAGE", default="en")

# Encrypted e-mail: every gpg run gets a throw-away home under GPG_TMP_DIR (tmpfs in production,
# under the service's RuntimeDirectory), deleted after use. None: the system temp directory.
GPG_BINARY = env("GPG_BINARY", default="gpg")
GPG_TMP_DIR = env("GPG_TMP_DIR", default=None)

# Shared by all workers: rate-limit counters and the sealed-episode flag. They live in the
# database, never on the vault-key tmpfs (a 1 MiB RAM mount that must hold the key and
# nothing else). Create the tables with `manage.py createcachetable` (the Compose web service
# runs it on start); the worker's `cache_purge` job deletes expired rows every hour.
# Test and development settings use LocMem.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "pay_cache",
        "OPTIONS": {"MAX_ENTRIES": 10000},
    },
    # Rate-limit counters get their own store so a flood of throwaway keys can never cull
    # the vault-unlock counter or the sealed-episode flag out of the default cache. That store
    # never culls either: a cull deletes the lowest-sorting third of the live keys, whichever
    # limits they are (audit WEB-3). MAX_ENTRIES is never reached; `cache_purge` deletes the
    # expired counters every hour instead.
    "ratelimit": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "pay_ratelimit",
        "OPTIONS": {"MAX_ENTRIES": 10**12},
    },
}

FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700

# Staff sessions: eight hours, and gone when the browser closes.
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# Outgoing webhooks. HTTP is off under the test runner (see test.py). A destination must be https
# and resolve only to public addresses; the two switches lift that for a development receiver.
WEBHOOK_HTTP_ENABLED = env.bool("WEBHOOK_HTTP_ENABLED", default=True)
WEBHOOK_DNS_ENABLED = env.bool("WEBHOOK_DNS_ENABLED", default=True)  # off under the test runner too
WEBHOOK_ALLOW_HTTP = env.bool("WEBHOOK_ALLOW_HTTP", default=False)
WEBHOOK_ALLOW_PRIVATE = env.bool("WEBHOOK_ALLOW_PRIVATE", default=False)
# Addresses a webhook may never reach, refused like a private one: production lists this host's own
# public addresses, so a webhook cannot probe the services listening on them.
WEBHOOK_DENY_ADDRESSES = env.list("WEBHOOK_DENY_ADDRESSES", default=[])
# Full https URL prefixes (ending in "/") delivered to although their host resolves to a denied address: a
# receiver that lives on this host's own address (audit SEC-5). By prefix, never by host, so the same host's
# /server-status stays refused. Production: https://shop.example.com/payments/webhook/invoicing/
WEBHOOK_ALLOW_URL_PREFIXES = env.list("WEBHOOK_ALLOW_URL_PREFIXES", default=[])

# Crypto rails: outbound HTTP to chain APIs. Off under the test runner (see test.py).
RAILS_HTTP_ENABLED = env.bool("RAILS_HTTP_ENABLED", default=True)
# The rail URL guard's name lookups: closed only under the test runner (a test resolves a host by mocking
# socket.getaddrinfo). Not an operator switch, and independent of WEBHOOK_DNS_ENABLED.
RAILS_DNS_ENABLED = True
# A rail URL staff type (BTCPay, LND, CLN, LNbits) must resolve to public addresses only (audit SEC-4).
# An LND or CLN on this machine is reached on loopback only at this one port; unset, never.
LOCAL_LIGHTNING_PORT = int(env("LOCAL_LIGHTNING_PORT", default="").strip() or 0) or None
# {"arbitrum": ["https://..."]} replaces the built-in endpoints of that network (keys may be in the URL).
EVM_RPC_URLS = env.json("EVM_RPC_URLS", default={})
TRONGRID_API_URL = env("TRONGRID_API_URL", default="https://api.trongrid.io")
TRONGRID_API_KEY = env("TRONGRID_API_KEY", default="")
SOLANA_RPC_URLS = env.list("SOLANA_RPC_URLS", default=["https://api.mainnet-beta.solana.com",
                                                      "https://solana-rpc.publicnode.com"])
# TON stays off until the credit leg is verified (project 2): toncenter's jetton transfer, its hash
# and `transaction_aborted` describe the sender's debit, not the credit to our own jetton wallet.
# While off, a TON account cannot be saved active, is never scanned or quoted, and is not offered.
TON_ENABLED = env.bool("TON_ENABLED", default=False)
TONCENTER_API_URL = env("TONCENTER_API_URL", default="https://toncenter.com/api/v3")
TONCENTER_API_KEY = env("TONCENTER_API_KEY", default="")
FX_FRANKFURTER_URL = env("FX_FRANKFURTER_URL", default="https://api.frankfurter.dev/v1")

# Where the Django admin lives. Production refuses a short or guessable value (config/admin_path.py).
DJANGO_ADMIN_PATH = env("DJANGO_ADMIN_PATH", default="django-admin/")
