"""The hidden Django admin keeps SEC-3's step-up (review S5, I-1): a change there needs a second factor
from the last ten minutes, and every change to a step-up model is told to every superuser."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api.models import ApiToken
from apps.contacts.models import ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.notify.models import OutboxMessage
from apps.orgs.models import BankAccount, Organization
from apps.rails.models import RailAccount
from apps.staff import passkeys, stepup
from apps.staff.models import StaffPasskey, StaffTOTPDevice
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin

ADMIN = "/hidden-admin-xyz"
EVM = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "ee" * 20
BANK = {"beneficiary_name": "Seller", "currency": "EUR", "label": "EUR account", "active": "on",
        "instructions": "null", "beneficiary_address": "", "bank_name": "", "bank_address": "", "bank_swift": "",
        "intermediary_bank_name": "", "intermediary_swift": "", "intermediary_account": "",
        "account_number": "SK1111111111111111111111"}


def _stale(browser):
    s = browser.session
    s[passkeys.SESSION_VERIFIED_AT] = int(passkeys._now()) - passkeys.RECENT_VERIFICATION_S - 1
    s.save()


@override_settings(SITE_URL="https://pay.example", STAFF_NOTIFY_EMAILS=[])
class AdminStepUpTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.root = verified_staff_client(superuser=True, username="root")
        User.objects.filter(pk=self.root.user.pk).update(email="root@seller.example")
        User.objects.create_user("root2", email="root2@seller.example", password="pw-x-1-long", is_staff=True,
                                 is_superuser=True)
        self.token = ApiToken.issue(self.org, "liberation", ["read"])[0]
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                               networks=["arbitrum"], assets=["USDC"])
        self.colleague = User.objects.create_user("colleague", password="pw-x-1-long", is_staff=True)
        self.key = StaffPasskey.objects.create(user=self.colleague, name="yubikey", confirmed=True,
                                               credential_id="cred-1", public_key=b"PK")
        self.bank = BankAccount.objects.create(org=self.org, **{k: v for k, v in BANK.items()
                                                                if k not in ("active", "instructions")})

    def alerts(self):
        return OutboxMessage.objects.filter(kind=stepup.KIND)

    def rescope(self):
        return self.root.post(f"{ADMIN}/api/apitoken/{self.token.pk}/change/", {
            "scopes": '["read", "payments:confirm", "admin"]', "revoked_at_0": "", "revoked_at_1": ""})

    def move_wallet(self):
        return self.root.post(f"{ADMIN}/rails/railaccount/{self.rail.pk}/change/", {
            "org": self.org.pk, "kind": "evm", "label": "EVM", "address": OTHER_WALLET,
            "networks": json.dumps(["arbitrum"]), "assets": json.dumps(["USDC"]), "config": "{}", "active": "on",
            "_save": "Save"})

    def delete_colleagues_passkey(self):
        return self.root.post(f"{ADMIN}/staff/staffpasskey/{self.key.pk}/delete/", {"post": "yes"})

    # --- the reviewer's three PoC cases, stale and fresh

    def test_a_stale_session_cannot_rescope_a_token(self):
        _stale(self.root)
        r = self.rescope()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], stepup._reverify_url(f"{ADMIN}/api/apitoken/{self.token.pk}/change/"))
        self.assertEqual(ApiToken.objects.get(pk=self.token.pk).scopes, ["read"])
        self.assertFalse(self.alerts().exists())

    def test_a_stale_session_cannot_move_a_wallet(self):
        _stale(self.root)
        r = self.move_wallet()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify/", r["Location"])
        self.assertEqual(RailAccount.objects.get(pk=self.rail.pk).address, EVM)
        self.assertFalse(self.alerts().exists())

    def test_a_stale_session_cannot_delete_a_colleagues_passkey(self):
        _stale(self.root)
        r = self.delete_colleagues_passkey()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/verify/", r["Location"])
        self.assertTrue(StaffPasskey.objects.filter(pk=self.key.pk).exists())
        self.assertFalse(self.alerts().exists())

    def test_a_fresh_session_rescopes_and_one_alert_is_queued(self):
        self.assertEqual(self.rescope().status_code, 302)
        self.assertIn("payments:confirm", ApiToken.objects.get(pk=self.token.pk).scopes)
        alert = self.alerts().get()
        self.assertEqual(sorted(alert.to), ["root2@seller.example", "root@seller.example"])
        self.assertIn("liberation", alert.body_context["what"])
        self.assertIn("payments:confirm", alert.body_context["what"])
        self.assertEqual(alert.body_context["who"], "root")

    def test_a_fresh_session_moves_a_wallet_and_one_alert_is_queued(self):
        self.assertEqual(self.move_wallet().status_code, 302)
        self.assertEqual(RailAccount.objects.get(pk=self.rail.pk).address, OTHER_WALLET)
        alert = self.alerts().get()
        self.assertIn("address", alert.body_context["what"])
        self.assertNotIn(OTHER_WALLET, str(alert.body_context))

    def test_a_fresh_session_deletes_a_colleagues_passkey_and_one_alert_is_queued(self):
        self.assertEqual(self.delete_colleagues_passkey().status_code, 302)
        self.assertFalse(StaffPasskey.objects.filter(pk=self.key.pk).exists())
        alert = self.alerts().get()
        self.assertIn("colleague", alert.body_context["what"])

    # --- the rest of the gated models

    def test_the_verify_page_says_why(self):
        _stale(self.root)
        r = self.root.post(f"{ADMIN}/api/apitoken/{self.token.pk}/change/", {"scopes": '["read"]'}, follow=True)
        self.assertContains(r, "Confirm your second factor first")

    def test_reading_and_signing_out_need_no_fresh_factor(self):
        _stale(self.root)
        self.assertEqual(self.root.get(f"{ADMIN}/api/apitoken/{self.token.pk}/change/").status_code, 200)
        self.assertEqual(self.root.get(f"{ADMIN}/").status_code, 200)
        r = self.root.post(f"{ADMIN}/logout/")
        self.assertIn(r.status_code, (200, 302))
        self.assertNotIn("/verify/", r.get("Location", ""))

    def test_a_bank_account_change_is_announced_without_its_number(self):
        r = self.root.post(f"{ADMIN}/orgs/bankaccount/{self.bank.pk}/change/",
                           {**BANK, "org": self.org.pk, "account_number": "GB00ATTACKER0000000000"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        alert = self.alerts().get()
        self.assertIn("account_number", alert.body_context["what"])
        self.assertNotIn("ATTACKER", str(alert.body_context))

    def test_the_organizations_us_flag_is_announced_and_its_other_fields_are_not(self):
        url = f"{ADMIN}/orgs/organization/{self.org.pk}/change/"
        form = {"slug": "eu", "legal_name": "Seller s.r.o.", "default_language": "en",
                "number_format": "{year}-{seq:04d}", "settings": json.dumps(self.org.settings),
                "initial-settings": json.dumps(self.org.settings),  # a callable default: the page posts it
                "address": "", "tax_ids": "", "email_from": "", "footer_note": ""}
        for prefix in ("bank_accounts", "fiat_links"):
            form.update({f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0",
                         f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"})
        initial = BankAccount.objects.filter(org=self.org).count()
        form.update({"bank_accounts-TOTAL_FORMS": str(initial), "bank_accounts-INITIAL_FORMS": str(initial)})
        for i, bank in enumerate(BankAccount.objects.filter(org=self.org).order_by("pk")):
            form.update({f"bank_accounts-{i}-id": str(bank.pk), f"bank_accounts-{i}-org": str(self.org.pk),
                         **{f"bank_accounts-{i}-{k}": v for k, v in BANK.items()}})
        r = self.root.post(url, {**form, "legal_name": "Seller a.s."})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertFalse(self.alerts().exists(), [a.body_context["what"] for a in self.alerts()])
        r = self.root.post(url, {**form, "is_us_resident_issuer": "on"})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertTrue(Organization.objects.get(pk=self.org.pk).is_us_resident_issuer)
        alert = self.alerts().get()
        self.assertIn("US-resident issuer", alert.body_context["what"])

    def test_a_contact_address_added_in_the_admin_is_announced(self):
        contact, _ = find_or_create_contact(self.org, "first@client.example", legal_name="Client")
        first = contact.emails.get()
        form = {"org": self.org.pk, "legal_name": "Client", "display_name": "", "address": "null", "tax_ids": "",
                "phone": "", "notes": "", "preferred_language": "", "us_tax_resident": contact.us_tax_resident,
                "emails-TOTAL_FORMS": "2", "emails-INITIAL_FORMS": "1", "emails-MIN_NUM_FORMS": "0",
                "emails-MAX_NUM_FORMS": "1000",
                "emails-0-id": str(first.pk), "emails-0-contact": str(contact.pk),
                "emails-0-address": "first@client.example", "emails-0-primary": "on",
                "emails-1-id": "", "emails-1-contact": str(contact.pk), "emails-1-address": "attacker@evil.example"}
        r = self.root.post(f"{ADMIN}/contacts/contact/{contact.pk}/change/", form)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertEqual(ContactEmail.objects.filter(contact=contact).count(), 2)
        alert = self.alerts().get()
        self.assertIn(f"contact #{contact.pk}", alert.body_context["what"])
        self.assertNotIn("attacker@evil.example", str(alert.body_context))

    def test_unconfirming_a_colleagues_authenticator_is_announced(self):
        device = StaffTOTPDevice.objects.create(user=self.colleague, name="phone", confirmed=True)
        r = self.root.post(f"{ADMIN}/staff/stafftotpdevice/{device.pk}/change/",
                           {"name": "phone", "throttling_failure_count": "0"})  # "confirmed" unticked
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertFalse(StaffTOTPDevice.objects.get(pk=device.pk).confirmed)
        self.assertIn("colleague", self.alerts().get().body_context["what"])

    def test_a_rename_of_ones_own_passkey_is_not_announced(self):
        own = StaffPasskey.objects.create(user=self.root.user, name="mine", confirmed=True, credential_id="c-2",
                                          public_key=b"PK")
        r = self.root.post(f"{ADMIN}/staff/staffpasskey/{own.pk}/change/", {"name": "renamed"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertFalse(self.alerts().exists())
