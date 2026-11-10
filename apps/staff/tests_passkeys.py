"""Passkeys for staff (spec 2026-11-04 §3): WebAuthn as the second factor beside the authenticator app.

The browser is played by `SoftAuthenticator`, a software authenticator on `cryptography` (EC P-256,
ES256, attestation "none"): it answers the options the server hands out with the JSON a browser
would post. No network, no browser.
"""
import hashlib
import json
import os
import re
import struct
from pathlib import Path
from unittest import mock

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from webauthn.helpers import bytes_to_base64url

from apps.audit.models import AuditEntry
from apps.staff import passkeys
from apps.staff.device_cookie import COOKIE
from apps.staff.models import StaffPasskey, StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import UnsealedVaultMixin

SITE = "https://invoices.example.test"
RP_ID = "invoices.example.test"
PASSWORD = "pw-clerk-1-long-enough"

SECURITY = "/security/"
REGISTER_OPTIONS = "/security/passkeys/options/"
REGISTER = "/security/passkeys/"
LOGIN_OPTIONS = "/verify/passkey/options/"
LOGIN = "/verify/passkey/"

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


def b64(data: bytes) -> str:
    return bytes_to_base64url(data)


class SoftAuthenticator:
    """One ES256 credential on a software authenticator. `create` and `get` answer the options the
    server sent, as the JSON the page posts; the keyword arguments make it misbehave on purpose."""

    def __init__(self, sign_count=0, counting=False):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = sign_count
        self.counting = counting  # a security key counts every use; a synced passkey reports 0

    @property
    def id(self) -> str:
        return b64(self.credential_id)

    def cose_key(self) -> bytes:
        n = self.private_key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def client_data(kind, challenge, origin) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def auth_data(self, rp_id, flags, count, attested=False) -> bytes:
        data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
        if attested:
            data += bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_key()
        return data

    def create(self, options, *, origin=SITE, rp_id=None, uv=True, challenge=None) -> dict:
        client_data = self.client_data("webauthn.create", challenge or options["challenge"], origin)
        flags = FLAG_UP | FLAG_AT | (FLAG_UV if uv else 0)
        auth_data = self.auth_data(rp_id or options["rp"]["id"], flags, self.sign_count, attested=True)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": self.id, "rawId": self.id, "type": "public-key", "authenticatorAttachment": "platform",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client_data), "attestationObject": b64(attestation),
                             "transports": ["internal", "hybrid"]}}

    def get(self, options, *, origin=SITE, rp_id=None, uv=True, sign_count=None, challenge=None) -> dict:
        if sign_count is None:
            if self.counting:
                self.sign_count += 1
            sign_count = self.sign_count
        client_data = self.client_data("webauthn.get", challenge or options["challenge"], origin)
        auth_data = self.auth_data(rp_id or options["rpId"], FLAG_UP | (FLAG_UV if uv else 0), sign_count)
        signature = self.private_key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self.id, "rawId": self.id, "type": "public-key", "authenticatorAttachment": "platform",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(client_data), "authenticatorData": b64(auth_data),
                             "signature": b64(signature), "userHandle": None}}


@override_settings(SITE_URL=SITE)
class PasskeyBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_user("clerk", password=PASSWORD, is_staff=True)
        self.totp = StaffTOTPDevice.objects.create(user=self.staff, name="phone app", confirmed=True)

    # -- the two steps of a sign-in
    def password(self, client=None, username="clerk", ip="192.0.2.10"):
        client = client or self.client
        r = client.post("/login/", {"username": username, "password": PASSWORD}, HTTP_X_FORWARDED_FOR=ip)
        self.assertEqual(r.status_code, 302, r.content[:300])

    def code(self, device=None) -> str:
        """A fresh authenticator code. django-otp refuses a code of a time step already used (its
        replay protection), and these tests sign in several times within one step, so the device
        forgets its last step first: what is tested here is the passkey, not that rule."""
        device = device or self.totp
        StaffTOTPDevice.objects.filter(pk=device.pk).update(last_t=-1, throttling_failure_count=0)
        device.refresh_from_db()
        return token_for(device)

    def sign_in(self, client=None, device=None):
        """Password and authenticator code: a verified session, verified just now."""
        client = client or self.client
        self.password(client)
        r = client.post("/verify/", {"token": self.code(device)})
        self.assertEqual(r.status_code, 302, r.content[:300])

    def sign_out(self, client=None):
        (client or self.client).post("/logout/")

    # -- the ceremonies, as the page's script runs them
    def options(self, url, client=None, status=200) -> dict:
        r = (client or self.client).post(url)
        self.assertEqual(r.status_code, status, r.content[:300])
        self.assertEqual(r["Content-Type"], "application/json")
        return r.json()

    def register(self, authenticator, name="Laptop", client=None, **misbehave):
        client = client or self.client
        credential = authenticator.create(self.options(REGISTER_OPTIONS, client), **misbehave)
        return client.post(REGISTER, {"name": name, "credential": json.dumps(credential)})

    def assertion(self, authenticator, client=None, **misbehave) -> str:
        return json.dumps(authenticator.get(self.options(LOGIN_OPTIONS, client), **misbehave))

    def passkey_login(self, authenticator, client=None, **misbehave):
        client = client or self.client
        credential = self.assertion(authenticator, client, **misbehave)
        return client.post(LOGIN, {"credential": credential, "next": "/invoices/"})

    def registered(self, authenticator=None, name="Laptop") -> SoftAuthenticator:
        """A passkey on the account, registered through the page by a fully signed-in session."""
        authenticator = authenticator or SoftAuthenticator()
        self.sign_in()
        r = self.register(authenticator, name)
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.sign_out()
        return authenticator

    def verified(self, client=None) -> bool:
        return (client or self.client).get("/").status_code == 200


