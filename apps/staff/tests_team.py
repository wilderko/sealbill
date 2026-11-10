"""The Team page and removing another person's second factor (spec 2026-11-05 §9 item 5)."""
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.staff import passkeys
from apps.staff.models import StaffPasskey, StaffTOTPDevice
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin

REMOVE = "staff:team_factor_remove"


def _passkey(user, name="phone"):
    return StaffPasskey.objects.create(user=user, name=name, confirmed=True, credential_id=f"cred-{name}-{user.pk}",
                                       public_key=b"PUBLIC-KEY-BYTES-XYZ")


def _stale(browser):
    s = browser.session
    s[passkeys.SESSION_VERIFIED_AT] = int(passkeys._now()) - passkeys.RECENT_VERIFICATION_S - 1
    s.save()


class TeamTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.me = verified_staff_client(superuser=True, username="boss")
        self.other = get_user_model().objects.create_user("colleague", password="pw-x-1-long", is_staff=True)
        self.totp = StaffTOTPDevice.objects.create(user=self.other, name="old phone", confirmed=True)
        self.key = _passkey(self.other, "yubikey")

    def post(self, kind, pk, client=None):
        return (client or self.me).post(reverse(REMOVE), {"kind": kind, "pk": pk})

    def test_page_lists_staff_without_secrets(self):
        r = self.me.get(reverse("staff:team"))
        body = r.content.decode()
        self.assertContains(r, "colleague")
        self.assertContains(r, "yubikey")
        self.assertContains(r, "old phone")
        self.assertNotIn(self.totp.key, body)
        self.assertNotIn("otpauth", body)
        self.assertNotIn("PUBLIC-KEY-BYTES", body)
        self.assertNotIn(self.key.credential_id, body)

    def test_linked_from_settings(self):
        self.assertContains(self.me.get(reverse("staff:settings")), reverse("staff:team"))

    def test_stale_session_is_sent_to_verify_and_nothing_changes(self):
        _stale(self.me)
        r = self.post("passkey", self.key.pk)
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("staff:verify"), r["Location"])
        self.assertTrue(StaffPasskey.objects.filter(pk=self.key.pk).exists())
        self.assertFalse(AuditEntry.objects.filter(action="passkey.remove").exists())

    def test_removes_and_audits_a_passkey(self):
        r = self.post("passkey", self.key.pk)
        self.assertRedirects(r, reverse("staff:team"), fetch_redirect_response=False)
        self.assertFalse(StaffPasskey.objects.filter(pk=self.key.pk).exists())
        e = AuditEntry.objects.get(action="passkey.remove")
        self.assertEqual(e.detail, {"name": "yubikey", "user": "colleague", "user_id": self.other.pk, "via": "staff"})
        self.assertEqual(e.actor, self.me.user)

    def test_removes_and_audits_a_totp_device(self):
        self.post("totp", self.totp.pk)
        self.assertFalse(StaffTOTPDevice.objects.filter(pk=self.totp.pk).exists())
        e = AuditEntry.objects.get(action="totp_device.remove")
        self.assertEqual(e.detail, {"name": "old phone", "user": "colleague", "user_id": self.other.pk, "via": "staff"})

    def test_another_persons_last_factor_may_go_and_the_page_says_what_next(self):
        self.post("passkey", self.key.pk)
        r = self.post("totp", self.totp.pk)
        r = self.me.get(r["Location"])
        flash = [m for m in r.context["messages"]]
        self.assertEqual(flash[-1].level_tag, "warning")
        self.assertIn("payinv-manage create_staff_totp colleague --replace", flash[-1].message)
        self.assertContains(r, "notice--warn")
        self.assertEqual(passkeys.second_factor_count(self.other), 0)

    def test_own_last_factor_is_refused(self):
        own = StaffTOTPDevice.objects.get(user=self.me.user)
        r = self.post("totp", own.pk)
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "last second factor", status_code=400)
        self.assertTrue(StaffTOTPDevice.objects.filter(pk=own.pk).exists())

    def test_own_factor_goes_when_another_remains(self):
        own = StaffTOTPDevice.objects.get(user=self.me.user)
        _passkey(self.me.user, "mine")
        self.assertEqual(self.post("totp", own.pk).status_code, 302)
        self.assertFalse(StaffTOTPDevice.objects.filter(pk=own.pk).exists())

    def test_unknown_kind_or_pk_is_404_and_non_staff_devices_are_untouchable(self):
        self.assertEqual(self.post("nope", 1).status_code, 404)
        self.assertEqual(self.post("totp", 999999).status_code, 404)
        outsider = get_user_model().objects.create_user("cust", password="pw-x-1-long", is_staff=False)
        d = StaffTOTPDevice.objects.create(user=outsider, name="x", confirmed=True)
        self.assertEqual(self.post("totp", d.pk).status_code, 404)

    def test_stale_redirect_returns_to_team(self):
        _stale(self.me)
        r = self.post("passkey", self.key.pk)
        self.assertIn("next=" + quote(reverse("staff:team"), safe=""), r["Location"])

    def test_unconfirmed_device_cannot_be_removed(self):
        d = _passkey(self.other, "pending")
        StaffPasskey.objects.filter(pk=d.pk).update(confirmed=False)
        self.assertEqual(self.post("passkey", d.pk).status_code, 404)
        self.assertTrue(StaffPasskey.objects.filter(pk=d.pk).exists())

    def test_non_numeric_pk_is_404(self):
        self.assertEqual(self.post("totp", "1e3").status_code, 404)
        self.assertEqual(self.post("totp", "-1").status_code, 404)

    def test_removed_device_ends_that_session(self):
        other = verified_staff_client(username="signed-in")
        _passkey(other.user, "spare")
        self.assertEqual(other.get(reverse("staff:team")).status_code, 200)
        dev = StaffTOTPDevice.objects.get(user=other.user)
        self.post("totp", dev.pk)
        r = other.get(reverse("staff:team"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("staff:verify"), r["Location"])

    def test_get_not_allowed_and_anonymous_refused(self):
        self.assertEqual(self.me.get(reverse(REMOVE)).status_code, 405)
        from django.test import Client
        r = Client().post(reverse(REMOVE), {"kind": "totp", "pk": self.totp.pk})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("staff:login"), r["Location"])
        self.assertTrue(StaffTOTPDevice.objects.filter(pk=self.totp.pk).exists())


class SuperuserFactorTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("root1", password="pw-x-1-long", is_staff=True, is_superuser=True)
        self.admin_key = _passkey(self.admin, "root-key")
        _passkey(self.admin, "root-key-2")

    def test_non_superuser_gets_403_nothing_removed_nothing_audited(self):
        clerk = verified_staff_client(username="plain")
        r = clerk.post(reverse(REMOVE), {"kind": "passkey", "pk": self.admin_key.pk})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(StaffPasskey.objects.filter(pk=self.admin_key.pk).exists())
        self.assertFalse(AuditEntry.objects.filter(action="passkey.remove").exists())

    def test_remove_button_hidden_for_non_superuser(self):
        clerk = verified_staff_client(username="plain")
        body = clerk.get(reverse("staff:team")).content.decode()
        self.assertIn("root1", body)
        self.assertNotIn('value="passkey"', body)  # no Remove form for either of root1's passkeys

    def test_superuser_may_remove_another_superuser_factor(self):
        boss = verified_staff_client(superuser=True, username="boss2")
        r = boss.post(reverse(REMOVE), {"kind": "passkey", "pk": self.admin_key.pk})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(StaffPasskey.objects.filter(pk=self.admin_key.pk).exists())
        self.assertTrue(AuditEntry.objects.filter(action="passkey.remove").exists())
        own = StaffTOTPDevice.objects.get(user=boss.user)
        self.assertEqual(boss.post(reverse(REMOVE), {"kind": "totp", "pk": own.pk}).status_code, 400)
