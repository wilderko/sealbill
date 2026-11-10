from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
VAULT_KEY_PATH = env("VAULT_KEY_PATH", default="/tmp/pay-vault.key")  # noqa: F405
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "ratelimit": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "ratelimit",
                  "OPTIONS": {"MAX_ENTRIES": 100000}},
}
# A receiver on this machine (http://localhost:...) is fine in development.
WEBHOOK_ALLOW_HTTP = env.bool("WEBHOOK_ALLOW_HTTP", default=True)  # noqa: F405
WEBHOOK_ALLOW_PRIVATE = env.bool("WEBHOOK_ALLOW_PRIVATE", default=True)  # noqa: F405
