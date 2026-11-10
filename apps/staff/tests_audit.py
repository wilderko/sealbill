"""Regression tests from the invoicing audit of 2026-11-03 that concern the staff login.

WEB-2: anyone could keep a named staff account, and the admin, locked out with 11 POSTs every 5
minutes, because the per-username bucket refused the right password too. The bucket stays (it caps
distributed password guessing), but a browser that has completed password and TOTP before carries a
signed device cookie naming the user, and skips it. The cookie never skips TOTP, nor the per-address
bucket, and a password change invalidates it.

WEB-3: a request whose address is already refused no longer charges the username bucket, and the
rate-limit store never culls (its `MAX_ENTRIES` is never reached; the hourly `cache_purge` empties it).

`StaffLockoutTest`, `StaffLockoutWithCsrfTest` and `RateLimitCullTest` are ported from
`docs/superpowers/audit/2026-11-03-invoicing/poc/web/poc_web_2.py` and `poc_web_3.py` in the
liberation.services repository.
"""
import re
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django_otp.oath import TOTP

from apps.vault.ratelimit import rate_limited
from apps.vault.testing import UnsealedVaultMixin

COOKIE = "pay_staff_device"
PASSWORD = "the-right-password-1"


def token_for(device) -> str:
    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = time.time()
    return f"{t.token():0{device.digits}d}"


def make_device(user):
    from apps.staff.models import StaffTOTPDevice

    return StaffTOTPDevice.objects.create(user=user, name="t", confirmed=True)


def strip_per_response(content: bytes) -> bytes:
    """The CSRF token and the CSP nonce differ per response by design; nothing else may."""
    return re.sub(rb'(csrfmiddlewaretoken" value|nonce)="[^"]+"', b"", content)


class LoginBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        caches["ratelimit"].clear(); cache.clear()
        self.staff = get_user_model().objects.create_user("alice", password=PASSWORD, is_staff=True)
        self.device = make_device(self.staff)

    def sign_in(self, client, ip="192.0.2.10", username="alice"):
        """Password, then TOTP: what sets the device cookie."""
        r = client.post("/login/", {"username": username, "password": PASSWORD}, HTTP_X_FORWARDED_FOR=ip)
        self.assertEqual(r.status_code, 302, r.content[:300])
        r = client.post("/verify/", {"token": token_for(self.device)}, HTTP_X_FORWARDED_FOR=ip)
        self.assertEqual(r.status_code, 302, r.content[:300])
        return r

    def end_session(self, client):
        """The browser is closed: the session cookie goes (SESSION_EXPIRE_AT_BROWSER_CLOSE), the
        90-day device cookie stays."""
        client.cookies.pop("sessionid", None)

    def flood(self, username="Alice", ip="203.0.113.66", n=11):
        attacker = Client()
        return [attacker.post("/login/", {"username": username, "password": "x"},
                              HTTP_X_FORWARDED_FOR=ip).status_code for _ in range(n)]


class StaffLockoutTest(LoginBase):
    """poc_web_2: an outsider names the account 11 times; no password needed.

    Adapted to the fix the controller ruled (WEB-2 is the device cookie): the staff member's own
    browser has signed in before, so it carries the device cookie, and the attacker uses a browser
    of their own. A browser that never signed in is still held by the account bucket: see
    `NewBrowserTest`."""

    def test_outsider_locks_staff_out_with_eleven_requests(self):
        staff_office = "192.0.2.10"  # an address that never failed
        self.sign_in(self.client, staff_office)
        self.end_session(self.client)
        self.flood()  # no password needed; the 11th is already refused for the attacker too
        r = self.client.post("/login/", {"username": "alice", "password": PASSWORD},
                             HTTP_X_FORWARDED_FOR=staff_office)
        self.assertEqual(r.status_code, 302, "a correct password from an address that never failed is refused")