class RegisterAndLogInTests(PasskeyBase):
    def test_register_then_log_in_with_the_passkey(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        r = self.register(authenticator, "MacBook Touch ID")
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertEqual(r["Location"], SECURITY)
        passkey = StaffPasskey.objects.get()
        self.assertEqual((passkey.user, passkey.name, passkey.credential_id), (self.staff, "MacBook Touch ID", authenticator.id))
        self.assertEqual(bytes(passkey.public_key), authenticator.cose_key())
        self.assertEqual(passkey.sign_count, 0)
        self.assertEqual(passkey.transports, ["hybrid", "internal"])
        self.assertIsNotNone(passkey.created_at)
        self.assertIsNone(passkey.last_used_at)

        self.sign_out()
        self.password()
        self.assertFalse(self.verified())
        r = self.passkey_login(authenticator)
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertEqual(r["Location"], "/invoices/")
        self.assertTrue(self.verified())
        self.assertEqual(self.client.session["otp_device_id"], passkey.persistent_id)
        self.assertIn(COOKIE, r.cookies)  # the device cookie, as after a code (audit WEB-2)
        passkey.refresh_from_db()
        self.assertIsNotNone(passkey.last_used_at)

    def test_registration_options_name_this_site_and_require_user_verification(self):
        existing = self.registered()
        self.sign_in()
        options = self.options(REGISTER_OPTIONS)
        self.assertEqual(options["rp"]["id"], RP_ID)
        self.assertEqual(options["attestation"], "none")
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "preferred")
        self.assertEqual(options["timeout"], 300000)
        self.assertEqual([c["id"] for c in options["excludeCredentials"]], [existing.id])
        self.assertEqual(options["user"]["name"], "clerk")
        self.assertNotIn("clerk", options["user"]["id"])  # the user handle names nobody
        self.assertGreaterEqual(len(options["challenge"]), 43)  # at least 32 random bytes

    def test_the_relying_party_comes_from_site_url_never_from_the_request(self):
        self.sign_in()
        r = self.client.post(REGISTER_OPTIONS, HTTP_HOST="localhost")
        self.assertEqual(r.json()["rp"]["id"], RP_ID)
        with override_settings(SITE_URL="https://invoices.example.test/"):
            self.assertEqual(passkeys.relying_party(), (RP_ID, SITE))
        # A challenge is kept on the server, in the session store, never in a cookie the browser holds.
        self.assertNotIn("signed_cookies", settings.SESSION_ENGINE)
        self.assertNotIn(r.json()["challenge"], str(self.client.cookies))

    def test_login_options_offer_this_accounts_passkeys_only(self):
        mine = self.registered()
        other = get_user_model().objects.create_user("other", password=PASSWORD, is_staff=True)
        StaffPasskey.objects.create(user=other, name="theirs", credential_id=b64(os.urandom(32)),
                                    public_key=SoftAuthenticator().cose_key())
        self.password()
        options = self.options(LOGIN_OPTIONS)
        self.assertEqual(options["rpId"], RP_ID)
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual([c["id"] for c in options["allowCredentials"]], [mine.id])
        self.assertEqual(options["allowCredentials"][0]["transports"], ["hybrid", "internal"])

    def test_a_synced_passkey_reporting_no_counter_logs_in_every_time(self):
        authenticator = self.registered()
        for _ in range(3):
            self.password()
            self.assertEqual(self.passkey_login(authenticator).status_code, 302)
            self.sign_out()
        self.assertEqual(StaffPasskey.objects.get().sign_count, 0)

    def test_a_counting_authenticator_advances_the_stored_counter(self):
        authenticator = self.registered(SoftAuthenticator(sign_count=1, counting=True))
        self.password()
        self.assertEqual(self.passkey_login(authenticator).status_code, 302)
        self.assertEqual(StaffPasskey.objects.get().sign_count, 2)

    def test_the_code_still_verifies_beside_a_passkey(self):
        self.registered()
        self.sign_in()
        self.assertTrue(self.verified())

    def test_a_passkey_never_accepts_a_code(self):
        self.registered()
        passkey = StaffPasskey.objects.get()
        self.assertFalse(passkey.verify_token("123456"))
        self.totp.delete()
        self.password()
        r = self.client.post("/verify/", {"token": "123456"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.verified())

    def test_a_password_alone_cannot_register_or_open_the_security_page(self):
        self.password()
        for url in (SECURITY,):
            self.assertTrue(self.client.get(url)["Location"].startswith("/verify/"))
        r = self.client.post(REGISTER_OPTIONS)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/verify/"))
        self.assertNotIn(passkeys.SESSION_CHALLENGE, self.client.session)

    def test_login_options_need_a_password_first_and_a_passkey_to_offer(self):
        r = self.client.post(LOGIN_OPTIONS)
        self.assertEqual(r.status_code, 403)
        self.password()
        body = self.options(LOGIN_OPTIONS, status=400)
        self.assertIn("No passkey", body["error"])
        customer = get_user_model().objects.create_user("cust", password=PASSWORD)
        other = Client()
        other.force_login(customer)
        self.assertEqual(other.post(LOGIN_OPTIONS).status_code, 403)
        self.assertEqual(other.post(LOGIN, {"credential": "{}"}).status_code, 403)

    def test_create_staff_totp_replace_keeps_the_passkeys(self):
        self.registered()
        call_command("create_staff_totp", "clerk", "--replace", stdout=open(os.devnull, "w"))
        self.assertEqual(StaffPasskey.objects.filter(user=self.staff).count(), 1)


class RefusalTests(PasskeyBase):
    def refused(self, r, client=None):
        self.assertEqual(r.status_code, 400, r.content[:500])
        self.assertContains(r, 'role="alert"', status_code=400)
        self.assertFalse(self.verified(client))

    def test_a_replayed_assertion_is_refused(self):
        authenticator = self.registered()
        self.password()
        credential = self.assertion(authenticator)
        self.assertEqual(self.client.post(LOGIN, {"credential": credential}).status_code, 302)
        r = self.client.post(LOGIN, {"credential": credential})  # again, in the same session
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "That passkey was not accepted.", status_code=400)
        self.sign_out()
        self.password()
        self.refused(self.client.post(LOGIN, {"credential": credential}))  # no challenge issued
        self.options(LOGIN_OPTIONS)
        self.refused(self.client.post(LOGIN, {"credential": credential}))  # another challenge
        attacker = Client()
        self.password(attacker)
        self.options(LOGIN_OPTIONS, attacker)
        self.refused(attacker.post(LOGIN, {"credential": credential}), attacker)  # another session

    def test_a_replayed_registration_is_refused(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        credential = json.dumps(authenticator.create(self.options(REGISTER_OPTIONS)))
        self.assertEqual(self.client.post(REGISTER, {"name": "a", "credential": credential}).status_code, 302)
        StaffPasskey.objects.all().delete()
        r = self.client.post(REGISTER, {"name": "a", "credential": credential})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(StaffPasskey.objects.exists())

    def test_a_credential_id_longer_than_1023_bytes_is_malformed(self):
        """Final review F5: WebAuthn caps a credential id at 1023 bytes and py_webauthn does not check it;
        a longer one would be a database error on PostgreSQL (the column holds 1366 characters)."""
        self.sign_in()
        too_long = SoftAuthenticator()
        too_long.credential_id = os.urandom(passkeys.MAX_CREDENTIAL_ID_BYTES + 1)
        with self.assertLogs("apps.staff.passkeys", "INFO") as logs:
            r = self.register(too_long)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(StaffPasskey.objects.exists())
        self.assertIn("malformed", logs.output[0])
        longest = SoftAuthenticator()
        longest.credential_id = os.urandom(passkeys.MAX_CREDENTIAL_ID_BYTES)
        self.assertEqual(self.register(longest).status_code, 302)
        stored = StaffPasskey.objects.get()
        self.assertLessEqual(len(stored.credential_id), StaffPasskey._meta.get_field("credential_id").max_length)

    def test_a_challenge_is_used_once_even_when_the_answer_was_refused(self):
        authenticator = self.registered()
        self.password()
        options = self.options(LOGIN_OPTIONS)
        bad = authenticator.get(options, origin="https://evil.example")
        self.refused(self.client.post(LOGIN, {"credential": json.dumps(bad)}))
        good = authenticator.get(options)
        self.refused(self.client.post(LOGIN, {"credential": json.dumps(good)}))

    def test_a_challenge_older_than_five_minutes_is_refused(self):
        authenticator = self.registered()
        self.password()
        credential = self.assertion(authenticator)
        later = passkeys._now() + passkeys.CHALLENGE_TTL_S + 1
        with mock.patch("apps.staff.passkeys._now", return_value=later):
            r = self.client.post(LOGIN, {"credential": credential})
        self.refused(r)
        self.assertContains(r, "expired", status_code=400)

    def test_a_wrong_origin_is_refused(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        r = self.register(authenticator, origin="https://invoices.example.test.evil.example")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(StaffPasskey.objects.exists())
        for origin in ("http://invoices.example.test", "https://evil.example", "https://invoices.example.test:8443"):
            r = self.register(authenticator, origin=origin)
            self.assertEqual(r.status_code, 400, origin)
        self.assertFalse(StaffPasskey.objects.exists())

        self.sign_out()
        authenticator = self.registered()
        self.password()
        self.refused(self.passkey_login(authenticator, origin="https://evil.example"))

    def test_a_wrong_rp_id_is_refused(self):
        self.sign_in()
        r = self.register(SoftAuthenticator(), rp_id="evil.example")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(StaffPasskey.objects.exists())

        self.sign_out()
        authenticator = self.registered()
        self.password()
        self.refused(self.passkey_login(authenticator, rp_id="example.test"))

    def test_a_sign_counter_going_backwards_is_refused(self):
        authenticator = self.registered(SoftAuthenticator(sign_count=5, counting=True))
        self.password()
        self.assertEqual(self.passkey_login(authenticator).status_code, 302)  # 6
        self.sign_out()
        for count in (6, 3, 0):
            self.password()
            with self.assertLogs("apps.staff.passkeys", "WARNING") as logs:
                self.refused(self.passkey_login(authenticator, sign_count=count))
            self.assertIn("sign count", logs.output[0])
            self.sign_out()
        self.assertEqual(StaffPasskey.objects.get().sign_count, 6)

    def test_a_removed_credential_is_refused(self):
        authenticator = self.registered()
        self.sign_in()
        passkey = StaffPasskey.objects.get()
        self.assertEqual(self.client.post(f"/security/passkeys/{passkey.pk}/remove/").status_code, 302)
        self.assertFalse(StaffPasskey.objects.exists())
        self.sign_out()
        self.password()
        another = SoftAuthenticator()  # the account keeps a passkey, so login options are issued at all
        StaffPasskey.objects.create(user=self.staff, name="other", credential_id=another.id,
                                    public_key=another.cose_key())
        self.refused(self.passkey_login(authenticator))

    def test_another_users_credential_is_refused(self):
        mine = self.registered()
        other = get_user_model().objects.create_user("other", password=PASSWORD, is_staff=True)
        theirs = SoftAuthenticator()
        StaffPasskey.objects.create(user=other, name="theirs", credential_id=theirs.id, public_key=theirs.cose_key())
        self.password()
        self.refused(self.passkey_login(theirs))
        self.assertEqual(self.passkey_login(mine).status_code, 302)

    def test_user_verification_is_required(self):
        self.sign_in()
        r = self.register(SoftAuthenticator(), uv=False)
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "PIN", status_code=400)
        self.assertFalse(StaffPasskey.objects.exists())

        self.sign_out()
        authenticator = self.registered()
        self.password()
        self.refused(self.passkey_login(authenticator, uv=False))

    def test_a_bad_signature_is_refused(self):
        authenticator = self.registered()
        self.password()
        credential = authenticator.get(self.options(LOGIN_OPTIONS))
        credential["response"]["signature"] = b64(SoftAuthenticator().private_key.sign(b"x", ec.ECDSA(hashes.SHA256())))
        self.refused(self.client.post(LOGIN, {"credential": json.dumps(credential)}))

    def test_garbage_is_refused_never_a_500(self):
        authenticator = self.registered()
        self.password()
        for junk in ("", "not json", "[]", "{}", '{"id": 1}', json.dumps({"id": authenticator.id, "rawId": "%%%",
                     "type": "public-key", "response": {}}), "[" * 5000 + "]" * 5000, "x" * 70000):
            self.options(LOGIN_OPTIONS)
            self.refused(self.client.post(LOGIN, {"credential": junk}))
        self.sign_in()
        for junk in ("", "not json", "{}", "[" * 5000 + "]" * 5000):
            self.options(REGISTER_OPTIONS)
            r = self.client.post(REGISTER, {"name": "x", "credential": junk})
            self.assertEqual(r.status_code, 400, junk[:20])
        self.assertEqual(StaffPasskey.objects.count(), 1)

    def test_the_same_credential_cannot_be_registered_twice(self):
        authenticator = self.registered()
        self.sign_in()
        r = self.register(authenticator, "again")
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "already", status_code=400)
        self.assertEqual(StaffPasskey.objects.count(), 1)


