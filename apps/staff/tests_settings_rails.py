"""Rail accounts on the staff pages (Task 5)."""
import datetime
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.rails.models import LOCKED_MESSAGE, RailAccount, WatcherHealth
from apps.staff.testing import verified_staff_client
from apps.vault.testing import UnsealedVaultMixin

EVM_LOWER = "0x" + "ab" * 20
TRON = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"
SENTINEL = "SENTINEL-credential-value-123"


# A rail URL is resolved as it is saved (audit SEC-4): every host here answers one public address.
PUBLIC_ADDRINFO = [(2, 1, 6, "", ("93.184.216.34", 443))]

class RailBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Liberation s.r.o.")
        self.c = verified_staff_client(superuser=True)
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=PUBLIC_ADDRINFO))

    def new_url(self, kind):
        return f"{reverse('staff:rail_new')}?kind={kind}&org={self.org.pk}"

    def evm(self, **over):
        data = {"label": "EVM main", "active": "on", "address": EVM_LOWER, "networks": ["ethereum", "arbitrum"],
                "assets": ["USDC"]}
        data.update(over)
        return data

    def make_evm(self, **over):
        fields = dict(org=self.org, kind="evm", label="EVM", address=EVM_LOWER, networks=["arbitrum"], assets=["USDC"])
        fields.update(over)
        return RailAccount.objects.create(**fields)

    def quote_for(self, account):
        from apps.invoices.services import create_invoice, send_invoice
        from apps.contacts.services import find_or_create_contact
        from apps.quotes.models import Quote

        contact, _ = find_or_create_contact(self.org, "q@example.com", legal_name="Q")
        draft, _ = create_invoice(self.org, contact, currency="EUR", lines=[{"name": "x", "unit_price_minor": 100}])
        invoice, _v, _p = send_invoice(draft, email_client=False)
        now = timezone.now()
        return Quote.objects.create(
            invoice=invoice, rail_account=account, network="arbitrum", asset="USDC", address=account.address,
            amount_base_units="1", min_accept_base_units="1", ceiling_base_units="2", invoice_amount_minor=100,
            issued_at=now, expires_at=now + datetime.timedelta(minutes=30))


