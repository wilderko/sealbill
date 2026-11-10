"""Rail credentials and BTCPay webhook registration on the staff pages (Task 6)."""
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.rails.gateways.lightning import nwc
from apps.rails.models import RailAccount
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin

NWC = "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32
INVOICES_ONLY = {"permissions": ["btcpay.store.canviewinvoices:S1", "btcpay.store.cancreateinvoice:S1"]}
EVM = "0x" + "ab" * 20


# A rail URL is resolved as it is saved (audit SEC-4): every host here answers one public address.
PUBLIC_ADDRINFO = [(2, 1, 6, "", ("93.184.216.34", 443))]

class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.c = verified_staff_client(superuser=True)
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=PUBLIC_ADDRINFO))
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="B", assets=["BTC"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})
        self.ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})

    def url(self, acct):
        return reverse("staff:rail_credentials", args=[acct.pk])


class CredentialsPageTests(Base):
    def test_inputs_are_password_and_never_prefilled(self):
        self.btcpay.credentials = {"api_key": "SENTINEL-KEY-9"}
        self.btcpay.save(update_fields=["credentials"])
        r = self.c.get(self.url(self.btcpay))
        self.assertContains(r, 'type="password"', count=2)
        self.assertContains(r, 'autocomplete="new-password"', count=2)
        self.assertNotContains(r, "SENTINEL-KEY-9")
        self.assertContains(r, "Set: yes")
        self.assertContains(r, "Set: no")

    def test_lnbits_checkbox_only_for_lnbits(self):
        self.assertNotContains(self.c.get(self.url(self.ln)), "lnbits_vouched")
        lb = RailAccount.objects.create(org=self.org, kind="lightning", label="L", assets=["BTC"],
                                        config={"backend": "lnbits", "url": "https://l.example"})
        self.assertContains(self.c.get(self.url(lb)), "lnbits_vouched")

    def test_chain_account_has_no_credentials_page(self):
        evm = RailAccount.objects.create(org=self.org, kind="evm", label="E", address=EVM, networks=["arbitrum"],
                                         assets=["USDC"])
        self.assertEqual(self.c.get(self.url(evm)).status_code, 404)

    def test_save_stores_and_audits_names_only(self):
        with mock.patch.object(nwc, "capabilities", return_value={"make_invoice", "lookup_invoice"}):
            r = self.c.post(self.url(self.ln), {"nwc_url": NWC})
        self.assertEqual(r.status_code, 302)
        self.ln.refresh_from_db()
        self.assertEqual(self.ln.credentials, {"nwc_url": NWC})
        e = AuditEntry.objects.get(action="rail_account.credentials")
        self.assertEqual(e.actor, self.c.user)
        self.assertEqual(e.detail["fields"], ["nwc_url"])
        self.assertNotIn("cd" * 32, str(e.detail))

    def test_refused_rerenders_with_message_and_stores_nothing(self):
        with mock.patch.object(nwc, "capabilities", return_value={"make_invoice", "lookup_invoice", "pay_invoice"}):
            r = self.c.post(self.url(self.ln), {"nwc_url": NWC})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "pay_invoice")
        self.assertNotContains(r, "cd" * 32)
        self.ln.refresh_from_db()
        self.assertIsNone(self.ln.credentials)
        self.assertFalse(AuditEntry.objects.filter(action="rail_account.credentials").exists())

    def test_empty_post_keeps_stored(self):
        self.ln.credentials = {"nwc_url": NWC}
        self.ln.save(update_fields=["credentials"])
        r = self.c.post(self.url(self.ln), {"nwc_url": ""})
        self.assertEqual(r.status_code, 302)
        self.ln.refresh_from_db()
        self.assertEqual(self.ln.credentials, {"nwc_url": NWC})
        self.assertFalse(AuditEntry.objects.filter(action="rail_account.credentials").exists())

    def test_sealed_vault_goes_to_unlock(self):
        from apps.vault.vault import vault
        with mock.patch.object(vault, "is_unsealed", return_value=False):
            r = self.c.get(self.url(self.ln))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/unlock/", r["Location"])

    def test_anonymous_is_sent_away(self):
        from django.test import Client
        self.assertEqual(Client().get(self.url(self.ln)).status_code, 302)

    def test_link_from_rail_edit(self):
        self.assertContains(self.c.get(reverse("staff:rail_edit", args=[self.btcpay.pk])), self.url(self.btcpay))


