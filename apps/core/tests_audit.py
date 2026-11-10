"""Regression tests from the invoicing audit of 2026-11-03 that concern the deployment (WEB-8).

Production started without a word with `PROXY_SECRET` empty, or with the `change-me` placeholder
that `deploy/env.example` ships, so every `www-data` process could forge the address the rate limits
are keyed on. Startup is still not refused without the secret (residual R2: the Compose path has no
Apache to send it), but `check --deploy` and `manage.py healthcheck` now say so under production
settings, and refuse the placeholder for `PROXY_SECRET` and `SECRET_KEY`.
"""
import io
import re

from django.conf import settings
from django.core import checks
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.tests_health import beat
from apps.vault.testing import UnsealedVaultMixin

BASE = settings.BASE_DIR
GOOD = {"PRODUCTION": True, "PROXY_SECRET": "Zq0w8x-proxy-secret-from-token_urlsafe",
        "SECRET_KEY": "k" * 60}


def deploy_ids():
    return sorted(m.id for m in checks.run_checks(include_deployment_checks=True) if m.id.startswith("core."))


class DeployCheckTest(SimpleTestCase):
    def test_production_settings_say_they_are_production(self):
        from apps.core.tests_settings import production_settings

        self.assertTrue(production_settings().PRODUCTION)
        self.assertFalse(settings.PRODUCTION)

    @override_settings(**GOOD)
    def test_nothing_to_say_when_both_are_real(self):
        self.assertEqual(deploy_ids(), [])

    @override_settings(**{**GOOD, "PROXY_SECRET": ""})
    def test_an_empty_proxy_secret_is_a_warning(self):
        found = [m for m in checks.run_checks(include_deployment_checks=True) if m.id.startswith("core.")]
        self.assertEqual([(m.id, m.level) for m in found], [("core.W001", checks.WARNING)])
        self.assertIn("PROXY_SECRET", found[0].msg)

    @override_settings(**{**GOOD, "PROXY_SECRET": "change-me"})
    def test_the_proxy_secret_placeholder_is_an_error(self):
        found = [m for m in checks.run_checks(include_deployment_checks=True) if m.id.startswith("core.")]
        self.assertEqual([(m.id, m.level) for m in found], [("core.E001", checks.ERROR)])

    @override_settings(**{**GOOD, "SECRET_KEY": "change-me"})
    def test_the_secret_key_placeholder_is_an_error(self):
        found = [m for m in checks.run_checks(include_deployment_checks=True) if m.id.startswith("core.")]
        self.assertEqual([(m.id, m.level) for m in found], [("core.E002", checks.ERROR)])

    @override_settings(**{**GOOD, "PROXY_SECRET": "", "SECRET_KEY": "change-me"})
    def test_only_with_deploy_checks_and_only_under_production(self):
        self.assertEqual([m.id for m in checks.run_checks() if m.id.startswith("core.")], [])
        self.assertEqual(deploy_ids(), ["core.E002", "core.W001"])
        with self.settings(PRODUCTION=False):
            self.assertEqual(deploy_ids(), [])

    @override_settings(**{**GOOD, "PROXY_SECRET": " Change-Me "})
    def test_the_placeholder_is_recognised_however_it_is_typed(self):
        self.assertEqual(deploy_ids(), ["core.E001"])


class HealthcheckLineTest(UnsealedVaultMixin, TestCase):
    def run_cmd(self):
        out = io.StringIO()
        try:
            call_command("healthcheck", "--no-network", stdout=out)
        except SystemExit as exit_:
            return exit_.code, out.getvalue()
        return 0, out.getvalue()

    def setUp(self):
        super().setUp()
        beat()

    @override_settings(**GOOD)
    def test_quiet_when_both_are_real(self):
        self.assertEqual(self.run_cmd(), (0, ""))

    @override_settings(**{**GOOD, "PROXY_SECRET": ""})
    def test_an_empty_proxy_secret_is_a_line(self):
        self.assertEqual(self.run_cmd(), (1, "proxy_secret_empty 1\n"))

    @override_settings(**{**GOOD, "PROXY_SECRET": "change-me", "SECRET_KEY": "change-me"})
    def test_placeholders_are_lines(self):
        code, text = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(text.split("\n")[:-1]), ["proxy_secret_placeholder 1", "secret_key_placeholder 1"])
        self.assertNotIn("change-me", text)

    @override_settings(**{**GOOD, "PRODUCTION": False, "PROXY_SECRET": ""})
    def test_not_under_development_or_test_settings(self):
        self.assertEqual(self.run_cmd(), (0, ""))


class DocumentedTest(SimpleTestCase):
    def test_env_example_and_the_compose_section_name_the_proxy_secret(self):
        env = (BASE / ".env.example").read_text()
        self.assertRegex(env, r"(?m)^PROXY_SECRET=")
        compose = (BASE / "docs/operations.md").read_text().split("## Docker Compose")[1].split("\n## ")[0]
        self.assertIn("PROXY_SECRET", compose)
        self.assertIn("X-Pay-Proxy-Secret", compose)
        self.assertRegex((BASE / "deploy" / "env.example").read_text(), r"(?m)^PROXY_SECRET=")

    def test_the_readme_asks_for_a_long_vault_password(self):
        readme = (BASE / "docs/operations.md").read_text()
        self.assertTrue(re.search(r"16 characters", readme))