class StaffLockoutWithCsrfTest(LoginBase):
    """The same, through Django's CSRF check as a script would pass it: one GET for the cookie and
    the form token, a Referer naming the site (HTTPS requests are checked for one)."""

    def csrf(self, client, url, ip):
        page = client.get(url, secure=True, HTTP_X_FORWARDED_FOR=ip)
        return re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()

    def post(self, client, url, data, ip):
        data = {**data, "csrfmiddlewaretoken": self.csrf(client, url, ip)}
        return client.post(url, data, secure=True, HTTP_REFERER=f"https://testserver{url}", HTTP_X_FORWARDED_FOR=ip)

    def test_csrf_is_no_obstacle(self):
        # Adaptation (WEB-2 ruling): the staff member's browser signed in once before, with CSRF
        # enforced as a browser does it, and was then closed.
        staff = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(staff, "/login/", {"username": "alice", "password": PASSWORD},
                                   "192.0.2.10").status_code, 302)
        self.assertEqual(self.post(staff, "/verify/", {"token": token_for(self.device)},
                                   "192.0.2.10").status_code, 302)
        self.end_session(staff)

        bot = Client(enforce_csrf_checks=True)
        page = bot.get("/login/", secure=True, HTTP_X_FORWARDED_FOR="203.0.113.66")
        token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
        codes = [bot.post("/login/", {"username": "alice", "password": "x", "csrfmiddlewaretoken": token},
                          secure=True, HTTP_REFERER="https://testserver/login/",
                          HTTP_X_FORWARDED_FOR="203.0.113.66").status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)
        r = self.post(staff, "/login/", {"username": "alice", "password": PASSWORD}, "192.0.2.10")
        self.assertEqual(r.status_code, 302)


class DeviceCookieTest(LoginBase):
    def test_the_cookie_is_set_after_password_and_totp_only(self):
        r = self.client.post("/login/", {"username": "alice", "password": PASSWORD})
        self.assertNotIn(COOKIE, r.cookies)  # the password alone is not enough
        r = self.client.post("/verify/", {"token": "000000"})
        self.assertNotIn(COOKIE, r.cookies)
        type(self.device).objects.filter(pk=self.device.pk).update(throttling_failure_count=0)  # django-otp's delay
        r = self.client.post("/verify/", {"token": token_for(self.device)})
        cookie = r.cookies[COOKIE]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(int(cookie["max-age"]), 90 * 24 * 60 * 60)
        self.assertNotIn("alice", cookie.value)  # it names the user by id, not by the login name
        self.assertNotIn(self.staff.password, cookie.value)
        self.assertNotIn(self.staff.get_session_auth_hash(), cookie.value)

    def test_the_cookie_never_skips_totp(self):
        self.sign_in(self.client)
        self.end_session(self.client)
        self.flood()
        r = self.client.post("/login/", {"username": "alice", "password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/verify/"), r["Location"])
        self.assertTrue(self.client.get("/")["Location"].startswith("/verify/"))

    def test_the_cookie_does_not_skip_the_per_address_bucket(self):
        self.sign_in(self.client, "192.0.2.10")
        self.end_session(self.client)
        for _ in range(9):  # with the sign-in above, ten from this address
            self.client.post("/login/", {"username": "alice", "password": "bad"}, HTTP_X_FORWARDED_FOR="192.0.2.10")
        r = self.client.post("/login/", {"username": "alice", "password": PASSWORD},
                             HTTP_X_FORWARDED_FOR="192.0.2.10")
        self.assertEqual(r.status_code, 429)

    def test_the_cookie_is_for_its_own_user_only(self):
        other = get_user_model().objects.create_user("clerk", password=PASSWORD, is_staff=True)
        self.sign_in(self.client)
        self.end_session(self.client)
        self.flood(username="clerk")
        r = self.client.post("/login/", {"username": "clerk", "password": PASSWORD}, HTTP_X_FORWARDED_FOR="192.0.2.77")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(other.pk)

    def test_a_password_change_invalidates_the_cookie(self):
        self.sign_in(self.client)
        self.end_session(self.client)
        self.staff.set_password("a-new-password-2")
        self.staff.save()
        self.flood()
        r = self.client.post("/login/", {"username": "alice", "password": "a-new-password-2"},
                             HTTP_X_FORWARDED_FOR="192.0.2.77")
        self.assertEqual(r.status_code, 429)

    def test_a_tampered_or_foreign_cookie_is_ignored_without_a_500(self):
        self.flood()
        from django.core import signing

        for value in ("garbage", "1:abc:def", signing.get_cookie_signer(salt="something-else").sign("1:x:y")):
            self.client.cookies[COOKIE] = value
            r = self.client.post("/login/", {"username": "alice", "password": PASSWORD},
                                 HTTP_X_FORWARDED_FOR="192.0.2.77")
            self.assertEqual(r.status_code, 429, value)

    def test_a_cookie_of_a_user_no_longer_staff_is_ignored(self):
        self.sign_in(self.client)
        self.end_session(self.client)
        type(self.staff).objects.filter(pk=self.staff.pk).update(is_staff=False)
        self.flood()
        r = self.client.post("/login/", {"username": "alice", "password": PASSWORD},
                             HTTP_X_FORWARDED_FOR="192.0.2.77")
        self.assertEqual(r.status_code, 429)

    def test_a_stolen_cookie_has_a_bucket_of_its_own(self):
        # A device cookie lifts the account bucket for that browser, not the cap on guessing: the
        # attempts made with it are counted against the cookie, from whatever addresses.
        self.sign_in(self.client)
        self.end_session(self.client)
        codes = [self.client.post("/login/", {"username": "alice", "password": "bad"},
                                  HTTP_X_FORWARDED_FOR=f"198.51.100.{i}").status_code for i in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)


