"""The Lightning credential command accepts a receive-only NWC connection and nothing else."""
import io
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.rails import http
from apps.rails.gateways.lightning import nwc
from apps.rails.models import RailAccount
from apps.vault.testing import UnsealedVaultMixin

NWC = "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32
RECEIVE = {"make_invoice", "lookup_invoice", "get_info", "get_balance"}


class NwcReceiveOnlyTests(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]  # an NWC credential's relays resolve to public addresses
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=public))
        org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.ln = RailAccount.objects.create(org=org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})

    def run_with(self, methods=None, error=None):
        out = io.StringIO()
        patch = mock.patch.object(nwc, "capabilities", side_effect=error) if error else \
            mock.patch.object(nwc, "capabilities", return_value=methods)
        with patch, mock.patch("sys.stdin", io.StringIO(NWC + "\n")):
            call_command("set_rail_credentials", str(self.ln.pk), stdout=out)
        return out.getvalue()

    def refused(self, **kw):
        with self.assertRaises(CommandError) as cm:
            self.run_with(**kw)
        self.assertNotIn(NWC, str(cm.exception))
        self.assertNotIn("cd" * 32, str(cm.exception))
        self.assertIsNone(RailAccount.objects.get(pk=self.ln.pk).credentials)
        self.assertFalse(AuditEntry.objects.filter(action="rail_account.credentials").exists())
        return str(cm.exception)

    def test_nwc_with_pay_methods_refused(self):
        for method in ("pay_invoice", "multi_pay_invoice", "pay_keysend", "multi_pay_keysend"):
            self.assertIn(method, self.refused(methods=RECEIVE | {method}))

    def test_any_pay_method_is_refused_known_or_not(self):
        # pay_offer (BOLT12) and whatever pay_* a later NIP-47 adds can spend as surely as pay_invoice.
        for method in ("pay_offer", "multi_pay_offer", "pay_bolt12_offer", "pay_onchain", "prepay_something"):
            with self.subTest(method):
                self.assertIn(method, self.refused(methods=RECEIVE | {method}))

    def test_nwc_missing_make_invoice_refused(self):
        self.assertIn("make_invoice", self.refused(methods={"lookup_invoice", "get_info"}))
        self.assertIn("lookup_invoice", self.refused(methods={"make_invoice"}))

    def test_nwc_unreachable_refused_and_nothing_stored(self):
        message = self.refused(error=http.Unavailable("lightning", "nwc"))
        self.assertIn("unlock Alby Hub and try again", message)

    def test_receive_only_nwc_accepted(self):
        self.run_with(methods=RECEIVE)
        self.assertEqual(RailAccount.objects.get(pk=self.ln.pk).credentials, {"nwc_url": NWC})

    def test_capabilities_reads_the_method_names_through_the_run_seam(self):
        from nostr_sdk import Method

        info = mock.Mock(methods=[Method.MAKE_INVOICE(), Method.PAY_INVOICE(),
                                  Method.UNKNOWN(unknown="multi_pay_invoice")])

        async def fake_get_info():
            return info

        client = mock.Mock(get_info=fake_get_info)
        import asyncio
        with mock.patch.object(nwc, "_run", side_effect=lambda url, what: asyncio.run(what(client))):
            self.assertEqual(nwc.capabilities(NWC), {"make_invoice", "pay_invoice", "multi_pay_invoice"})