@override_settings(SITE_URL="https://pay.example")
class WebhookButtonTests(Base):
    def hook_url(self):
        return reverse("staff:rail_btcpay_webhook", args=[self.btcpay.pk])

    def test_button_only_with_api_key(self):
        self.assertNotContains(self.c.get(self.url(self.btcpay)), self.hook_url())
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        self.assertContains(self.c.get(self.url(self.btcpay)), self.hook_url())
        self.assertNotContains(self.c.get(self.url(self.ln)), "btcpay-webhook")

    def test_post_registers(self):
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH1"}) as post:
            r = self.c.post(self.hook_url())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(post.call_count, 1)
        self.btcpay.refresh_from_db()
        self.assertIn("webhook_secret", self.btcpay.credentials)

    def test_get_not_allowed(self):
        self.assertEqual(self.c.get(self.hook_url()).status_code, 405)

    def test_refusal_shown_without_post(self):
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        listing = [{"id": "W", "url": f"https://pay.example/hooks/btcpay/{self.btcpay.pk}/"}]
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, listing]), \
                mock.patch("apps.rails.http.post_json") as post:
            r = self.c.post(self.hook_url(), follow=True)
        post.assert_not_called()
        self.assertContains(r, "delete")


class Task5CarryOverTests(Base):
    def test_same_address_other_case_is_no_change(self):
        a = RailAccount.objects.create(org=self.org, kind="evm", label="E", address=EVM, networks=["arbitrum"],
                                       assets=["USDC"])
        r = self.c.post(reverse("staff:rail_edit", args=[a.pk]), {
            "label": "E", "active": "on", "address": "0x" + "AB" * 20, "networks": ["arbitrum"], "assets": ["USDC"]})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(AuditEntry.objects.filter(action="railaccount.update").exists())

    def test_toggle_error_restores_active(self):
        from django.core.exceptions import ValidationError
        a = RailAccount.objects.create(org=self.org, kind="evm", label="E", address=EVM, networks=["arbitrum"],
                                       assets=["USDC"], active=True)

        def boom(self_, *args, **kw):
            raise ValidationError("no")
        with mock.patch.object(RailAccount, "save", boom):
            with mock.patch("apps.staff.views_rails.get_object_or_404", return_value=a):
                self.c.post(reverse("staff:rail_toggle", args=[a.pk]))
        self.assertTrue(a.active)