class NewBrowserTest(LoginBase):
    """Review focus 5: a person who has never signed in from this browser, whose account bucket
    is full, gets the existing 429 page: no 500, and nothing that tells which accounts exist."""

    def test_a_new_browser_with_a_full_account_bucket_gets_the_same_429_as_any_name(self):
        self.flood("alice", ip="203.0.113.1")
        self.flood("nobody-at-all", ip="203.0.113.2")
        real = Client().post("/login/", {"username": "alice", "password": PASSWORD},
                             HTTP_X_FORWARDED_FOR="192.0.2.50")
        unknown = Client().post("/login/", {"username": "nobody-at-all", "password": PASSWORD},
                                HTTP_X_FORWARDED_FOR="192.0.2.51")
        self.assertEqual((real.status_code, unknown.status_code), (429, 429))
        self.assertContains(real, "Too many attempts. Try again in a few minutes.", status_code=429)
        self.assertEqual(strip_per_response(real.content), strip_per_response(unknown.content))
        self.assertNotIn(COOKIE, real.cookies)


class FloodOrderTest(LoginBase):
    def test_a_refused_address_charges_no_username_bucket(self):
        for i in range(10):
            self.client.post("/login/", {"username": f"name{i}", "password": "x"}, HTTP_X_FORWARDED_FOR="203.0.113.66")
        store = caches["ratelimit"]
        for i in range(10, 40):
            r = self.client.post("/login/", {"username": f"name{i}", "password": "x"},
                                 HTTP_X_FORWARDED_FOR="203.0.113.66")
            self.assertEqual(r.status_code, 429)
            self.assertIsNone(store.get(f"rl:staff-login-user:name{i}"))
        # ...and the names of the accepted ten are still counted.
        self.assertIsNotNone(store.get("rl:staff-login-user:name0"))


DB_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "ratelimit": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "pay_ratelimit_poc",
                  "OPTIONS": {"MAX_ENTRIES": 60}},
}