class RateLimitTests(PasskeyBase):
    def test_passkey_attempts_share_the_per_account_bucket_with_codes(self):
        authenticator = self.registered()
        self.password()
        caches["ratelimit"].clear()
        # Every attempt from an address of its own: only the account's bucket can refuse.
        for i in range(5):
            r = self.client.post("/verify/", {"token": "000000"}, HTTP_X_FORWARDED_FOR=f"192.0.2.{i}")
            self.assertEqual(r.status_code, 200)
            StaffTOTPDevice.objects.update(throttling_failure_count=0)
        for i in range(5):
            self.options(LOGIN_OPTIONS)
            r = self.client.post(LOGIN, {"credential": "{}"}, HTTP_X_FORWARDED_FOR=f"198.51.100.{i}")
            self.assertEqual(r.status_code, 400)
        credential = self.assertion(authenticator)
        r = self.client.post(LOGIN, {"credential": credential}, HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Too many attempts", status_code=429)
        self.assertFalse(self.verified())

    def test_passkey_attempts_are_counted_per_address_too(self):
        for name in ("one", "two"):
            get_user_model().objects.create_user(name, password=PASSWORD, is_staff=True)
        clients = [Client(), Client()]
        for client, name in zip(clients, ("one", "two")):
            self.password(client, name, ip="198.51.100.7")
        codes = []
        for i in range(11):
            client = clients[i % 2]
            codes.append(client.post(LOGIN, {"credential": "{}"}, HTTP_X_FORWARDED_FOR="198.51.100.7").status_code)
        self.assertEqual(codes[:10], [400] * 10)
        self.assertEqual(codes[10], 429)
        # The authenticator code at that address is held by the same bucket.
        r = clients[0].post("/verify/", {"token": "000000"}, HTTP_X_FORWARDED_FOR="198.51.100.7")
        self.assertEqual(r.status_code, 429)


class SecurityPageTests(PasskeyBase):
    def remove_url(self, device):
        kind = "passkeys" if isinstance(device, StaffPasskey) else "authenticators"
        return f"/security/{kind}/{device.pk}/remove/"

    def test_the_page_lists_passkeys_and_authenticators(self):
        self.registered(name="YubiKey 5C")
        self.sign_in()
        r = self.client.get(SECURITY)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "YubiKey 5C")
        self.assertContains(r, "phone app")
        self.assertContains(r, 'data-passkey="register"')
        self.assertContains(r, f'action="{self.remove_url(StaffPasskey.objects.get())}"')
        self.assertContains(r, f'action="{self.remove_url(self.totp)}"')
        self.assertContains(r, 'href="/security/"')  # the rail links to it

    def test_only_ones_own_second_factors_are_listed(self):
        other = get_user_model().objects.create_user("other", password=PASSWORD, is_staff=True)
        StaffTOTPDevice.objects.create(user=other, name="their phone", confirmed=True)
        self.sign_in()
        self.assertNotContains(self.client.get(SECURITY), "their phone")

    def test_removing_the_last_second_factor_is_refused(self):
        self.sign_in()
        r = self.client.post(self.remove_url(self.totp))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "last second factor", status_code=400)
        self.assertTrue(StaffTOTPDevice.objects.filter(pk=self.totp.pk).exists())

        self.assertEqual(self.register(SoftAuthenticator()).status_code, 302)
        self.assertEqual(self.client.post(self.remove_url(self.totp)).status_code, 302)
        self.assertFalse(StaffTOTPDevice.objects.exists())
        passkey = StaffPasskey.objects.get()
        r = self.client.post(self.remove_url(passkey))
        # The session rested on the code just removed: it verifies again before anything else.
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/verify/"))
        self.assertTrue(StaffPasskey.objects.filter(pk=passkey.pk).exists())

    def test_the_last_passkey_cannot_be_removed_either(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        self.register(authenticator)
        self.client.post(self.remove_url(self.totp))
        self.sign_out()
        self.password()
        self.assertEqual(self.passkey_login(authenticator).status_code, 302)
        passkey = StaffPasskey.objects.get()
        r = self.client.post(self.remove_url(passkey))
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "last second factor", status_code=400)
        self.assertTrue(StaffPasskey.objects.filter(pk=passkey.pk).exists())

    def test_the_ten_minute_window_is_exact_at_its_edge(self):
        """Final review F4: exactly RECENT_VERIFICATION_S after the verification is still inside the window;
        a second later is not."""
        self.registered()
        self.sign_in()
        now = 1_800_000_000
        for age, inside in ((0, True), (passkeys.RECENT_VERIFICATION_S, True), (passkeys.RECENT_VERIFICATION_S + 1, False)):
            session = self.client.session
            session[passkeys.SESSION_VERIFIED_AT] = now - age
            session.save()
            with mock.patch.object(passkeys, "_now", return_value=float(now)):
                page = self.client.get(SECURITY).content.decode()
                self.assertEqual('data-passkey="register"' in page, inside, age)
                self.assertEqual("Verify again" in page, not inside, age)
                r = self.client.post(REGISTER_OPTIONS)
                self.assertEqual(r.status_code, 200 if inside else 403, age)

    def test_removal_and_registration_need_a_verification_from_the_last_ten_minutes(self):
        self.registered()
        self.sign_in()
        passkey = StaffPasskey.objects.get()
        session = self.client.session
        # An int, as the login stores it: a float would be refused for its type and prove nothing (F4).
        session[passkeys.SESSION_VERIFIED_AT] = int(passkeys._now()) - passkeys.RECENT_VERIFICATION_S - 1
        session.save()
        page = self.client.get(SECURITY)
        self.assertContains(page, "Verify again")
        self.assertNotContains(page, 'data-passkey="register"')
        for url in (self.remove_url(passkey), self.remove_url(self.totp)):
            r = self.client.post(url)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r["Location"], "/verify/?next=%2Fsecurity%2F")
        r = self.client.post(REGISTER_OPTIONS)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["verify"], "/verify/?next=%2Fsecurity%2F")
        self.assertNotIn(passkeys.SESSION_CHALLENGE, self.client.session)
        r = self.client.post(REGISTER, {"name": "x", "credential": "{}"})
        self.assertEqual(r["Location"], "/verify/?next=%2Fsecurity%2F")
        self.assertEqual(StaffPasskey.objects.count(), 1)
        self.assertEqual(StaffTOTPDevice.objects.count(), 1)
        self.assertFalse(AuditEntry.objects.filter(action__in=["passkey.remove", "totp_device.remove"]).exists())

        # Verifying again, in the same session, opens the window.
        r = self.client.post("/verify/", {"token": self.code()})
        self.assertEqual(r["Location"], "/")
        self.assertEqual(self.client.post(self.remove_url(passkey)).status_code, 302)
        self.assertFalse(StaffPasskey.objects.exists())

    def test_a_session_verified_before_the_timestamp_existed_counts_as_stale(self):
        self.sign_in()
        session = self.client.session
        del session[passkeys.SESSION_VERIFIED_AT]
        session.save()
        self.assertTrue(self.verified())
        self.assertEqual(self.client.post(REGISTER_OPTIONS).status_code, 403)

    def test_a_passkey_login_opens_the_window_too(self):
        authenticator = self.registered()
        self.password()
        self.passkey_login(authenticator)
        self.assertEqual(self.client.post(self.remove_url(self.totp)).status_code, 302)
        self.assertFalse(StaffTOTPDevice.objects.exists())

    def test_removal_is_a_post_with_csrf(self):
        self.registered()
        self.sign_in()
        passkey = StaffPasskey.objects.get()
        self.assertEqual(self.client.get(self.remove_url(passkey)).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        self.assertEqual(strict.post(self.remove_url(passkey)).status_code, 403)
        self.assertEqual(strict.post(REGISTER_OPTIONS).status_code, 403)
        self.assertEqual(strict.post(REGISTER, {"name": "x", "credential": "{}"}).status_code, 403)
        self.assertEqual(self.client.get(REGISTER_OPTIONS).status_code, 405)
        self.assertEqual(self.client.get(REGISTER).status_code, 405)
        self.assertTrue(StaffPasskey.objects.filter(pk=passkey.pk).exists())

    def test_the_options_take_the_csrf_token_from_the_page_in_a_header(self):
        self.sign_in()
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        page = strict.get(SECURITY)
        token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
        r = strict.post(REGISTER_OPTIONS, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rp"]["id"], RP_ID)

    def test_another_users_device_cannot_be_removed(self):
        other = get_user_model().objects.create_user("other", password=PASSWORD, is_staff=True)
        theirs = StaffTOTPDevice.objects.create(user=other, name="t", confirmed=True)
        StaffTOTPDevice.objects.create(user=other, name="t2", confirmed=True)
        their_key = SoftAuthenticator()
        their_passkey = StaffPasskey.objects.create(user=other, name="k", credential_id=their_key.id,
                                                    public_key=their_key.cose_key())
        self.sign_in()
        self.assertEqual(self.client.post(self.remove_url(theirs)).status_code, 404)
        self.assertEqual(self.client.post(self.remove_url(their_passkey)).status_code, 404)
        self.assertEqual(StaffTOTPDevice.objects.filter(user=other).count(), 2)
        self.assertTrue(StaffPasskey.objects.filter(pk=their_passkey.pk).exists())

    def test_a_name_is_required_and_short(self):
        self.sign_in()
        for name in ("", "   ", "x" * 65):
            credential = json.dumps(SoftAuthenticator().create(self.options(REGISTER_OPTIONS)))
            r = self.client.post(REGISTER, {"name": name, "credential": credential})
            self.assertEqual(r.status_code, 400, name)
            self.assertNotIn(passkeys.SESSION_CHALLENGE, self.client.session)  # the challenge is spent
        self.assertFalse(StaffPasskey.objects.exists())


class AuditTests(PasskeyBase):
    def test_adding_and_removing_are_audited(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        self.register(authenticator, "Laptop")
        passkey = StaffPasskey.objects.get()
        added = AuditEntry.objects.get(action="passkey.add")
        self.assertEqual((added.actor, added.model_label, added.object_pk), (self.staff, "staff.staffpasskey", str(passkey.pk)))
        self.assertEqual(added.detail["name"], "Laptop")
        self.assertNotIn(authenticator.id, json.dumps(added.detail))  # no credential material

        self.client.post(f"/security/passkeys/{passkey.pk}/remove/")
        removed = AuditEntry.objects.get(action="passkey.remove")
        self.assertEqual((removed.actor, removed.object_pk, removed.detail["name"]), (self.staff, str(passkey.pk), "Laptop"))

        self.sign_out()
        self.registered()
        self.sign_in()
        totp_pk = self.totp.pk
        self.client.post(f"/security/authenticators/{totp_pk}/remove/")
        gone = AuditEntry.objects.get(action="totp_device.remove")
        self.assertEqual((gone.actor, gone.model_label, gone.object_pk, gone.detail["name"]),
                         (self.staff, "staff.stafftotpdevice", str(totp_pk), "phone app"))

    def test_refusals_are_not_audited_as_changes(self):
        self.sign_in()
        self.client.post(f"/security/authenticators/{self.totp.pk}/remove/")  # the last factor
        self.register(SoftAuthenticator(), uv=False)
        self.assertFalse(AuditEntry.objects.filter(action__startswith="passkey.").exists())
        self.assertFalse(AuditEntry.objects.filter(action__startswith="totp_device.").exists())


class VerifyPageTests(PasskeyBase):
    def test_the_passkey_choice_appears_only_for_an_account_with_one(self):
        self.password()
        page = self.client.get("/verify/")
        self.assertNotContains(page, 'data-passkey="login"')
        self.assertNotContains(page, "passkeys.js")
        self.assertContains(page, 'name="token"')
        self.assertContains(page, "autofocus")
        self.sign_out()
        self.registered()
        self.password()
        page = self.client.get("/verify/?next=/invoices/")
        self.assertContains(page, 'data-passkey="login"')
        self.assertContains(page, "Use a passkey")
        self.assertContains(page, f'data-options-url="{LOGIN_OPTIONS}"')
        self.assertContains(page, f'action="{LOGIN}"')
        self.assertContains(page, 'name="token"')
        self.assertContains(page, 'value="/invoices/"')
        self.assertNotContains(page, "autofocus")  # the passkey is the primary action

    def test_an_account_with_a_passkey_and_no_code_is_not_asked_for_a_code(self):
        authenticator = SoftAuthenticator()
        self.sign_in()
        self.register(authenticator)
        self.client.post(f"/security/authenticators/{self.totp.pk}/remove/")
        self.sign_out()
        self.password()
        page = self.client.get("/verify/")
        self.assertContains(page, 'data-passkey="login"')
        self.assertNotContains(page, 'name="token"')

    def test_the_next_page_is_kept_on_this_site(self):
        authenticator = self.registered()
        self.password()
        credential = self.assertion(authenticator)
        r = self.client.post(LOGIN, {"credential": credential, "next": "https://evil.example/"})
        self.assertEqual(r["Location"], "/")


class CspTests(PasskeyBase):
    SCRIPT = Path(settings.BASE_DIR) / "static" / "ui" / "passkeys.js"

    def assert_scripts_are_files_under_the_nonce(self, r):
        policy = r["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertIn("connect-src 'self'", policy)
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        page = r.content.decode()
        tags = re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S)
        self.assertTrue(tags)
        self.assertEqual([body.strip() for body in tags], [""] * len(tags))  # no inline script at all
        for tag in re.findall(r"<script\b[^>]*>", page):
            self.assertIn("src=", tag)
            self.assertIn(f'nonce="{nonce}"', tag)
        self.assertIn('src="/static/ui/passkeys.js"', page)
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", page, re.I))
        self.assertIsNone(re.search(r"\sstyle\s*=", page, re.I))

    def test_the_security_and_verify_pages_load_the_script_as_a_file_under_the_nonce(self):
        self.registered()
        self.password()
        self.assert_scripts_are_files_under_the_nonce(self.client.get("/verify/"))
        self.sign_in()
        self.assert_scripts_are_files_under_the_nonce(self.client.get(SECURITY))

    def test_the_script_is_one_static_file_that_evaluates_nothing(self):
        source = self.SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML",
                          "document.write", "setTimeout(\"", "setTimeout('", ".style."):
            self.assertNotIn(forbidden, source)
        self.assertIn("navigator.credentials.create", source)
        self.assertIn("navigator.credentials.get", source)
        self.assertIn("X-CSRFToken", source)
        self.assertIn('credentials: "same-origin"', source)
        self.assertEqual(sorted(p.name for p in self.SCRIPT.parent.glob("passkey*")), ["passkeys.js"])

    def test_the_options_answers_are_json_and_not_cached(self):
        self.sign_in()
        r = self.client.post(REGISTER_OPTIONS)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r["Cache-Control"], "no-store")
        self.registered()
        self.password()
        r = self.client.post(LOGIN_OPTIONS)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r["Cache-Control"], "no-store")


