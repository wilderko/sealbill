"""`manage.py set_rail_credentials`: the one way the encrypted credentials are set (final review m1)."""
import io
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.rails.models import RailAccount
from apps.rails.tests_audit_gateways import BAKED_OPS, macaroon, rune
from apps.vault.testing import UnsealedVaultMixin
from apps.vault.vault import vault

NWC = "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32

RECEIVE_ONLY = {"make_invoice", "lookup_invoice", "get_info", "get_balance", "list_transactions"}
# What BTCPay answers for a key that only creates and reads the store's invoices (audit DATA-3).
BTCPAY_INVOICES_ONLY = {"permissions": ["btcpay.store.canviewinvoices:S1", "btcpay.store.cancreateinvoice:S1"]}


class SetRailCredentialsTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]  # an NWC credential's relays resolve to public addresses
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=public))
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})
        self.ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})
        # BTCPay is asked what a key may do (audit DATA-3): here it answers "invoices only".
        self.enterContext(mock.patch("apps.rails.http.get_json", return_value=BTCPAY_INVOICES_ONLY))

    def run_with(self, account, stdin, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            call_command("set_rail_credentials", str(account.pk), *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def creds(self, account):
        return RailAccount.objects.get(pk=account.pk).credentials

    def test_btcpay_secrets_are_read_from_stdin_saved_encrypted_and_audited(self):
        printed = self.run_with(self.btcpay, "KEY-123\nwhsec-456\n")
        self.assertEqual(self.creds(self.btcpay), {"api_key": "KEY-123", "webhook_secret": "whsec-456"})
        self.assertIn("api_key, webhook_secret", printed)
        self.assertNotIn("KEY-123", printed)
        self.assertNotIn("whsec-456", printed)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT credentials FROM {RailAccount._meta.db_table} WHERE id = %s", [self.btcpay.pk])
            (raw,) = cursor.fetchone()
        self.assertTrue(raw)
        self.assertNotIn("KEY-123", raw)  # encrypted at rest
        entry = AuditEntry.objects.get(action="rail_account.credentials", object_pk=str(self.btcpay.pk))
        self.assertEqual(entry.actor_username, "manage.py set_rail_credentials")
        self.assertEqual(entry.detail, {"rail_account": self.btcpay.pk, "kind": "btcpay",
                                        "fields": ["api_key", "webhook_secret"]})
        # An empty answer keeps what is stored: one secret can be replaced alone.
        self.run_with(self.btcpay, "\nwhsec-789\n")
        self.assertEqual(self.creds(self.btcpay), {"api_key": "KEY-123", "webhook_secret": "whsec-789"})

    def test_lightning_asks_for_its_backends_secret(self):
        with mock.patch("apps.rails.gateways.lightning.nwc.capabilities", return_value=RECEIVE_ONLY):
            self.run_with(self.ln, NWC + "\n")
        self.assertEqual(self.creds(self.ln), {"nwc_url": NWC})
        # Each backend's secret is one that provably cannot spend (audit DATA-3); LNbits' needs the
        # operator's word for it.
        for backend, field, secret, args in (
                ("lnd", "macaroon", macaroon(BAKED_OPS), ()),
                ("cln", "rune", rune("=0&method=invoice|method=listinvoices"), ()),
                ("lnbits", "invoice_key", "s3cret", ("--lnbits-key-is-invoice-key",))):
            acct = RailAccount.objects.create(org=self.org, kind="lightning", label=backend, assets=["BTC"],
                                              config={"backend": backend, "url": "https://node.example"})
            self.run_with(acct, secret + "\n", *args)
            self.assertEqual(self.creds(acct), {field: secret}, backend)

    def test_refusals_change_nothing(self):
        with self.assertRaisesMessage(CommandError, "nostr+walletconnect://"):
            self.run_with(self.ln, "https://not-a-connection-string\n")
        with self.assertRaisesMessage(CommandError, "api_key is required"):
            self.run_with(self.btcpay, "\nwhsec\n")
        evm = RailAccount.objects.create(org=self.org, kind="evm", label="EVM", address="0x" + "ab" * 20,
                                         networks=["arbitrum"], assets=["USDC"])
        with self.assertRaisesMessage(CommandError, "no credentials"):
            self.run_with(evm, "x\n")
        with self.assertRaisesMessage(CommandError, "no rail account"):
            call_command("set_rail_credentials", "999999")
        self.assertIsNone(self.creds(self.ln))
        self.assertIsNone(self.creds(self.btcpay))
        self.assertFalse(AuditEntry.objects.filter(action="rail_account.credentials").exists())

    def test_sealed_vault_is_refused_before_anything_is_read(self):
        vault.seal()
        stdin = io.StringIO("KEY\nwhsec\n")
        with mock.patch("sys.stdin", stdin), self.assertRaisesMessage(CommandError, "sealed"):
            call_command("set_rail_credentials", str(self.btcpay.pk))
        self.assertEqual(stdin.tell(), 0)

    def test_a_secret_never_travels_in_argv(self):
        with self.assertRaises(CommandError):
            call_command("set_rail_credentials", str(self.btcpay.pk), "KEY-123")

    def test_at_a_terminal_the_secrets_are_prompted_for_without_echo(self):
        tty = mock.Mock(isatty=mock.Mock(return_value=True))
        with mock.patch("sys.stdin", tty), mock.patch("getpass.getpass", side_effect=["KEY", "whsec"]) as ask:
            call_command("set_rail_credentials", str(self.btcpay.pk), stdout=io.StringIO())
        self.assertEqual([c.args[0] for c in ask.call_args_list], ["api_key: ", "webhook_secret: "])
        self.assertEqual(self.creds(self.btcpay), {"api_key": "KEY", "webhook_secret": "whsec"})