@override_settings(CACHES=DB_CACHES)
class RateLimitCullTest(UnsealedVaultMixin, TestCase):
    """poc_web_3: one address mints unlimited rate-limit keys, and the database cache culls the
    lowest-sorting third of the counters, the per-link key-upload limit among them.

    Production uses DatabaseCache (config/settings/base.py). Here the same backend runs with
    MAX_ENTRIES 60 so the PoC is quick; the mechanism does not depend on the size."""

    def setUp(self):
        call_command("createcachetable", "pay_ratelimit_poc", verbosity=0)
        super().setUp()

    def test_login_flood_from_one_address_resets_the_key_upload_limit(self):
        link = "0123456789abcdef"  # tokens.hash_token(token)[:16], what apps/keys/views.py keys on
        for _ in range(5):  # UPLOAD_LIMIT reached: five confirmation letters sent to the contact
            self.assertFalse(rate_limited("key-upload", link, 5, 3600))
        self.assertTrue(rate_limited("key-upload", link, 5, 3600))  # the sixth is refused, as designed

        # One attacker address, refused by its own per-IP bucket after ten tries, keeps going.
        for i in range(70):
            r = self.client.post("/login/", {"username": f"zz{i:06d}", "password": "x"},
                                 HTTP_X_FORWARDED_FOR="203.0.113.66")
        self.assertEqual(r.status_code, 429)
        allowed_again = not rate_limited("key-upload", link, 5, 3600)
        self.assertFalse(allowed_again, "a login flood from one address reset another page's limit")


class NoCullTest(TestCase):
    def test_the_production_rate_limit_store_is_never_culled(self):
        from apps.core.tests_settings import production_settings

        store = production_settings().CACHES["ratelimit"]
        self.assertEqual(store["BACKEND"], "django.core.cache.backends.db.DatabaseCache")
        # Never reached: a cull would delete the lowest-sorting third of the live counters. The
        # hourly cache_purge job deletes the expired ones instead.
        self.assertGreaterEqual(store["OPTIONS"]["MAX_ENTRIES"], 10**12)


# ---------------------------------------------------------------------------
# TOTP seeds (plan A Task 10, fixed at the owner's word): encrypted under the vault key.
# ---------------------------------------------------------------------------
def staff_device_model():
    from apps.staff.models import StaffTOTPDevice

    return StaffTOTPDevice


def code_from_secret(secret_hex: str, offset_steps: int = 0) -> str:
    """What an authenticator app holding `secret_hex` shows now (or `offset_steps` steps away)."""
    from binascii import unhexlify

    from django_otp.oath import totp

    return f"{totp(unhexlify(secret_hex), t0=-30 * offset_steps):06d}"


class TotpSeedInPlaintextTest(UnsealedVaultMixin, TestCase):
    """poc_data_3 (known item, plan A Task 10): the staff second factor's seed was a plaintext column,
    readable with the vault sealed, and in every backup. The PoC read the column with the ORM and
    asked the device to verify while sealed; both now need the vault, so the port reads the column
    with SQL (what a dump holds) and asks the device once the vault is open again."""

    def test_database_alone_does_not_yield_a_staff_totp_code(self):
        from binascii import Error as HexError, unhexlify

        from django.db import connection
        from django_otp.oath import totp

        from apps.vault.testing import TEST_VAULT_PASSWORD
        from apps.vault.vault import vault

        Device = staff_device_model()
        user = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        device = Device.objects.create(user=user, name="authenticator", confirmed=True)
        vault.seal()
        self.assertFalse(vault.is_unsealed())
        with connection.cursor() as cursor:  # what a dump holds
            cursor.execute(f"SELECT {connection.ops.quote_name('key')} FROM {Device._meta.db_table} WHERE id = %s",
                           [device.pk])
            seed = cursor.fetchone()[0]
        try:
            code = f"{totp(unhexlify(seed)):06d}"
        except (HexError, ValueError):
            code = None  # not a hex secret at all
        vault.unseal(TEST_VAULT_PASSWORD)
        accepted = code is not None and Device.objects.get(pk=device.pk).verify_token(code)
        self.assertFalse(accepted, "the TOTP seed is plaintext in the database (and in every backup)")
        self.assertNotIn(device.key, seed)
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self.assertFalse(TOTPDevice.objects.exists())  # nothing went to django-otp's plaintext table