class AdminPasskeyAuditTests(PasskeyBase):
    """Final review F6: renaming or deleting a passkey in the Django admin is audited like the Security
    page's removal, by ids and names only, and says it was done in the admin. The admin stays the
    recovery path for a lost passkey, so it may delete an account's last second factor."""

    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root", password=PASSWORD)
        own = StaffTOTPDevice.objects.create(user=self.root, name="root's", confirmed=True)
        self.client.force_login(self.root)
        self.assertEqual(self.client.post("/verify/", {"token": self.code(own)}).status_code, 302)
        StaffTOTPDevice.objects.filter(user=self.staff).delete()  # the passkeys are the clerk's only factors
        self.keys = [StaffPasskey.objects.create(user=self.staff, name=name, confirmed=True,
                                                 credential_id=SoftAuthenticator().id,
                                                 public_key=SoftAuthenticator().cose_key())
                     for name in ("Laptop", "Phone", "Spare")]
        self.base = "/hidden-admin-xyz/staff/staffpasskey/"

    def entries(self, action):
        return list(AuditEntry.objects.filter(action=action).order_by("pk"))

    def test_rename_delete_and_bulk_delete_are_audited(self):
        laptop, phone, spare = self.keys
        r = self.client.post(f"{self.base}{laptop.pk}/change/", {"name": "Work laptop"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        (renamed,) = self.entries("passkey.rename")
        self.assertEqual((renamed.actor, renamed.object_pk), (self.root, str(laptop.pk)))
        self.assertEqual(renamed.detail, {"old_name": "Laptop", "name": "Work laptop", "user": self.staff.pk,
                                          "via": "admin"})

        r = self.client.post(f"{self.base}{phone.pk}/delete/", {"post": "yes"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        r = self.client.post(self.base, {"action": "delete_selected", "_selected_action": [laptop.pk, spare.pk],
                                         "post": "yes"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.assertFalse(StaffPasskey.objects.exists())  # the last factor too: this is the recovery path
        removed = self.entries("passkey.remove")
        self.assertEqual(sorted((e.object_pk, e.detail["name"]) for e in removed),
                         sorted([(str(phone.pk), "Phone"), (str(laptop.pk), "Work laptop"), (str(spare.pk), "Spare")]))
        for entry in removed:
            self.assertEqual((entry.actor, entry.detail["via"], entry.detail["user"]), (self.root, "admin", self.staff.pk))
            self.assertEqual(set(entry.detail), {"name", "user", "via"})  # ids and names only

    def test_saving_without_a_change_records_nothing(self):
        laptop = self.keys[0]
        self.client.post(f"{self.base}{laptop.pk}/change/", {"name": "Laptop"})
        self.assertEqual(self.entries("passkey.rename"), [])
