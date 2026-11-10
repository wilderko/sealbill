from .base import *  # noqa: F401,F403

SECRET_KEY = "test-only-key"
DJANGO_ADMIN_PATH = "hidden-admin-xyz/"  # one stable value; tests address the admin at /hidden-admin-xyz/
PROXY_SECRET = ""  # a value in .env must not refuse every request of the suite (tests_proxy sets its own)
# SQLite in memory, unless DATABASE_URL names another database: the PostgreSQL-only tests run that
# way (a throwaway test_<name> database is created and dropped by the runner), and they must
# then really run on PostgreSQL, never silently on SQLite.
if env.str("DATABASE_URL", default="").strip():  # noqa: F405
    DATABASES = {"default": env.db("DATABASE_URL")}  # noqa: F405
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ALLOWED_HOSTS = ["testserver", "localhost"]
VAULT_KEY_PATH = "/tmp/pay-vault-test.key"
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "ratelimit": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "ratelimit",
                  "OPTIONS": {"MAX_ENTRIES": 100000}},
}
INSTALLED_APPS = [*INSTALLED_APPS, "apps.vault.tests_app"]  # noqa: F405
RAILS_HTTP_ENABLED = False
WEBHOOK_HTTP_ENABLED = False
WEBHOOK_DNS_ENABLED = False  # a test resolves a webhook host only by mocking socket.getaddrinfo
RAILS_DNS_ENABLED = False  # and a rail URL's host likewise (apps.rails.http)

# WhiteNoise warns when STATIC_ROOT does not exist; tests never run collectstatic.
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

STATIC_ROOT = _tempfile.mkdtemp(prefix="pay-test-static-")
_atexit.register(_shutil.rmtree, STATIC_ROOT, ignore_errors=True)