class RailAccountsTest(RailBase):
    def test_pages_render_and_settings_links(self):
        acct = self.make_evm()
        self.assertContains(self.c.get(reverse("staff:settings")), reverse("staff:rail_list"))
        self.assertContains(self.c.get(reverse("staff:rail_list")), "EVM")
        self.assertEqual(self.c.get(reverse("staff:rail_new")).status_code, 200)  # the chooser
        for kind in ("evm", "tron", "solana", "ton", "btcpay", "lightning"):
            self.assertEqual(self.c.get(self.new_url(kind)).status_code, 200, kind)
        self.assertEqual(self.c.get(reverse("staff:rail_edit", args=[acct.pk])).status_code, 200)

    def test_create_evm_normalises_address(self):
        lower = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        r = self.c.post(self.new_url("evm"), self.evm(address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))
        self.assertRedirects(r, reverse("staff:rail_list"), fetch_redirect_response=False)
        a = RailAccount.objects.get()
        self.assertEqual((a.org, a.kind, sorted(a.networks), a.assets), (self.org, "evm", ["arbitrum", "ethereum"], ["USDC"]))
        self.assertEqual(a.address, lower)  # a valid EIP-55 spelling is stored in the project's normal (lower-case) form
        entry = AuditEntry.objects.get(action="railaccount.create")
        self.assertNotIn(lower, str(entry.detail).lower())
        self.assertIn("address", entry.detail["fields"])

    def test_tron_networks_are_fixed(self):
        r = self.c.post(self.new_url("tron"), {"label": "T", "active": "on", "address": TRON, "assets": ["USDT"]})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        self.assertEqual(RailAccount.objects.get().networks, ["tron"])

    def test_btcpay_highspeed_refused(self):
        data = {"label": "B", "active": "on", "url": "https://pay.example.com", "store_id": "s1",
                "speed_policy": "HighSpeed", "assets": ["BTC"]}
        r = self.c.post(self.new_url("btcpay"), data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "HighSpeed")
        self.assertFalse(RailAccount.objects.exists())
        data["speed_policy"] = "MediumSpeed"
        self.assertEqual(self.c.post(self.new_url("btcpay"), data).status_code, 302)
        a = RailAccount.objects.get()
        self.assertEqual(a.config, {"url": "https://pay.example.com", "store_id": "s1", "speed_policy": "MediumSpeed"})
        self.assertEqual(a.address, "")

    def test_lightning_requires_backend(self):
        r = self.c.post(self.new_url("lightning"), {"label": "L", "active": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(RailAccount.objects.exists())
        r = self.c.post(self.new_url("lightning"), {"label": "L", "active": "on", "backend": "nwc"})
        self.assertEqual(r.status_code, 302)
        a = RailAccount.objects.get()
        self.assertEqual((a.assets, a.config), (["BTC"], {"backend": "nwc"}))

    @override_settings(TON_ENABLED=False)
    def test_ton_only_inactive_while_off(self):
        from apps.rails.models import TON_DISABLED

        addr = "UQ" + "A" * 46
        data = {"label": "TON", "address": addr, "assets": ["USDT"], "active": "on"}
        r = self.c.post(self.new_url("ton"), data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "TON is off")
        self.assertFalse(RailAccount.objects.exists())

    def test_deactivate_with_quote_keeps_watching(self):
        a = self.make_evm()
        self.quote_for(a)
        r = self.c.post(reverse("staff:rail_toggle", args=[a.pk]))
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db()
        self.assertFalse(a.active)
        self.assertIn(a.pk, list(RailAccount.objects.watched().values_list("pk", flat=True)))
        entry = AuditEntry.objects.get(action="railaccount.update")
        self.assertEqual(entry.detail["fields"], ["active"])
        self.assertContains(self.c.get(reverse("staff:rail_edit", args=[a.pk])),
                            "Quotes a client may still pay keep being watched until they are released.")
        self.assertEqual(self.c.get(reverse("staff:rail_toggle", args=[a.pk])).status_code, 405)

    def test_address_locked_while_quoted(self):
        a = self.make_evm()
        self.quote_for(a)
        other = "0x" + "cd" * 20
        r = self.c.post(reverse("staff:rail_edit", args=[a.pk]),
                        self.evm(address=other, networks=["arbitrum"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, LOCKED_MESSAGE.format(field="address")[:60])
        a.refresh_from_db()
        self.assertEqual(a.address.lower(), EVM_LOWER)
        self.assertFalse(AuditEntry.objects.filter(action="railaccount.update").exists())

    def test_edit_label_audits_names_only(self):
        a = self.make_evm()
        r = self.c.post(reverse("staff:rail_edit", args=[a.pk]), self.evm(label="Renamed", address=a.address,
                                                                           networks=["arbitrum"]))
        self.assertEqual(r.status_code, 302)
        entry = AuditEntry.objects.get(action="railaccount.update")
        self.assertEqual(entry.detail["fields"], ["label"])
        a.refresh_from_db()
        self.assertEqual(a.label, "Renamed")

    def test_edit_that_changes_nothing_is_not_audited(self):
        a = self.make_evm()
        r = self.c.post(reverse("staff:rail_edit", args=[a.pk]), self.evm(label="EVM", address=a.address,
                                                                           networks=["arbitrum"]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(AuditEntry.objects.exists())

    def test_btcpay_edit_that_changes_nothing_is_not_audited(self):
        a = RailAccount.objects.create(org=self.org, kind="btcpay", label="B", assets=["BTC"],
                                       config={"url": "https://p.example.com", "store_id": "s"})
        r = self.c.post(reverse("staff:rail_edit", args=[a.pk]),
                        {"label": "B", "active": "on", "url": "https://p.example.com", "store_id": "s",
                         "speed_policy": "MediumSpeed", "assets": ["BTC"]})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(AuditEntry.objects.exists())

    def test_kind_and_org_read_only_on_edit(self):
        a = self.make_evm()
        other = Organization.objects.create(slug="us", legal_name="LLC")
        self.c.post(reverse("staff:rail_edit", args=[a.pk]),
                    {**self.evm(label="X", address=a.address, networks=["arbitrum"]), "org": other.pk, "kind": "tron"})
        a.refresh_from_db()
        self.assertEqual((a.org, a.kind), (self.org, "evm"))

    def test_list_never_shows_credentials_and_shows_health(self):
        a = self.make_evm(credentials={"api_key": SENTINEL})
        RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", active=False, assets=["BTC"],
                                   config={"url": "https://btcpay.example", "store_id": "S"},
                                   credentials={"api_key": SENTINEL})
        WatcherHealth.objects.create(name=f"scan:evm:arbitrum:{a.pk}", failing_since=timezone.now(),
                                     consecutive_failures=3)
        r = self.c.get(reverse("staff:rail_list"))
        self.assertNotContains(r, SENTINEL)
        self.assertContains(r, "failing")
        self.assertContains(r, "credentials set")
        self.assertNotContains(self.c.get(reverse("staff:rail_edit", args=[a.pk])), SENTINEL)

    def test_no_delete_url(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("staff:rail_delete", args=[1])

    def test_requires_staff(self):
        from django.test import Client

        self.assertNotEqual(Client().get(reverse("staff:rail_list")).status_code, 200)
