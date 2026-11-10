"""The step-up for staff powers (audit SEC-3): a superuser with a second factor from the last ten
minutes, or nothing changes; and every such change e-mails every active superuser."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.api.models import ApiToken
from apps.contacts.models import ContactEmail
from apps.contacts.services import find_or_create_contact
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import BankAccount, FiatLink, Organization
from apps.rails.gateways.lightning import nwc
from apps.webhooks.models import WebhookEndpoint
from apps.rails.models import RailAccount
from apps.staff import passkeys, stepup
from apps.staff.models import StaffPasskey, StaffTOTPDevice
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin

EVM = "0x" + "ab" * 20
NWC = "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32
SITE = "https://pay.example"


def _stale(browser):
    s = browser.session
    s[passkeys.SESSION_VERIFIED_AT] = int(passkeys._now()) - passkeys.RECENT_VERIFICATION_S - 1
    s.save()


def org_fields(**over):
    data = {"slug": "eu", "legal_name": "Seller s.r.o.", "address": "", "tax_ids": "", "email_from": "",
            "footer_note": "", "default_language": "en", "number_format": "{year}-{seq:04d}",
            "underpay_allowance_bps": "50", "underpay_allowance_usd": "1.00", "overpay_ceiling_bps": "1000",
            "accounting_currency": "EUR", "return_url_hosts": ""}
    data.update(over)
    return data


BANK = {"beneficiary_name": "Seller s.r.o.", "beneficiary_address": "", "account_number": "SK12 3456 7890",
        "bank_name": "Bank", "bank_address": "", "bank_swift": "ABCDSKBX", "intermediary_bank_name": "",
        "intermediary_swift": "", "intermediary_account": "", "currency": "EUR", "label": "Main"}
FIAT = {"kind": "revolut", "label": "Rev", "url": "https://revolut.me/seller", "currencies": ["EUR"]}


@override_settings(SITE_URL=SITE, STAFF_NOTIFY_EMAILS=[])
class StepUpBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]  # an NWC credential's relays resolve to public addresses
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=public))
        User = get_user_model()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.boss = verified_staff_client(superuser=True, username="boss")
        User.objects.filter(pk=self.boss.user.pk).update(email="boss@seller.example")
        User.objects.create_user("root2", email="root2@seller.example", password="pw-x-1-long", is_staff=True,
                                 is_superuser=True)
        User.objects.create_user("gone", email="gone@seller.example", password="pw-x-1-long", is_staff=True,
                                 is_superuser=True, is_active=False)
        User.objects.create_user("noaddr", password="pw-x-1-long", is_staff=True, is_superuser=True)
        self.clerk = verified_staff_client(username="clerk")
        User.objects.filter(pk=self.clerk.user.pk).update(email="clerk@seller.example")
        self.token = ApiToken.issue(self.org, "liberation", ["read"])[0]
        self.rail = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address=EVM,
                                               networks=["arbitrum"], assets=["USDC"])
        self.ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="B", assets=["BTC"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"},
                                                 credentials={"api_key": "SECRET-API-KEY-1"})
        self.bank = BankAccount.objects.create(org=self.org, **{**BANK, "label": "Old"})
        self.fiat = FiatLink.objects.create(org=self.org, kind="revolut", label="R", url="https://revolut.me/a",
                                            currencies=["EUR"])
        self.contact, _ = find_or_create_contact(self.org, "first@client.example", legal_name="Client")
        self.second = ContactEmail.objects.create(contact=self.contact, address="second@client.example", primary=False)
        self.hook = WebhookEndpoint.objects.create(org=self.org, url="https://hooks.example.com/in/HOOK-PATH-SECRET",
                                                   secret="HOOK-SIGNING-SECRET", events=["*"])
        self.colleague = User.objects.create_user("colleague", password="pw-x-1-long", is_staff=True)
        self.colleague_key = StaffPasskey.objects.create(user=self.colleague, name="yubikey", confirmed=True,
                                                         credential_id="cred-1", public_key=b"PK")

        def register(acct, actor):  # the BTCPay round trip, answered here
            acct.credentials = {**(acct.credentials or {}), "webhook_secret": "WHSEC-SECRET-9"}
            acct.save(update_fields=["credentials"])

        self.enterContext(mock.patch.object(nwc, "capabilities", return_value={"make_invoice", "lookup_invoice"}))
        self.enterContext(mock.patch("apps.rails.credentials.register_btcpay_webhook", side_effect=register))

    # Each action: (name, url, data, did it change?, the page a stale superuser comes back to).
    def actions(self):
        org_url = reverse("staff:org_edit", args=[self.org.pk])
        rail_edit = reverse("staff:rail_edit", args=[self.rail.pk])
        ln_creds = reverse("staff:rail_credentials", args=[self.ln.pk])
        btc_creds = reverse("staff:rail_credentials", args=[self.btcpay.pk])
        contact_url = reverse("staff:contact_detail", args=[self.contact.pk])
        bank_edit = reverse("staff:bank_edit", args=[self.bank.pk])
        fiat_edit = reverse("staff:fiat_edit", args=[self.fiat.pk])
        bank_new = reverse("staff:bank_new", args=[self.org.pk])
        fiat_new = reverse("staff:fiat_new", args=[self.org.pk])
        rail_new = f"{reverse('staff:rail_new')}?kind=evm&org={self.org.pk}"
        tokens = reverse("staff:token_list")

        def fresh(model, pk):
            return model.objects.get(pk=pk)

        return [
            ("token create", reverse("staff:token_new"), {"org": self.org.pk, "name": "agent", "scopes": ["admin"]},
             lambda: ApiToken.objects.filter(name="agent").exists(), tokens),
            ("token rescope", reverse("staff:token_scopes", args=[self.token.pk]),
             {"scopes": ["read", "payments:confirm"]},
             lambda: set(fresh(ApiToken, self.token.pk).scopes) != {"read"}, tokens),
            ("token revoke", reverse("staff:token_revoke", args=[self.token.pk]), {},
             lambda: fresh(ApiToken, self.token.pk).revoked_at is not None, tokens),
            ("rail create", rail_new, {"label": "New EVM", "active": "on", "address": EVM, "networks": ["ethereum"],
                                       "assets": ["USDC"]},
             lambda: RailAccount.objects.filter(label="New EVM").exists(), rail_new),
            ("rail edit", rail_edit, {"label": "Renamed", "active": "on", "address": EVM, "networks": ["arbitrum"],
                                      "assets": ["USDC"]},
             lambda: fresh(RailAccount, self.rail.pk).label == "Renamed", rail_edit),
            ("rail toggle", reverse("staff:rail_toggle", args=[self.rail.pk]), {},
             lambda: not fresh(RailAccount, self.rail.pk).active, rail_edit),
            ("rail credentials", ln_creds, {"nwc_url": NWC},
             lambda: bool(fresh(RailAccount, self.ln.pk).credentials), ln_creds),
            ("btcpay webhook", reverse("staff:rail_btcpay_webhook", args=[self.btcpay.pk]), {},
             lambda: "webhook_secret" in (fresh(RailAccount, self.btcpay.pk).credentials or {}), btc_creds),
            ("org US flag", org_url, org_fields(is_us_resident_issuer="on"),
             lambda: fresh(Organization, self.org.pk).is_us_resident_issuer, org_url),
            ("org return hosts", org_url, org_fields(return_url_hosts="evil.example"),
             lambda: fresh(Organization, self.org.pk).settings.get("return_url_hosts") == ["evil.example"], org_url),
            ("bank create", bank_new, BANK, lambda: BankAccount.objects.filter(label="Main").exists(), bank_new),
            ("bank edit", bank_edit, {**BANK, "label": "Changed"},
             lambda: fresh(BankAccount, self.bank.pk).label == "Changed", bank_edit),
            ("bank toggle", reverse("staff:bank_toggle", args=[self.bank.pk]), {},
             lambda: not fresh(BankAccount, self.bank.pk).active, org_url),
            ("fiat create", fiat_new, FIAT, lambda: FiatLink.objects.filter(label="Rev").exists(), fiat_new),
            ("fiat edit", fiat_edit, {**FIAT, "label": "Changed"},
             lambda: fresh(FiatLink, self.fiat.pk).label == "Changed", fiat_edit),
            ("fiat toggle", reverse("staff:fiat_toggle", args=[self.fiat.pk]), {},
             lambda: not fresh(FiatLink, self.fiat.pk).active, org_url),
            ("contact e-mail add", reverse("staff:contact_email_add", args=[self.contact.pk]),
             {"address": "attacker@evil.example"}, lambda: ContactEmail.objects.filter(contact=self.contact).count() == 3,
             contact_url),
            ("contact e-mail primary",
             reverse("staff:contact_email_primary", args=[self.contact.pk, self.second.pk]), {},
             lambda: fresh(ContactEmail, self.second.pk).primary, contact_url),
            # Red-team A6: an endpoint is a lasting outbound channel, as a token is lasting access.
            ("webhook create", reverse("staff:webhook_new"),
             {"org": self.org.pk, "url": "https://hooks.example.com/in/NEW-PATH-SECRET"},
             lambda: WebhookEndpoint.objects.exclude(pk=self.hook.pk).exists(), reverse("staff:webhook_list")),
            ("webhook delete", reverse("staff:webhook_delete", args=[self.hook.pk]), {},
             lambda: not WebhookEndpoint.objects.filter(pk=self.hook.pk).exists(),
             reverse("staff:webhook_detail", args=[self.hook.pk])),
            ("colleague's factor", reverse("staff:team_factor_remove"), {"kind": "passkey", "pk": self.colleague_key.pk},
             lambda: not StaffPasskey.objects.filter(pk=self.colleague_key.pk).exists(), reverse("staff:team")),
        ]

    def run_action(self, client, url, data):
        return client.post(url, data)

    def alerts(self):
        return OutboxMessage.objects.filter(kind=stepup.KIND)


class NotASuperuserTests(StepUpBase):
    def test_every_action_is_refused_with_403_and_changes_nothing(self):
        for name, url, data, changed, _back in self.actions():
            with self.subTest(name):
                r = self.run_action(self.clerk, url, data)
                self.assertEqual(r.status_code, 403, name)
                self.assertContains(r, "Only a superuser", status_code=403)
                self.assertFalse(changed(), name)
        self.assertFalse(self.alerts().exists())

    def test_other_organization_fields_stay_open_to_staff(self):
        r = self.clerk.post(reverse("staff:org_edit", args=[self.org.pk]), org_fields(legal_name="Seller a.s."))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Organization.objects.get(pk=self.org.pk).legal_name, "Seller a.s.")
        self.assertFalse(self.alerts().exists())

    def test_own_factor_needs_no_superuser(self):
        StaffPasskey.objects.create(user=self.clerk.user, name="spare", confirmed=True, credential_id="c-2",
                                    public_key=b"PK")
        own = StaffTOTPDevice.objects.get(user=self.clerk.user)
        r = self.clerk.post(reverse("staff:team_factor_remove"), {"kind": "totp", "pk": own.pk})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(StaffTOTPDevice.objects.filter(pk=own.pk).exists())
        self.assertFalse(self.alerts().exists())


class StaleSuperuserTests(StepUpBase):
    def test_every_action_sends_to_verify_and_changes_nothing(self):
        for name, url, data, changed, back in self.actions():
            with self.subTest(name):
                _stale(self.boss)
                r = self.run_action(self.boss, url, data)
                self.assertEqual(r.status_code, 302, name)
                self.assertEqual(r["Location"], stepup._reverify_url(back), name)
                self.assertFalse(changed(), name)
        self.assertFalse(self.alerts().exists())

    def test_the_verify_page_says_why_and_comes_back(self):
        _stale(self.boss)
        r = self.boss.post(reverse("staff:token_revoke", args=[self.token.pk]), follow=True)
        self.assertContains(r, "Confirm your second factor first")
        self.assertContains(r, "Nothing was changed")
        self.assertEqual(r.context["next"], reverse("staff:token_list"))

    def test_pages_offer_the_verify_path(self):
        _stale(self.boss)
        for url in (reverse("staff:token_list"), reverse("staff:rail_edit", args=[self.rail.pk]),
                    reverse("staff:rail_credentials", args=[self.ln.pk]),
                    reverse("staff:org_edit", args=[self.org.pk]), reverse("staff:bank_edit", args=[self.bank.pk]),
                    reverse("staff:fiat_new", args=[self.org.pk]),
                    reverse("staff:contact_detail", args=[self.contact.pk]), reverse("staff:team")):
            with self.subTest(url):
                r = self.boss.get(url)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, "Verify again")
                self.assertContains(r, stepup._reverify_url(url).replace("&", "&amp;"))

    def test_pages_tell_a_clerk_who_may(self):
        r = self.clerk.get(reverse("staff:token_list"))
        self.assertContains(r, "Only a superuser")


class FreshSuperuserTests(StepUpBase):
    def test_every_action_works_and_queues_one_alert_to_every_active_superuser(self):
        for name, url, data, changed, _back in self.actions():
            with self.subTest(name):
                before = self.alerts().count()
                r = self.run_action(self.boss, url, data)
                self.assertIn(r.status_code, (200, 302), name)
                self.assertTrue(changed(), name)
                self.assertEqual(self.alerts().count(), before + 1, name)
                msg = self.alerts().order_by("-pk").first()
                self.assertEqual(sorted(msg.to), ["boss@seller.example", "root2@seller.example"])
                self.assertEqual(msg.language, "en")
                self.assertEqual(msg.body_context["who"], "boss")
                self.assertTrue(msg.body_context["when"].endswith(" UTC"))
                self.assertTrue(msg.body_context["admin_url"].startswith(SITE + "/"))

    def test_alerts_carry_no_secret_values(self):
        for name, url, data, _changed, _back in self.actions():
            self.run_action(self.boss, url, data)
        text = " ".join(str(m.body_context) + str(m.subject_context) for m in self.alerts())
        for secret in ("SECRET-API-KEY-1", "WHSEC-SECRET-9", NWC, "cd" * 32, "SK12 3456", "attacker@evil.example",
                       "HOOK-PATH-SECRET", "NEW-PATH-SECRET", "HOOK-SIGNING-SECRET",
                       "second@client.example", "first@client.example"):
            self.assertNotIn(secret, text)
        self.assertNotIn(ApiToken.objects.get(name="agent").token_hash, text)

    def test_alert_is_sent_by_the_outbox(self):
        self.boss.post(reverse("staff:token_revoke", args=[self.token.pk]))
        self.assertEqual(notify.flush_outbox(), 1)
        sent = mail.outbox[-1]
        self.assertEqual(sorted(sent.to), ["boss@seller.example", "root2@seller.example"])
        self.assertEqual(sent.subject, "Staff change: API token revoked")
        self.assertIn("boss", sent.body)
        self.assertIn("liberation", sent.body)
        self.assertIn(f"#{self.token.pk}", sent.body)
        self.assertIn(" UTC", sent.body)

    def test_org_edit_without_the_two_fields_is_not_announced(self):
        self.boss.post(reverse("staff:org_edit", args=[self.org.pk]), org_fields(legal_name="Seller a.s."))
        self.assertFalse(self.alerts().exists())

    def test_nothing_changed_is_not_announced(self):
        self.boss.post(reverse("staff:token_scopes", args=[self.token.pk]), {"scopes": ["read"]})
        self.boss.post(reverse("staff:bank_edit", args=[self.bank.pk]), {**BANK, "label": "Old"})
        self.boss.post(reverse("staff:contact_email_primary", args=[self.contact.pk,
                                                                     self.contact.emails.get(primary=True).pk]))
        self.assertFalse(self.alerts().exists())

    @override_settings(STAFF_NOTIFY_EMAILS=["ops@seller.example"])
    def test_with_no_superuser_address_the_staff_alert_list_is_told(self):
        get_user_model().objects.filter(is_superuser=True).update(email="")
        self.boss.post(reverse("staff:token_revoke", args=[self.token.pk]))
        self.assertEqual(self.alerts().get().to, ["ops@seller.example"])

    def test_with_nobody_to_tell_the_change_still_happens_and_is_logged(self):
        get_user_model().objects.filter(is_superuser=True).update(email="")
        with self.assertLogs("apps.staff.stepup", "WARNING"):
            self.boss.post(reverse("staff:token_revoke", args=[self.token.pk]))
        self.assertIsNotNone(ApiToken.objects.get(pk=self.token.pk).revoked_at)
        self.assertFalse(self.alerts().exists())