class FormRefusalTests(Base):
    def test_broad_btcpay_key_refused_in_form(self):
        broad = {"permissions": ["btcpay.store.canviewinvoices:S1", "btcpay.store.canmodifystoresettings:S1"]}
        with mock.patch("apps.rails.http.get_json", return_value=broad):
            r = self.c.post(self.url(self.btcpay), {"api_key": "KEY-X", "webhook_secret": "WS"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "KEY-X")
        self.btcpay.refresh_from_db()
        self.assertIsNone(self.btcpay.credentials)

    def test_lnd_macaroon_beyond_invoices_refused_in_form(self):
        from apps.rails.tests_audit_gateways import macaroon
        lnd = RailAccount.objects.create(org=self.org, kind="lightning", label="L2", assets=["BTC"],
                                         config={"backend": "lnd", "url": "https://lnd.example"})
        r = self.c.post(self.url(lnd), {"macaroon": macaroon([("offchain", ["write"])])})
        self.assertEqual(r.status_code, 200)
        lnd.refresh_from_db()
        self.assertIsNone(lnd.credentials)

    def test_lnbits_and_btcpay_messages_use_ui_wording(self):
        lb = RailAccount.objects.create(org=self.org, kind="lightning", label="L3", assets=["BTC"],
                                        config={"backend": "lnbits", "url": "https://l.example"})
        r = self.c.post(self.url(lb), {"invoice_key": "IK"})
        self.assertContains(r, "tick")
        self.assertNotContains(r, "--lnbits-key-is-invoice-key")
        bare = RailAccount.objects.create(org=self.org, kind="btcpay", label="B2", assets=["BTC"], config={})
        r = self.c.post(self.url(bare), {"api_key": "K", "webhook_secret": "W"})
        self.assertContains(r, "BTCPay URL and store id")
        self.assertNotContains(r, "config.url")

    def test_slow_wallet_gets_the_budgeted_message(self):
        from apps.rails import http
        seen = {}

        def slow(url):
            seen["budget"] = http.capped(30)
            raise http.Unavailable("lightning", "timeout")
        with mock.patch.object(nwc, "capabilities", side_effect=slow):
            r = self.c.post(self.url(self.ln), {"nwc_url": NWC})
        self.assertEqual(seen["budget"], 15)
        self.assertContains(r, "unlock Alby Hub and try again")

    def test_error_messages_render_as_errors(self):
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        with mock.patch("apps.rails.http.get_json", side_effect=http_unavailable()):
            r = self.c.post(reverse("staff:rail_btcpay_webhook", args=[self.btcpay.pk]), follow=True)
        self.assertContains(r, '<div class="error" role="alert">')


def http_unavailable():
    from apps.rails import http
    return http.Unavailable("btcpay", "timeout")


class UnauthenticatedTests(Base):
    def test_unverified_and_anonymous_change_nothing(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        user = get_user_model().objects.create_user("half", password="pw-half-1", is_staff=True)
        half = Client()
        half.force_login(user)  # password only, no second factor
        self.btcpay.credentials = {"api_key": "K"}
        self.btcpay.save(update_fields=["credentials"])
        for client in (Client(), half):
            for name, data in (("staff:rail_credentials", {"api_key": "NEW", "webhook_secret": "W"}),
                               ("staff:rail_btcpay_webhook", {})):
                with mock.patch("apps.rails.http.post_json") as post, mock.patch("apps.rails.http.get_json") as get:
                    r = client.post(reverse(name, args=[self.btcpay.pk]), data)
                self.assertEqual(r.status_code, 302)
                post.assert_not_called()
                get.assert_not_called()
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials, {"api_key": "K"})
        self.assertFalse(AuditEntry.objects.filter(action__startswith="rail_account").exists())


class EditKeepsCredentialsTests(Base):
    def test_edit_does_not_revert_credentials_saved_meanwhile(self):
        self.btcpay.credentials = {"api_key": "OLD"}
        self.btcpay.save(update_fields=["credentials"])
        url = reverse("staff:rail_edit", args=[self.btcpay.pk])
        self.c.get(url)
        # a concurrent credential save commits after the form was loaded
        fresh = RailAccount.objects.get(pk=self.btcpay.pk)
        fresh.credentials = {"api_key": "OLD", "webhook_secret": "NEW-SECRET"}
        fresh.save(update_fields=["credentials"])

        # the view's instance was read before the concurrent save: hand it a stale copy
        stale = RailAccount.objects.get(pk=self.btcpay.pk)
        stale.credentials = {"api_key": "OLD"}
        with mock.patch("apps.staff.views_rails.get_object_or_404", return_value=stale):
            r = self.c.post(url, {"label": "Renamed", "active": "on", "url": "https://btcpay.example",
                                  "store_id": "S1", "speed_policy": "MediumSpeed", "assets": ["BTC"]})
        self.assertEqual(r.status_code, 302)
        row = RailAccount.objects.get(pk=self.btcpay.pk)
        self.assertEqual(row.label, "Renamed")
        self.assertEqual(row.credentials, {"api_key": "OLD", "webhook_secret": "NEW-SECRET"})
