from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, TestCase

from apps.vault.testing import UnsealedVaultMixin
from config import admin_path
from config.admin_path import check_admin_path

from .testing import verified_staff_client
from .views import CLIENT_ROOTS

ADMIN = "/hidden-admin-xyz/"  # DJANGO_ADMIN_PATH in config/settings/test.py


class HiddenAdminTest(UnsealedVaultMixin, TestCase):
    def test_anonymous_and_staff_get_404(self):
        self.assertEqual(Client().get(ADMIN).status_code, 404)
        self.assertEqual(Client().get(ADMIN + "login/").status_code, 404)
        self.assertEqual(verified_staff_client().get(ADMIN).status_code, 404)
        self.assertEqual(verified_staff_client().get(ADMIN + "login/").status_code, 404)

    def test_unverified_superuser_is_sent_to_verify(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("boss", password="pw-boss-1")
        browser = Client()
        browser.force_login(user)
        r = browser.get(ADMIN)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/verify/?next=%2Fhidden-admin-xyz%2F")

    def test_verified_superuser_gets_the_admin(self):
        self.assertEqual(verified_staff_client(superuser=True).get(ADMIN).status_code, 200)

    def test_old_admin_path_is_404_without_redirect(self):
        r = verified_staff_client(superuser=True).get("/admin/")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Location", r)

    def test_csp_relaxed_only_under_the_admin_path(self):
        r = verified_staff_client(superuser=True).get(ADMIN)
        self.assertIn("style-src 'self' 'unsafe-inline'", r["Content-Security-Policy"])
        r = verified_staff_client().get("/")
        self.assertNotIn("unsafe-inline", r["Content-Security-Policy"])


class HiddenPathLooksLikeAnyOtherTest(UnsealedVaultMixin, TestCase):
    PATHS = (ADMIN, ADMIN + "login/", "/no-such-page-xyz/")

    def assert_same(self, browser):
        seen = []
        for path in self.PATHS:
            r = browser.get(path)
            self.assertEqual(r.status_code, 404, path)
            seen.append((r.get("Content-Security-Policy", "").split("'nonce-")[0], r.get("Cache-Control")))
        self.assertEqual(len(set(seen)), 1, seen)

    def test_anonymous(self):
        self.assert_same(Client())

    def test_verified_non_superuser(self):
        self.assert_same(verified_staff_client())

    def test_verify_redirect_is_not_cached(self):
        from django.contrib.auth import get_user_model

        browser = Client()
        browser.force_login(get_user_model().objects.create_superuser("boss2", password="pw-boss-1"))
        self.assertIn("no-store", browser.get(ADMIN)["Cache-Control"])


class SealedHiddenPathTest(UnsealedVaultMixin, TestCase):
    def test_sealed_vault_answers_alike(self):
        from apps.vault.vault import vault

        vault.seal()
        a, b = Client().get(ADMIN), Client().get("/no-such-page-xyz/")
        self.assertEqual((a.status_code, a["Location"]), (b.status_code, b["Location"]))
        self.assertEqual(a["Location"], "/unlock/")


class AdminPathCheckTest(SimpleTestCase):
    def test_client_roots_match_the_staff_views(self):
        self.assertEqual(admin_path.CLIENT_ROOTS, CLIENT_ROOTS)

    def test_production_refuses_a_weak_admin_path(self):
        for bad in ("", "django-admin/", "short/", "a" * 30, "api/" + "x" * 30 + "/", "i/" + "x" * 30 + "/",
                    "/" + "x" * 30 + "/", "x" * 20 + "/../" + "y" * 10 + "/", "x" * 20 + "//" + "y" * 10 + "/",
                    "staff/" + "x" * 30 + "/", "admin/" + "x" * 30 + "/",
                    "change-me-to-a-long-random-path/"):  # the placeholder of the example env files
            with self.subTest(bad=bad), self.assertRaises(ImproperlyConfigured):
                check_admin_path(bad, CLIENT_ROOTS)
        check_admin_path("Zq3" + "x" * 29 + "/", CLIENT_ROOTS)