class StaffDeviceTest(UnsealedVaultMixin, TestCase):
    """django-otp's verification is the one in use: replay protection, drift, throttling."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.device = staff_device_model().objects.create(user=self.user, name="authenticator", confirmed=True)

    def fresh(self):
        return type(self.device).objects.get(pk=self.device.pk)

    def test_a_code_verifies_once(self):
        code = code_from_secret(self.device.key)
        self.assertTrue(self.fresh().verify_token(code))
        self.assertFalse(self.fresh().verify_token(code))  # the same time step again: refused

    def test_throttling_is_django_otps(self):
        self.assertFalse(self.fresh().verify_token("000000" if code_from_secret(self.device.key) != "000000" else "111111"))
        device = self.fresh()
        self.assertEqual(device.throttling_failure_count, 1)
        self.assertIsNotNone(device.throttling_failure_timestamp)
        self.assertFalse(device.verify_token(code_from_secret(self.device.key)))  # inside the 1 s delay
        type(self.device).objects.filter(pk=self.device.pk).update(
            throttling_failure_timestamp=device.throttling_failure_timestamp - __import__("datetime").timedelta(seconds=5))
        self.assertTrue(self.fresh().verify_token(code_from_secret(self.device.key)))
        self.assertEqual(self.fresh().throttling_failure_count, 0)

    def test_drift_is_learned(self):
        self.assertTrue(self.fresh().verify_token(code_from_secret(self.device.key, offset_steps=1)))
        self.assertEqual(self.fresh().drift, 1)

    def test_the_setup_url_carries_the_secret_once(self):
        from base64 import b32encode
        from binascii import unhexlify

        url = self.device.config_url
        self.assertTrue(url.startswith("otpauth://totp/staff?"))
        self.assertIn(b32encode(unhexlify(self.device.key)).decode(), url)

    def test_loading_a_device_does_not_open_its_secret(self):
        # The session check loads the device on every staff request: the secret stays shut until
        # a code is verified.
        device = self.fresh()
        self.assertIn("key", device.get_deferred_fields())

    def test_a_sealed_vault_with_a_signed_in_session_does_not_500(self):
        from apps.vault.vault import vault

        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/verify/", {"token": code_from_secret(self.device.key)}).status_code, 302)
        vault.seal()
        self.assertEqual(self.client.get("/unlock/").status_code, 200)
        self.assertEqual(self.client.get("/")["Location"], "/unlock/")
        # The session check (django-otp's middleware) still loads the device: its secret stays shut.
        from django_otp.models import Device

        self.assertEqual(Device.from_persistent_id(self.device.persistent_id).pk, self.device.pk)


class LoginUsesTheEncryptedDeviceTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("staff", password=PASSWORD, is_staff=True)

    def test_a_plaintext_django_otp_device_is_not_accepted(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        plain = TOTPDevice.objects.create(user=self.user, name="left over", confirmed=True)
        self.client.force_login(self.user)
        r = self.client.post("/verify/", {"token": code_from_secret(plain.key)})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "That code was not accepted.")

    def test_the_encrypted_device_is(self):
        device = staff_device_model().objects.create(user=self.user, name="authenticator", confirmed=True)
        self.client.force_login(self.user)
        r = self.client.post("/verify/", {"token": code_from_secret(device.key)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_an_unconfirmed_device_is_not(self):
        device = staff_device_model().objects.create(user=self.user, name="authenticator", confirmed=False)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/verify/", {"token": code_from_secret(device.key)}).status_code, 200)


class CreateStaffTotpTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("staff", password=PASSWORD, is_staff=True)

    def test_the_command_creates_an_encrypted_device(self):
        import io

        out = io.StringIO()
        call_command("create_staff_totp", "staff", stdout=out)
        device = staff_device_model().objects.get(user=self.user)
        self.assertTrue(device.confirmed)
        self.assertIn("otpauth://totp/", out.getvalue())
        self.assertIn(device.config_url, out.getvalue())
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self.assertFalse(TOTPDevice.objects.exists())

    def test_replace_removes_every_earlier_device(self):
        import io

        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="plaintext", confirmed=True)
        call_command("create_staff_totp", "staff", stdout=io.StringIO())
        call_command("create_staff_totp", "staff", "--replace", stdout=io.StringIO())
        self.assertEqual(staff_device_model().objects.filter(user=self.user).count(), 1)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_a_sealed_vault_is_said_plainly(self):
        import io

        from django.core.management.base import CommandError

        from apps.vault.vault import vault

        vault.seal()
        with self.assertRaises(CommandError) as caught:
            call_command("create_staff_totp", "staff", stdout=io.StringIO())
        self.assertIn("unseal", str(caught.exception))


class AdminShowsNoSecretTest(UnsealedVaultMixin, TestCase):
    def test_the_admin_never_shows_the_secret(self):
        from base64 import b32encode
        from binascii import unhexlify

        from django.contrib import admin
        from django_otp.plugins.otp_totp.models import TOTPDevice

        admin_user = get_user_model().objects.create_superuser("root", password=PASSWORD)
        own = staff_device_model().objects.create(user=admin_user, name="root's", confirmed=True)
        other_user = get_user_model().objects.create_user("staff", password=PASSWORD, is_staff=True)
        device = staff_device_model().objects.create(user=other_user, name="authenticator", confirmed=True)
        self.client.force_login(admin_user)
        self.assertEqual(self.client.post("/verify/", {"token": code_from_secret(own.key)}).status_code, 302)
        secrets = (device.key, device.key.upper(), b32encode(unhexlify(device.key)).decode())
        base = "/hidden-admin-xyz/staff/stafftotpdevice/"
        for url in (base, f"{base}{device.pk}/change/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            for secret in secrets:
                self.assertNotContains(r, secret)
            self.assertNotContains(r, 'name="key"')
        self.assertEqual(self.client.get(f"{base}add/").status_code, 403)  # devices come from create_staff_totp
        self.assertFalse(admin.site.is_registered(TOTPDevice))  # django-otp's own admin shows the key
        # Editing what is shown (resetting the throttle, say) keeps the secret as it was.
        r = self.client.post(f"{base}{device.pk}/change/", {"name": "renamed", "confirmed": "on",
                                                            "throttling_failure_count": "0"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        fresh = staff_device_model().objects.get(pk=device.pk)
        self.assertEqual((fresh.name, fresh.key), ("renamed", device.key))


class TotpMigrationTest(TransactionTestCase):
    """The data migration moves the existing device (production has one, the owner's, created the day
    of the audit fix) without changing its secret, so the owner's authenticator keeps working."""

    BEFORE = [("staff", None)]

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from apps.vault.testing import TEST_VAULT_PASSWORD
        from apps.vault.vault import vault

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        o = override_settings(VAULT_KEY_PATH=str(d / "run" / "key"), VAULT_CONFIG_PATH=str(d / "cfg" / "vault.json"))
        o.enable()
        self.addCleanup(o.disable)
        vault._key = vault._seen = None
        vault.init(TEST_VAULT_PASSWORD)
        vault.unseal(TEST_VAULT_PASSWORD)
        self.addCleanup(vault.seal)
        self.addCleanup(self.migrate_to_latest)
        self.executor().migrate(self.BEFORE)

    def executor(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        return MigrationExecutor(connection)

    def migrate_to_latest(self):
        executor = self.executor()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def owners_device(self):
        import datetime

        from django.utils import timezone
        from django_otp.plugins.otp_totp.models import TOTPDevice

        owner = get_user_model().objects.create_user("alice", password=PASSWORD, is_staff=True, is_superuser=True)
        created = timezone.now() - datetime.timedelta(hours=5)
        device = TOTPDevice.objects.create(user=owner, name="authenticator", confirmed=True, drift=1, last_t=7,
                                           throttling_failure_count=2,
                                           throttling_failure_timestamp=timezone.now() - datetime.timedelta(hours=1))
        TOTPDevice.objects.filter(pk=device.pk).update(created_at=created)
        device.refresh_from_db()
        return owner, device

    def test_existing_totp_device_still_verifies_after_the_migration(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        owner, old = self.owners_device()
        unconfirmed = TOTPDevice.objects.create(user=owner, name="never scanned", confirmed=False)
        self.migrate_to_latest()

        Device = staff_device_model()
        moved = Device.objects.get(user=owner, name="authenticator")
        self.assertEqual(moved.key, old.key)  # the secret, unchanged
        self.assertEqual((moved.confirmed, moved.drift, moved.last_t, moved.step, moved.t0, moved.digits,
                          moved.tolerance, moved.throttling_failure_count, moved.throttling_failure_timestamp,
                          moved.created_at),
                         (True, 1, 7, old.step, old.t0, old.digits, old.tolerance, 2, old.throttling_failure_timestamp,
                          old.created_at))
        self.assertEqual(Device.objects.get(user=owner, name="never scanned").key, unconfirmed.key)
        self.assertFalse(TOTPDevice.objects.exists())  # the plaintext rows are gone
        # What the owner's authenticator app shows now still signs them in.
        self.client.force_login(owner)
        r = self.client.post("/verify/", {"token": code_from_secret(old.key)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_a_sealed_vault_stops_the_migration_and_loses_nothing(self):
        from django.core.management.base import CommandError
        from django.db import connection
        from django.db.migrations.recorder import MigrationRecorder
        from django_otp.plugins.otp_totp.models import TOTPDevice

        from apps.vault.testing import TEST_VAULT_PASSWORD
        from apps.vault.vault import vault

        owner, old = self.owners_device()
        vault.seal()
        with self.assertRaises(CommandError) as caught:
            self.migrate_to_latest()
        self.assertIn("unseal the vault first, then migrate", str(caught.exception))
        self.assertEqual(list(TOTPDevice.objects.values_list("pk", "key")), [(old.pk, old.key)])
        applied = {name for app, name in MigrationRecorder(connection).applied_migrations() if app == "staff"}
        self.assertNotIn("0002_move_totp_devices", applied)
        self.assertEqual(staff_device_model().objects.count(), 0)
        # Unsealed, the same migrate moves it.
        vault.unseal(TEST_VAULT_PASSWORD)
        self.migrate_to_latest()
        self.assertEqual(staff_device_model().objects.get(user=owner).key, old.key)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_the_sealed_hint_keeps_the_password_out_of_the_shell_history(self):
        """Final review F3: the hint used to show the password typed into the command line, where the
        shell history keeps it. It reads it with `read -rsp` and pipes it to payinv-manage, as the README does."""
        import importlib

        hint = importlib.import_module("apps.staff.migrations.0002_move_totp_devices").SEALED
        self.assertIn("unseal the vault first, then migrate", hint)
        self.assertIn("read -rsp", hint)
        self.assertIn("payinv-manage unseal_vault", hint)
        self.assertIn("unset p", hint)
        self.assertNotIn("<the vault password>", hint)
        self.assertNotRegex(hint, r"printf '%s\\n' '")  # no password between quotes on the command line

    def test_with_no_device_to_move_it_runs_sealed(self):
        from apps.vault.vault import vault

        vault.seal()
        self.migrate_to_latest()
        self.assertEqual(staff_device_model().objects.count(), 0)

    def test_it_reverses(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        owner, old = self.owners_device()
        self.migrate_to_latest()
        self.executor().migrate(self.BEFORE)
        back = TOTPDevice.objects.get(user=owner)
        self.assertEqual((back.key, back.drift, back.last_t, back.created_at), (old.key, 1, 7, old.created_at))
