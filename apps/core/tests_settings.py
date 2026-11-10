"""Production settings, imported with the required environment stubbed."""
import importlib
import logging
import os
import sys
from unittest import mock

from django.test import SimpleTestCase

ENV = {"SECRET_KEY": "x" * 50, "ALLOWED_HOSTS": "pay.example.org", "DATABASE_URL": "postgres://u:p@db:5432/pay",
       "SITE_URL": "https://pay.example.org", "VAULT_KEY_PATH": "/run/pay-invoicing/vault.key",
       "DJANGO_ADMIN_PATH": "x7Kq-prod-admin-path-0123456789/"}


def production_settings():
    with mock.patch.dict(os.environ, ENV):
        sys.modules.pop("config.settings.production", None)
        try:
            return importlib.import_module("config.settings.production")
        finally:
            sys.modules.pop("config.settings.production", None)


class ProductionLoggingTests(SimpleTestCase):
    def setUp(self):
        self.logging = production_settings().LOGGING

    def test_console_handler_and_levels(self):
        conf = self.logging
        self.assertEqual(conf["version"], 1)
        self.assertFalse(conf["disable_existing_loggers"])
        console = conf["handlers"]["console"]
        self.assertEqual(console["class"], "logging.StreamHandler")
        self.assertEqual(console["level"], "INFO")
        loggers = conf["loggers"]
        self.assertEqual((loggers["apps"]["level"], loggers["apps"]["handlers"]), ("INFO", ["console"]))
        for name in ("django.request", "django.security"):
            self.assertEqual((loggers[name]["level"], loggers[name]["handlers"]), ("ERROR", ["console"]), name)

    def test_formatters_carry_no_request_details(self):
        for name, formatter in self.logging["formatters"].items():
            fmt = formatter["format"]
            for field in ("%(request", "%(server_time", "%(status_code", "%(pathname", "%(args"):
                self.assertNotIn(field, fmt, name)
        self.assertNotIn("mail_admins", str(self.logging["loggers"]))

    def test_capability_tokens_are_redacted_from_logged_paths(self):
        from django.utils.module_loading import import_string

        spec = self.logging["filters"]["redact_tokens"]
        flt = import_string(spec["()"])()
        self.assertIn("redact_tokens", self.logging["handlers"]["console"]["filters"])
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "%s: %s",
                                   ("Internal Server Error", "/p/SECRETtoken123_-/declare"), None)
        self.assertTrue(flt.filter(record))
        self.assertEqual(record.getMessage(), "Internal Server Error: /p/<token>/declare")
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Not Found: %s", ("/i/abc/",), None)
        flt.filter(record)
        self.assertEqual(record.getMessage(), "Not Found: /i/<token>/")

    def test_every_capability_path_is_redacted(self):
        # /i/ (view), /p/ (pay), /k/ (key confirmation) and /l/ (payment link): each token is the only
        # credential of its page.
        from django.utils.module_loading import import_string

        flt = import_string(self.logging["filters"]["redact_tokens"]["()"])()
        token = "Zr4_8xQ-tok3n"
        cases = {
            f"/i/{token}/": "/i/<token>/",
            f"/i/{token}/key/": "/i/<token>/key/",
            f"/i/{token}/pdf": "/i/<token>/pdf",
            f"/p/{token}/quote/": "/p/<token>/quote/",
            f"/k/{token}/": "/k/<token>/",
            f"/k/{token}/?lang=sk": "/k/<token>/?lang=sk",
            f"/l/{token}/": "/l/<token>/",  # a payment link (I-1)
            f"/l/{token}/?promoCode=AUTUMN": "/l/<token>/?promoCode=AUTUMN",
        }
        for path, shown in cases.items():
            record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Service Unavailable: %s",
                                       (path,), None)
            self.assertTrue(flt.filter(record))
            self.assertEqual(record.getMessage(), f"Service Unavailable: {shown}", path)
        # Paths that merely contain the letters stay as they are.
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Not Found: %s",
                                   ("/keys/12/revoke/",), None)
        flt.filter(record)
        self.assertEqual(record.getMessage(), "Not Found: /keys/12/revoke/")

    def test_urllib3_logs_errors_only(self):
        # Its WARNING "Failed to parse headers (url=...)" carries the full URL, query string and all.
        loggers = self.logging["loggers"]
        self.assertEqual(loggers["urllib3"]["level"], "ERROR")
        self.assertEqual(loggers["urllib3"]["handlers"], ["console"])
        self.assertFalse(loggers["urllib3"]["propagate"])


def _test_settings(env):
    """config.settings.test as a fresh process would read it, with exactly this environment."""
    import subprocess

    from django.conf import settings

    full = {"PATH": os.environ.get("PATH", ""), **env}
    done = subprocess.run(
        [sys.executable, "-c", "import config.settings.test as s\nd = s.DATABASES['default']\n"
                               "print(d['ENGINE'], d['NAME'])"],
        cwd=settings.BASE_DIR, env=full, capture_output=True, text=True, timeout=60,
    )
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


class TestSettingsDatabaseTests(SimpleTestCase):
    def test_without_database_url_the_tests_use_sqlite_in_memory(self):
        self.assertEqual(_test_settings({}), "django.db.backends.sqlite3 :memory:")

    def test_database_url_decides_the_engine(self):
        # The PostgreSQL-only tests run on the server with DATABASE_URL set (plan C, P4): that run
        # must really be PostgreSQL, never SQLite under a PostgreSQL name.
        self.assertEqual(_test_settings({"DATABASE_URL": "sqlite:////tmp/pay-tests-url.sqlite3"}),
                         "django.db.backends.sqlite3 /tmp/pay-tests-url.sqlite3")
        engine, name = _test_settings({"DATABASE_URL": "postgres:///payinv?host=/var/run/postgresql"}).split()
        self.assertEqual((engine, name), ("django.db.backends.postgresql", "payinv"))


class StaticFilesTests(SimpleTestCase):
    """The pages' own CSS and scripts live in the top-level static/ directory, which no app owns."""

    OWN_FILES = ("ui/app.css", "ui/ui.js", "portal/htmx.min.js", "portal/quote.js", "fonts/inter-latin.woff2",
                 "fonts/inter-cyrillic.woff2", "fonts/jetbrains-mono-regular-latin.woff2",
                 "fonts/jetbrains-mono-regular-cyrillic.woff2")

    def test_finders_find_the_pages_own_files(self):
        from django.contrib.staticfiles import finders

        for name in self.OWN_FILES:
            self.assertIsNotNone(finders.find(name), name)

    def test_collectstatic_puts_them_in_the_production_manifest(self):
        # Production uses the manifest storage: a {% static %} name missing from the manifest is a
        # 500 on every page that names it, and the test client never asks for a static file.
        import json
        import tempfile

        from django.core.management import call_command
        from django.test import override_settings

        with tempfile.TemporaryDirectory() as root:
            storages = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
            with override_settings(STATIC_ROOT=root, STORAGES=storages):
                call_command("collectstatic", interactive=False, verbosity=0)
            with open(os.path.join(root, "staticfiles.json")) as f:
                paths = json.load(f)["paths"]
        for name in self.OWN_FILES:
            self.assertIn(name, paths, name)
