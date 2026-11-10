"""Audit 2026-11-03, gateways: MONEY-4, DATA-3 with MONEY-10b, and MONEY-9.

- MONEY-4: a BTCPay `speed_policy` is MediumSpeed, LowMediumSpeed or LowSpeed, never HighSpeed
  (0 confirmations) or an unknown string. A BTCPay payment is read again for 72 hours after it
  was confirmed, by the poller and on a webhook; BTCPay then calling its invoice `Invalid` puts it
  in front of a person, with an alert, and undoes nothing.
- DATA-3 / MONEY-10b: a gateway credential is stored only when it provably cannot spend. LND: the
  macaroon's operations, read offline, are within invoices:read, invoices:write and info:read.
  Core Lightning: a rune clause limits `method` to invoice, listinvoices, waitinvoice and getinfo.
  LNbits: the invoice key cannot be told from the admin key offline, so the operator vouches for
  it (`--lnbits-key-is-invoice-key`, audited). BTCPay: the key's own permission list, asked of
  BTCPay at set time, holds nothing beyond the four that create and read invoices.
- MONEY-9: a Frankfurter answer more than 5 days old is refused, and so is a fiat leg more than
  10 % away from the last one accepted for that pair. A refused leg tells staff that quotes in
  that currency are refused and what to do; a jump that is real is accepted by a person on Needs
  a person (TOTP, POST, a note, audited).

`SpendingCredentialsAcceptedTest` and `SettledThenInvalidTests` are the audit's PoCs
(`poc_data_4.py`, `poc_money_4.py`), moved here with their assertions kept.
"""
import base64
import datetime
import io
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.invoices.models import Invoice
from apps.notify import services as notify
from apps.notify.models import OutboxMessage
from apps.orgs.models import Organization
from apps.payments.models import Payment
from apps.payments.services import confirm_payment, reverse_payment
from apps.quotes import services as quotes
from apps.quotes.errors import QuoteError
from apps.quotes.models import Quote
from apps.rails import fx, http
from apps.rails.gateways import btcpay
from apps.rails import models as rails_models
from apps.rails.models import RailAccount, WatcherHealth
from apps.rails.tests_btcpay import INV_ID, SECRET, Base, load
from apps.staff.models import StaffTOTPDevice
from apps.staff.tests import token_for
from apps.vault.testing import UnsealedVaultMixin
from apps.webhooks.models import WebhookEndpoint, WebhookEvent


# ---------------------------------------------------------------------------
# Credentials as the backends print them
# ---------------------------------------------------------------------------
def _uvarint(n: int) -> bytes:
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb(number: int, data: bytes) -> bytes:
    return _uvarint(number << 3 | 2) + _uvarint(len(data)) + data


def _section(kind: int, data: bytes) -> bytes:
    return _uvarint(kind) + _uvarint(len(data)) + data


def macaroon(ops, *, caveats=(), id_version=3, location=b"lnd") -> str:
    """A macaroon as LND bakes it (binary V2, printed in hex): its identifier is the version byte
    3 and a protobuf MacaroonId {nonce, storageId, ops: [{entity, actions}]}."""
    mid = _pb(1, bytes(range(16))) + _pb(2, b"0")
    for entity, actions in ops:
        mid += _pb(3, _pb(1, entity.encode()) + b"".join(_pb(2, a.encode()) for a in actions))
    raw = b"\x02" + (_section(1, location) if location else b"") + _section(2, bytes([id_version]) + mid) + b"\x00"
    for caveat in caveats:
        raw += _section(2, caveat.encode()) + b"\x00"
    return (raw + b"\x00" + _section(6, bytes(32))).hex()


# lnd/rpcperms: what `lncli create` writes as admin.macaroon and invoice.macaroon.
ADMIN_OPS = [("address", ["read", "write"]), ("info", ["read", "write"]), ("invoices", ["read", "write"]),
             ("macaroon", ["generate", "read", "write"]), ("message", ["read", "write"]),
             ("offchain", ["read", "write"]), ("onchain", ["read", "write"]), ("peers", ["read", "write"]),
             ("signer", ["generate", "read"])]
STOCK_INVOICE_OPS = [("invoices", ["read", "write"]), ("address", ["read", "write"]), ("onchain", ["read"])]
BAKED_OPS = [("info", ["read"]), ("invoices", ["read", "write"])]  # lncli bakemacaroon invoices:read invoices:write info:read


def rune(restrictions: str) -> str:
    """A Core Lightning rune: base64url of its 32-byte hash and its restrictions in the clear."""
    return base64.urlsafe_b64encode(bytes(range(32)) + restrictions.encode()).decode()


BTCPAY_FULL = {"apiKey": "k", "label": "everything", "permissions": [
    "btcpay.store.canmodifystoresettings:S", "btcpay.store.canmanagepayouts:S",
    "btcpay.store.cancreatenonapprovedpullpayments:S", "btcpay.store.canviewinvoices:S",
    "btcpay.store.cancreateinvoice:S"]}
BTCPAY_RECEIVE = {"apiKey": "k", "label": "invoices", "permissions": [
    "btcpay.store.canviewinvoices:S", "btcpay.store.cancreateinvoice:S",
    "btcpay.store.webhooks.canmodifywebhooks:S", "btcpay.store.canviewstoresettings:S"]}

# ---------------------------------------------------------------------------
# DATA-3: the PoC (poc_data_4.py)
# ---------------------------------------------------------------------------
# What the usual guides hand out: LND's admin.macaroon (hex), an unrestricted CLN rune, LNbits's
# "Admin key", a BTCPay key with every permission. Any string will do: nothing is checked.
SPENDING = {
    ("lnd", "macaroon"): "0201036c6e6402f801030a10" + "ab" * 40,  # admin.macaroon
    ("cln", "rune"): "tU-RLjMiDpY2U0o3W1oFowar36RFGpWloPbW9-RuZdo9MyZpZD0wMjRiOWExZmE4ZTAwNmYxZTM5MzdmNjVmNjZjNDA4ZTZkYThlMWNhNzI4ZWE0MzIyMmE3MzgxZGYxY2M0NDk2MDUmbWV0aG9kPWxpc3RwZWVycw==",
    ("lnbits", "invoice_key"): "a3c5" + "0" * 28,  # LNbits admin key pasted into the invoice_key slot
}


class SpendingCredentialsAcceptedTest(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")

    def test_only_receive_only_lightning_credentials_are_stored(self):
        stored = []
        for (backend, field), secret in SPENDING.items():
            acct = RailAccount.objects.create(org=self.org, kind="lightning", label=backend, assets=["BTC"],
                                              config={"backend": backend, "url": "https://node.example"})
            with mock.patch("sys.stdin", io.StringIO(secret + "\n")):
                try:
                    call_command("set_rail_credentials", str(acct.pk), stdout=io.StringIO())
                except Exception:  # noqa: BLE001 - refused (the PoC printed which; the output stays quiet)
                    pass
            if (RailAccount.objects.get(pk=acct.pk).credentials or {}).get(field) == secret:
                stored.append(backend)
        btcpay_acct = RailAccount.objects.create(org=self.org, kind="btcpay", label="btcpay", assets=["BTC"],
                                                 config={"url": "https://btcpay.example", "store_id": "S"})
        # The command now asks BTCPay what the key may do; BTCPay answers for a full-permission key.
        with mock.patch("sys.stdin", io.StringIO("full-permission-api-key\nwebhook-secret\n")), \
                mock.patch("apps.rails.http.get_json", return_value=BTCPAY_FULL):
            try:
                call_command("set_rail_credentials", str(btcpay_acct.pk), stdout=io.StringIO())
            except CommandError:  # refused: BTCPay says the key can do more than create and read invoices
                pass
        if (RailAccount.objects.get(pk=btcpay_acct.pk).credentials or {}).get("api_key"):
            stored.append("btcpay")
        self.assertEqual(stored, [], "a spending credential is stored as readily as a receive-only one")


class CredentialBase(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")

    def lightning(self, backend):
        return RailAccount.objects.create(org=self.org, kind="lightning", label=backend, assets=["BTC"],
                                          config={"backend": backend, "url": "https://node.example"})

    def run_with(self, account, stdin, *args):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            call_command("set_rail_credentials", str(account.pk), *args, stdout=out)
        return out.getvalue()

    def creds(self, account):
        return RailAccount.objects.get(pk=account.pk).credentials

    def refused(self, account, secret, *args, stdin=None):
        with self.assertRaises(CommandError) as cm:
            self.run_with(account, stdin if stdin is not None else secret + "\n", *args)
        message = str(cm.exception)
        self.assertNotIn(secret, message)  # the secret is never echoed
        self.assertIsNone(self.creds(account))
        self.assertFalse(AuditEntry.objects.filter(action="rail_account.credentials").exists())
        return message


class LndMacaroonTests(CredentialBase):
    def test_admin_macaroon_is_refused_naming_what_it_can_do(self):
        message = self.refused(self.lightning("lnd"), macaroon(ADMIN_OPS))
        for op in ("offchain:write", "onchain:write", "macaroon:generate", "signer:generate"):
            self.assertIn(op, message)
        self.assertIn("lncli bakemacaroon invoices:read invoices:write info:read", message)

    def test_lnds_own_invoice_macaroon_is_refused_too(self):
        # address:write and onchain:read are outside the allow-list: bake a narrower one.
        message = self.refused(self.lightning("lnd"), macaroon(STOCK_INVOICE_OPS))
        self.assertIn("address:write", message)
        self.assertIn("onchain:read", message)
        self.assertNotIn("invoices:read", message.split("(")[1].split(")")[0])

    def test_a_baked_invoice_macaroon_is_stored(self):
        acct = self.lightning("lnd")
        hexed = macaroon(BAKED_OPS)
        self.run_with(acct, hexed + "\n")
        self.assertEqual(self.creds(acct), {"macaroon": hexed})
        acct2 = self.lightning("lnd")
        upper = macaroon([("invoices", ["read", "write"])], caveats=["time-before 2030-01-01T00:00:00Z"]).upper()
        self.run_with(acct2, upper + "\n")  # caveats only narrow it; hex in either case
        self.assertEqual(self.creds(acct2), {"macaroon": upper})

    def test_a_macaroon_that_cannot_make_or_read_invoices_is_refused(self):
        self.assertIn("invoices:write", self.refused(self.lightning("lnd"), macaroon([("invoices", ["read"])])))
        self.assertIn("invoices:read", self.refused(self.lightning("lnd"), macaroon([("info", ["read"])])))

    def test_uri_permissions_are_refused(self):
        hexed = macaroon([("invoices", ["read", "write"]), ("uri", ["/lnrpc.Lightning/SendPaymentSync"])])
        self.assertIn("uri:/lnrpc.Lightning/SendPaymentSync", self.refused(self.lightning("lnd"), hexed))

    def test_what_cannot_be_read_is_refused(self):
        good = macaroon(BAKED_OPS)
        for secret in (SPENDING[("lnd", "macaroon")],  # truncated: the identifier is longer than the rest
                       "not hex at all", base64.b64encode(bytes.fromhex(good)).decode(),
                       good[:-2], good + "00", "03" + good[2:],
                       macaroon(BAKED_OPS, id_version=2), ""):
            with self.subTest(secret=secret[:16]):
                if not secret:
                    continue  # an empty answer is "keep the stored one"
                self.assertIn("macaroon", self.refused(self.lightning("lnd"), secret))


class ClnRuneTests(CredentialBase):
    def test_a_rune_limited_to_invoices_is_stored(self):
        for restrictions in ("=0&method=invoice|method=listinvoices",
                             "=7&method=invoice|method=listinvoices|method=waitinvoice|method=getinfo",
                             "=1&id=024b9a1f&method=invoice|method=listinvoices&rate=60",
                             "=2&method=invoice|method=listinvoices|method=pay&method=invoice|method=listinvoices"):
            with self.subTest(restrictions):
                acct = self.lightning("cln")
                secret = rune(restrictions)
                self.run_with(acct, secret + "\n")
                self.assertEqual(self.creds(acct), {"rune": secret})

    def test_a_rune_that_can_call_other_methods_is_refused(self):
        cases = {
            "": "does not limit",  # the master rune
            "=0": "does not limit",
            "=0&method=invoice|method=listinvoices|method=pay": "pay",
            "=0&method/pay": "does not limit",  # "anything but pay" is still almost everything
            "=0&method^list": "does not limit",
            "=0&method=invoice|id=02ab": "does not limit",  # the id alone lets any method through
            "=0&method=invoice|method=listinvoices\\|pay": "listinvoices|pay",  # an escaped |, not a choice
            "=0&method=listpeers": "listpeers",
        }
        for restrictions, expected in cases.items():
            with self.subTest(restrictions):
                self.assertIn(expected, self.refused(self.lightning("cln"), rune(restrictions)))

    def test_a_rune_that_cannot_make_or_read_invoices_is_refused(self):
        self.assertIn("listinvoices", self.refused(self.lightning("cln"), rune("=0&method=invoice")))
        both = "=0&method=invoice|method=listinvoices&method=getinfo|method=invoice"  # together: invoice only
        self.assertIn("listinvoices", self.refused(self.lightning("cln"), rune(both)))

    def test_the_poc_rune_and_garbage_are_refused(self):
        self.assertIn("listpeers", self.refused(self.lightning("cln"), SPENDING[("cln", "rune")]))
        for secret in ("not a rune!", base64.urlsafe_b64encode(b"short").decode(), rune("=0&method"),
                       rune("=0&method=invoice\\"), rune("=0&method=invoice&=1")):
            with self.subTest(secret=secret[:12]):
                self.assertIn("rune", self.refused(self.lightning("cln"), secret))


class LnbitsKeyTests(CredentialBase):
    def test_an_lnbits_key_is_refused_unless_the_operator_vouches_for_it(self):
        message = self.refused(self.lightning("lnbits"), "a3c5" + "0" * 28)
        self.assertIn("--lnbits-key-is-invoice-key", message)
        self.assertIn("Admin key", message)

    def test_the_vouched_key_is_stored_and_the_flag_audited(self):
        acct = self.lightning("lnbits")
        self.run_with(acct, "invoice-read-key\n", "--lnbits-key-is-invoice-key")
        self.assertEqual(self.creds(acct), {"invoice_key": "invoice-read-key"})
        entry = AuditEntry.objects.get(action="rail_account.credentials", object_pk=str(acct.pk))
        self.assertEqual(entry.detail, {"rail_account": acct.pk, "kind": "lightning", "fields": ["invoice_key"],
                                        "lnbits_key_is_invoice_key": True})

    def test_the_flag_is_only_for_lnbits(self):
        message = self.refused(self.lightning("lnd"), macaroon(BAKED_OPS), "--lnbits-key-is-invoice-key")
        self.assertIn("LNbits", message)


class BTCPayKeyTests(CredentialBase):
    def setUp(self):
        super().setUp()
        self.acct = RailAccount.objects.create(org=self.org, kind="btcpay", label="BTCPay", assets=["BTC"],
                                               config={"url": "https://btcpay.example/", "store_id": "S"})

    def asked(self, answer):
        return mock.patch("apps.rails.http.get_json", side_effect=answer if isinstance(answer, Exception) else None,
                          return_value=None if isinstance(answer, Exception) else answer)

    def test_a_full_permission_key_is_refused_naming_the_permissions(self):
        with self.asked(BTCPAY_FULL) as get:
            message = self.refused(self.acct, "full-key", stdin="full-key\nwhsec\n")
        self.assertIn("btcpay.store.canmodifystoresettings", message)
        self.assertIn("btcpay.store.canmanagepayouts", message)
        self.assertIn("btcpay.store.cancreatenonapprovedpullpayments", message)
        self.assertNotIn("canviewinvoices", message.split("(")[1].split(")")[0])
        url = get.call_args.args[0]
        self.assertEqual(url, "https://btcpay.example/api/v1/api-keys/current")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "token full-key")
        self.assertEqual(get.call_args.kwargs["service"], "btcpay")

    def test_an_unrestricted_or_server_key_is_refused(self):
        for perms in (["unrestricted"], ["btcpay.server.canmodifyserversettings",
                                         *BTCPAY_RECEIVE["permissions"]]):
            with self.subTest(perms[0]), self.asked({"permissions": perms}):
                self.assertIn(perms[0], self.refused(self.acct, "k1", stdin="k1\nwhsec\n"))

    def test_a_key_limited_to_invoices_is_stored(self):
        with self.asked(BTCPAY_RECEIVE):
            self.run_with(self.acct, "good-key\nwhsec\n")
        self.assertEqual(self.creds(self.acct), {"api_key": "good-key", "webhook_secret": "whsec"})
        unscoped = {"permissions": ["btcpay.store.canviewinvoices", "btcpay.store.cancreateinvoice"]}
        with self.asked(unscoped):
            self.run_with(self.acct, "other-key\n\n")
        self.assertEqual(self.creds(self.acct)["api_key"], "other-key")

    def test_a_key_that_cannot_make_or_read_invoices_here_is_refused(self):
        for perms in (["btcpay.store.canviewinvoices:S"],
                      ["btcpay.store.canviewinvoices:OTHER", "btcpay.store.cancreateinvoice:OTHER"]):
            with self.subTest(perms), self.asked({"permissions": perms}):
                self.assertIn("lacks", self.refused(self.acct, "k2", stdin="k2\nwhsec\n"))

    def test_btcpay_that_cannot_be_asked_stores_nothing(self):
        with self.asked(http.Unavailable("btcpay", "timeout")):
            self.assertIn("could not ask BTCPay", self.refused(self.acct, "k3", stdin="k3\nwhsec\n"))
        with self.asked(http.Refused("btcpay", 401)):
            self.assertIn("refused this API key", self.refused(self.acct, "k4", stdin="k4\nwhsec\n"))
        for odd in ({"permissions": "all"}, {"nope": 1}, [1, 2], {"permissions": [1]}):
            with self.subTest(odd), self.asked(odd), self.assertLogs("apps.rails.gateways.btcpay", "WARNING"):
                self.assertIn("could not ask BTCPay", self.refused(self.acct, "k5", stdin="k5\nwhsec\n"))

    def test_replacing_only_the_webhook_secret_checks_the_stored_key(self):
        acct = RailAccount.objects.get(pk=self.acct.pk)
        acct.credentials = {"api_key": "old-full-key", "webhook_secret": "w"}  # stored before this check existed
        acct.save()
        with self.asked(BTCPAY_FULL), self.assertRaises(CommandError):
            self.run_with(acct, "\nnew-secret\n")
        self.assertEqual(self.creds(acct), {"api_key": "old-full-key", "webhook_secret": "w"})


# ---------------------------------------------------------------------------
# MONEY-4: the PoC (poc_money_4.py)
# ---------------------------------------------------------------------------
class SettledThenInvalidTests(Base):
    def test_a_zero_conf_policy_is_accepted_and_a_later_invalid_is_never_seen(self):
        acct = RailAccount.objects.get(pk=self.acct.pk)
        acct.config = {**acct.config, "speed_policy": "HighSpeed"}  # 0 confirmations at BTCPay
        with self.assertRaises(ValidationError):  # fixed: a 0-conf policy is refused in validation
            acct.full_clean()
        RailAccount.objects.filter(pk=acct.pk).update(config=acct.config)  # saved before the fix

        q = self.quote()
        self.set_state("Settled", paid="0.00123456")  # BTCPay: settled (at 0-conf under HighSpeed)
        self.patch_http()
        btcpay.btcpay_poll()
        self.assertEqual(self.payment().state, "confirmed")
        self.assertEqual(Invoice_status(self.invoice), "paid")
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

        # The payer replaces the transaction (RBF) / a reorg drops it: BTCPay now says Invalid.
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):  # the port: logs captured, nothing else
            looked = btcpay.btcpay_poll()
        # The webhook BTCPay sends for it flags nothing either (paid quotes are excluded).
        Quote.objects.filter(pk=q.pk).update(check_soon=False)
        with self.assertLogs("apps.rails.views", "WARNING"):  # the PoC posts it unsigned
            self.client.post(f"/hooks/btcpay/{self.acct.pk}/", b"{}", content_type="application/json")
        p = self.payment()
        reviews = OutboxMessage.objects.filter(kind="review_staff").count()
        self.assertTrue(
            looked > 0 or p.state != "confirmed" or reviews,
            f"BTCPay says Invalid, yet poll looked at {looked} quote(s); payment stays {p.state}, "
            f"invoice {Invoice_status(self.invoice)!r}, staff review letters: {reviews}")


def Invoice_status(invoice):
    return Invoice.objects.filter(pk=invoice.pk).values_list("status", flat=True).get()


class SpeedPolicyTests(Base):
    def validate(self, config):
        acct = RailAccount.objects.get(pk=self.acct.pk)
        acct.config = config
        acct.full_clean()

    def test_only_policies_that_wait_for_a_confirmation_are_accepted(self):
        base = {"url": "https://btcpay.example/", "store_id": "ST0RE1"}
        for policy in ("MediumSpeed", "LowMediumSpeed", "LowSpeed"):
            self.validate({**base, "speed_policy": policy})
        self.validate(base)  # absent: MediumSpeed
        for policy in ("HighSpeed", "highspeed", "Fast", "", None, 0, ["LowSpeed"]):
            with self.subTest(policy=policy), self.assertRaises(ValidationError) as cm:
                self.validate({**base, "speed_policy": policy})
            self.assertIn("speed_policy", str(cm.exception))

    def test_a_zero_conf_account_saved_before_the_fix_makes_no_new_invoice(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(
            config={"url": "https://btcpay.example/", "store_id": "ST0RE1", "speed_policy": "HighSpeed"})
        self.patch_http()
        with self.assertLogs("apps.rails.gateways.btcpay", "WARNING"), self.assertRaises(QuoteError) as cm:
            quotes.create_quote(self.invoice, RailAccount.objects.get(pk=self.acct.pk), "bitcoin", "BTC")
        self.assertEqual(cm.exception.code, "rail_unavailable")
        self.assertEqual(self.calls, [])  # nothing was asked of BTCPay
        name = f"btcpay:{self.acct.pk}"
        h = WatcherHealth.objects.get(name=name)
        self.assertEqual((h.last_error_kind, h.failing_since is not None), ("speed_policy", True))
        # Its existing quotes are still read, and a good read does not clear what is still wrong.
        q = self.quote()
        self.set_state("Processing", paid="0.00123456")
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(self.payment().state, "detected")
        self.assertTrue(Quote.objects.filter(pk=q.pk).exists())
        self.assertEqual(WatcherHealth.objects.get(name=name).last_error_kind, "speed_policy")
        self.assertIsNotNone(WatcherHealth.objects.get(name=name).failing_since)
        # Needs a person says what it means and what to do.
        user = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        device = StaffTOTPDevice.objects.create(user=user, name="t", confirmed=True)
        self.client.force_login(user)
        self.client.post("/verify/", {"token": token_for(device)})
        page = self.client.get("/").content.decode()
        self.assertIn(name, page)
        self.assertIn("No new BTCPay quote is made", page)
        self.assertIn("MediumSpeed, LowMediumSpeed or LowSpeed", page)
        # Fixed in the admin: the next pass is a good one.
        RailAccount.objects.filter(pk=self.acct.pk).update(
            config={"url": "https://btcpay.example/", "store_id": "ST0RE1", "speed_policy": "LowSpeed"})
        btcpay.btcpay_poll()
        self.assertIsNone(WatcherHealth.objects.get(name=name).failing_since)

    @override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"], SITE_URL="https://pay.example")
    def test_the_alert_says_quotes_are_refused_not_that_payments_go_unseen(self):
        from apps.rails import jobs

        RailAccount.objects.filter(pk=self.acct.pk).update(
            config={"url": "https://btcpay.example/", "store_id": "ST0RE1", "speed_policy": "HighSpeed"})
        self.patch_http()
        with self.assertLogs("apps.rails.gateways.btcpay", "WARNING"), self.assertRaises(QuoteError):
            quotes.create_quote(self.invoice, RailAccount.objects.get(pk=self.acct.pk), "bitcoin", "BTC")
        WatcherHealth.objects.update(failing_since=timezone.now() - datetime.timedelta(minutes=11))
        jobs.watcher_alert()
        (alert,) = mail.outbox
        self.assertIn(f"btcpay:{self.acct.pk}", alert.body)
        self.assertIn("No new BTCPay quote is made", alert.body)
        self.assertNotIn("not being seen", alert.body)

    def test_the_default_policy_is_medium_speed(self):
        RailAccount.objects.filter(pk=self.acct.pk).update(config={"url": "https://btcpay.example/",
                                                                   "store_id": "ST0RE1"})
        self.patch_http()
        quotes.create_quote(self.invoice, RailAccount.objects.get(pk=self.acct.pk), "bitcoin", "BTC")
        post = next(c for c in self.calls if c[0] == "POST")
        self.assertEqual(post[2]["checkout"]["speedPolicy"], "MediumSpeed")


class InvalidAfterConfirmationTests(Base):
    """BTCPay settles, the payment is confirmed and the invoice paid; BTCPay later reports the
    invoice `Invalid`. A person decides: nothing is reversed by code."""

    def confirm(self):
        q = self.quote()
        self.set_state("Settled", paid="0.00123456")
        self.patch_http()
        self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual((self.payment().state, Invoice_status(self.invoice)), ("confirmed", "paid"))
        OutboxMessage.objects.all().delete()
        return q

    def staff_in(self):
        user = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        device = StaffTOTPDevice.objects.create(user=user, name="t", confirmed=True)
        self.client.force_login(user)
        self.assertEqual(self.client.post("/verify/", {"token": token_for(device)}).status_code, 302)
        return user

    def test_invalid_after_confirmation_goes_to_a_person_and_undoes_nothing(self):
        WebhookEndpoint.objects.create(org=self.org, url="https://hook.example/x", secret="s", events=["staff.alert"])
        q = self.confirm()
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING") as logs:
            self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertNotIn(INV_ID, "".join(logs.output))
        p = self.payment()
        self.assertEqual((p.state, p.amount_minor, p.review_reason), ("confirmed", 10000, ""))
        self.assertIsNotNone(p.gateway_invalid_at)
        self.assertEqual(Invoice_status(self.invoice), "paid")
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")
        letter = OutboxMessage.objects.get(kind="review_staff")
        self.assertEqual(letter.plain_context["payment_id"], p.pk)
        self.assertEqual(letter.plain_context["reason"], "btcpay_invalid")
        self.assertIs(letter.plain_context["confirmed"], True)
        alert = WebhookEvent.objects.get(type="staff.alert")
        self.assertEqual(alert.data, {"kind": "payment_invalid_after_confirmation", "payment_id": p.pk,
                                      "invoice_id": self.invoice.pk, "reason": "btcpay_invalid"})
        self.assertTrue(AuditEntry.objects.filter(action="payment.gateway_invalid", object_pk=str(p.pk)).exists())
        # Told once: every later poll in the window sees it again and says nothing more.
        btcpay.btcpay_poll()
        btcpay.btcpay_poll()
        self.assertEqual(OutboxMessage.objects.filter(kind="review_staff").count(), 1)
        self.assertEqual(WebhookEvent.objects.filter(type="staff.alert").count(), 1)
        self.assertEqual(AuditEntry.objects.filter(action="payment.gateway_invalid").count(), 1)
        self.assertEqual(self.payment().state, "confirmed")

    @override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"], SITE_URL="https://pay.example")
    def test_the_letter_says_it_was_confirmed_and_nothing_was_undone(self):
        self.confirm()
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):
            btcpay.btcpay_poll()
        self.assertEqual(notify.flush_outbox(), 1)
        (letter,) = mail.outbox
        self.assertEqual(letter.subject, f"Payment #{self.payment().pk} needs review")
        self.assertIn("BTCPay reports the invoice as invalid.", letter.body)
        self.assertIn("nothing was undone", letter.body)
        self.assertNotIn("Assign it to an invoice", letter.body)

    def test_a_person_keeps_it_or_reverses_it(self):
        self.confirm()
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):
            btcpay.btcpay_poll()
        p = self.payment()
        self.staff_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Confirmed payments the gateway now calls invalid", page)
        self.assertIn(f"/payments/{p.pk}/gateway-invalid/", page)
        self.assertEqual(self.client.post(f"/payments/{p.pk}/gateway-invalid/", {"note": ""}).status_code, 409)
        r = self.client.post(f"/payments/{p.pk}/gateway-invalid/", {"note": "Checked BTCPay: it is there"})
        self.assertEqual(r.status_code, 302)
        p = self.payment()
        self.assertIsNotNone(p.gateway_invalid_acknowledged_at)
        self.assertEqual((p.state, Invoice_status(self.invoice)), ("confirmed", "paid"))
        self.assertTrue(AuditEntry.objects.filter(action="payment.gateway_invalid_ack", object_pk=str(p.pk)).exists())
        self.assertNotIn(f"/payments/{p.pk}/gateway-invalid/", self.client.get("/").content.decode())
        self.assertEqual(self.client.post(f"/payments/{p.pk}/gateway-invalid/", {"note": "again"}).status_code, 409)

    def test_reversing_it_takes_it_off_the_list(self):
        self.confirm()
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):
            btcpay.btcpay_poll()
        user = self.staff_in()
        reverse_payment(self.payment(), user, "double spent")
        self.assertEqual(Invoice_status(self.invoice), "sent")
        page = self.client.get("/").content.decode()
        self.assertNotIn(f"/payments/{self.payment().pk}/gateway-invalid/", page)
        btcpay.btcpay_poll()  # a reversed payment is not flagged again
        self.assertEqual(OutboxMessage.objects.filter(kind="review_staff").count(), 1)

    def test_still_settled_changes_nothing(self):
        q = self.confirm()
        for _ in range(3):
            self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        p = self.payment()
        self.assertEqual((p.state, p.gateway_invalid_at), ("confirmed", None))
        self.assertFalse(OutboxMessage.objects.exists())
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

    def test_watched_for_72_hours_after_confirmation_even_once_released(self):
        q = self.confirm()
        ago = timezone.now() - datetime.timedelta(hours=71)
        Payment.objects.filter(pk=self.payment().pk).update(confirmed_at=ago)
        Quote.objects.filter(pk=q.pk).update(closed_at=ago)
        self.assertEqual(quotes.release_quotes(timezone.now()), 1)  # a paid quote frees its slot after a day
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "released")
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):
            self.assertEqual(btcpay.btcpay_poll(), 1)
        self.assertIsNotNone(self.payment().gateway_invalid_at)
        Payment.objects.filter(pk=self.payment().pk).update(
            gateway_invalid_at=None, confirmed_at=timezone.now() - datetime.timedelta(hours=73))
        self.calls.clear()
        self.assertEqual(btcpay.btcpay_poll(), 0)
        self.assertEqual(self.calls, [])

    def test_the_webhook_flags_a_payment_confirmed_in_the_last_72_hours(self):
        import hashlib
        import hmac
        import json

        q = self.confirm()
        Quote.objects.filter(pk=q.pk).update(check_soon=False)

        def hook():
            raw = json.dumps({"invoiceId": INV_ID, "type": "InvoiceInvalid"}).encode()
            sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
            return self.client.post(f"/hooks/btcpay/{self.acct.pk}/", raw, content_type="application/json",
                                    HTTP_BTCPAY_SIG=sig)

        self.assertEqual(hook().status_code, 200)
        self.assertTrue(Quote.objects.get(pk=q.pk).check_soon)
        Quote.objects.filter(pk=q.pk).update(check_soon=False, state="released")
        Payment.objects.filter(pk=self.payment().pk).update(confirmed_at=timezone.now() - datetime.timedelta(hours=73))
        self.assertEqual(hook().status_code, 200)
        self.assertFalse(Quote.objects.get(pk=q.pk).check_soon)

    def test_monero_confirmed_by_the_chain_then_invalid_goes_to_a_person(self):
        self.methods = load("payment_methods_xmr.json")
        q = self.quote("XMR", "monero", 512345678901)
        self.set_state("Settled", paid="0.512345678901")
        self.patch_http()
        btcpay.btcpay_poll()
        self.assertEqual((self.payment().state, Invoice_status(self.invoice)), ("confirmed", "paid"))
        self.set_state("Invalid", paid="0")
        with self.assertLogs("apps.payments.matching", "WARNING"):
            self.assertEqual(btcpay.btcpay_poll(), 1)
        p = self.payment()
        self.assertEqual(p.state, "confirmed")
        self.assertIsNotNone(p.gateway_invalid_at)
        self.assertEqual(Quote.objects.get(pk=q.pk).state, "paid")

    def test_a_detected_payment_turning_invalid_is_reviewed_as_before(self):
        self.quote()
        self.set_state("Processing", paid="0.00123456")
        self.patch_http()
        btcpay.btcpay_poll()
        self.set_state("Invalid", paid="0.00123456")
        btcpay.btcpay_poll()
        p = self.payment()
        self.assertEqual((p.state, p.review_reason, p.gateway_invalid_at), ("needs_review", "btcpay_invalid", None))


# ---------------------------------------------------------------------------
# MONEY-9: the Frankfurter leg
# ---------------------------------------------------------------------------
class Prices:
    """Bitstamp and Coinbase agree on BTC in EUR; Frankfurter answers `ecb` dated `day`."""

    def __init__(self, ecb="24.353", day=None, btc="69583"):
        self.ecb, self.day, self.btc, self.frankfurter_calls = ecb, day, btc, 0

    def __call__(self, url, *, params=None, headers=None, service, timeout=10):
        if "bitstamp" in url:
            return {"last": self.btc, "timestamp": "1794260704"}
        if "coinbase" in url:
            return {"data": {"amount": self.btc, "base": "BTC", "currency": "EUR"}}
        self.frankfurter_calls += 1
        day = self.day or timezone.now().date()
        return {"amount": 1.0, "base": "EUR", "date": day.isoformat(), "rates": {"CZK": float(self.ecb)}}


class FrankfurterLegTests(UnsealedVaultMixin, TestCase):  # accepting a rate writes an audit entry
    def setUp(self):
        super().setUp()
        cache.clear()

    def price(self, prices, currency="CZK"):
        cache.clear()
        with mock.patch("apps.rails.fx.http.get_json", side_effect=prices):
            return fx.price_in("BTC", currency)

    def refused(self, prices, kind):
        """The leg is refused, said once in the log (by kind, never a rate)."""
        with self.assertLogs("apps.rails.fx", "WARNING") as logs, self.assertRaises(fx.RatesUnavailable) as cm:
            self.price(prices)
        self.assertEqual(logs.output, [f"WARNING:apps.rails.fx:fx: the fx:EUR:CZK leg was refused ({kind})"])
        return str(cm.exception)

    def days_ago(self, n):
        return timezone.now().date() - datetime.timedelta(days=n)

    def test_an_answer_five_days_old_is_used_six_days_old_refused(self):
        # Five: on Easter Tuesday morning the newest ECB rate is Thursday's (no rate on Good Friday
        # or Easter Monday), and every non-direct fiat quote must still be made.
        p = self.price(Prices(day=self.days_ago(5)))
        self.assertEqual(p.rate, Decimal("69583") * Decimal("24.353"))
        rails_models.FxLegRate.objects.all().delete()
        self.assertIn("stale", self.refused(Prices(day=self.days_ago(6)), "stale"))
        self.assertFalse(rails_models.FxLegRate.objects.exists())  # a refused answer is never the reference

    def test_an_answer_without_a_usable_date_is_refused(self):
        for broken in ({"rates": {"CZK": 24.353}}, {"date": "Friday", "rates": {"CZK": 24.353}},
                       {"date": None, "rates": {"CZK": 24.353}}):
            with self.subTest(broken), self.assertRaises(fx.RatesUnavailable):
                cache.clear()
                with mock.patch("apps.rails.fx.http.get_json",
                                side_effect=lambda url, **kw: broken if "frankfurter" in url else Prices()(url, **kw)):
                    fx.price_in("BTC", "CZK")
        self.refused(Prices(day=timezone.now().date() + datetime.timedelta(days=3)), "stale")
        self.assertFalse(rails_models.FxLegRate.objects.exists())

    def test_the_first_answer_is_stored_as_the_reference(self):
        self.price(Prices(ecb="24.353"))
        row = rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")
        self.assertEqual((Decimal(row.rate), row.source_date), (Decimal("24.353"), timezone.now().date()))

    def test_a_leg_more_than_ten_percent_from_the_last_one_is_refused(self):
        self.price(Prices(ecb="24.353"))
        for jumped in ("26.80", "21.90"):  # +10.05 %, -10.07 %
            with self.subTest(jumped):
                self.assertIn("10 %", self.refused(Prices(ecb=jumped), "jump"))
                self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(base="EUR", quote="CZK").rate),
                                 Decimal("24.353"))
        h = WatcherHealth.objects.get(name="fx:EUR:CZK")
        self.assertIsNotNone(h.failing_since)
        self.assertEqual(h.last_error_kind, "jump")
        # Asking again does not walk the reference towards the jump.
        self.refused(Prices(ecb="26.80"), "jump")

    def test_a_leg_within_ten_percent_is_used_and_becomes_the_reference(self):
        self.price(Prices(ecb="24.353", day=self.days_ago(1)))
        p = self.price(Prices(ecb="26.78"))  # +9.97 %, the next day
        self.assertEqual(p.rate, Decimal("69583") * Decimal("26.78"))
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(base="EUR", quote="CZK").rate), Decimal("26.78"))
        self.assertIsNone(WatcherHealth.objects.get(name="fx:EUR:CZK").failing_since)

    def test_a_stale_answer_marks_the_leg_failing_and_a_fresh_one_clears_it(self):
        self.refused(Prices(day=self.days_ago(6)), "stale")
        self.assertEqual(WatcherHealth.objects.get(name="fx:EUR:CZK").last_error_kind, "stale")
        self.price(Prices())
        self.assertIsNone(WatcherHealth.objects.get(name="fx:EUR:CZK").failing_since)

    def test_a_person_accepting_the_refused_answer_moves_the_reference(self):
        self.price(Prices(ecb="24.353"))
        self.refused(Prices(ecb="30.00"), "jump")
        leg = rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")
        user = get_user_model().objects.create_user("clerk", is_staff=True)
        with self.assertRaises(ValueError):
            fx.accept_leg(leg, user, "  ")  # a note is required
        self.assertTrue(fx.accept_leg(leg, user, "the ECB published 30.00"))
        self.assertFalse(fx.accept_leg(leg, user, "twice"))  # nothing waits any more
        self.assertEqual(self.price(Prices(ecb="30.00")).rate, Decimal("69583") * Decimal("30.00"))
        self.assertIsNone(WatcherHealth.objects.get(name="fx:EUR:CZK").failing_since)

    def test_a_jump_keeps_the_refused_answer_for_a_person_and_moves_nothing(self):
        self.price(Prices(ecb="24.353", day=self.days_ago(1)))
        self.refused(Prices(ecb="30.00"), "jump")
        row = rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")
        self.assertEqual((Decimal(row.rate), Decimal(row.pending_rate)), (Decimal("24.353"), Decimal("30.00")))
        self.refused(Prices(ecb="30.10"), "jump")  # the newest refused answer is the one offered
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(pk=row.pk).pending_rate), Decimal("30.10"))
        self.price(Prices(ecb="24.50"))  # back within 10 %: used, and nothing waits for a person
        row = rails_models.FxLegRate.objects.get(pk=row.pk)
        self.assertEqual((Decimal(row.rate), row.pending_rate), (Decimal("24.50"), ""))

    def test_another_rate_for_a_day_already_accepted_waits_for_a_person(self):
        # Red-team A5: inside the bound every call used to move the reference, so a lying source
        # walked it to 1.77 times the start in six calls. One ECB day has one rate.
        self.price(Prices(ecb="24.353"))
        self.assertIn("a person must check", self.refused(Prices(ecb="26.00"), "changed"))
        row = rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")
        self.assertEqual((Decimal(row.rate), Decimal(row.pending_rate)), (Decimal("24.353"), Decimal("26.00")))
        self.assertIn("never changes a published rate", fx.leg_explanation("fx:EUR:CZK", "changed"))
        user = get_user_model().objects.create_user("clerk", is_staff=True)
        self.assertTrue(fx.accept_leg(row, user, "the ECB corrected it"))  # a person may still take it
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(pk=row.pk).rate), Decimal("26.00"))
        self.assertEqual(self.price(Prices(ecb="26.000")).rate, Decimal("69583") * Decimal("26.00"))  # same rate

    def test_a_walk_inside_the_bound_takes_a_new_ecb_day_per_step(self):
        self.price(Prices(ecb="24.353", day=self.days_ago(2)))
        self.price(Prices(ecb="26.70", day=self.days_ago(1)))   # +9.6 % the next day
        self.refused(Prices(ecb="29.30", day=self.days_ago(1)), "changed")  # again that day: no
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(base="EUR", quote="CZK").rate), Decimal("26.70"))

    def test_an_answer_older_than_the_reference_uses_the_reference(self):
        self.price(Prices(ecb="24.353"))
        p = self.price(Prices(ecb="25.00", day=self.days_ago(1)))  # a cache a day behind
        self.assertEqual(p.rate, Decimal("69583") * Decimal("24.353"))
        row = rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")
        self.assertEqual((Decimal(row.rate), row.source_date), (Decimal("24.353"), timezone.now().date()))

    def test_the_admin_cannot_change_or_delete_the_reference(self):
        from django.contrib import admin

        model_admin = admin.site._registry[rails_models.FxLegRate]
        for allowed in (model_admin.has_add_permission(None), model_admin.has_change_permission(None),
                        model_admin.has_delete_permission(None)):
            self.assertFalse(allowed)

    def test_each_currency_has_its_own_reference(self):
        self.price(Prices(ecb="24.353"))

        def pln(url, **kw):
            if "frankfurter" in url:
                return {"date": timezone.now().date().isoformat(), "rates": {"PLN": 4.25}}
            return Prices()(url, **kw)

        self.price(pln, "PLN")
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(quote="PLN").rate), Decimal("4.25"))
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(quote="CZK").rate), Decimal("24.353"))

    def test_direct_fiat_never_asks_frankfurter(self):
        prices = Prices()
        self.price(prices, "EUR")
        self.assertEqual(prices.frankfurter_calls, 0)
        self.assertFalse(rails_models.FxLegRate.objects.exists())


@override_settings(STAFF_NOTIFY_EMAILS=["staff@example.com"], SITE_URL="https://pay.example")
class RefusedLegForStaffTests(UnsealedVaultMixin, TestCase):
    """A refused fiat leg tells staff that quotes in that currency are refused and what to do about
    it, and a real move is accepted by a person, never by code."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = get_user_model().objects.create_user("clerk", password="pw-clerk-1", is_staff=True)
        self.device = StaffTOTPDevice.objects.create(user=self.user, name="t", confirmed=True)

    def price(self, prices):
        cache.clear()
        with mock.patch("apps.rails.fx.http.get_json", side_effect=prices):
            return fx.price_in("BTC", "CZK")

    def jump(self):
        self.price(Prices(ecb="24.353"))
        with self.assertLogs("apps.rails.fx", "WARNING"), self.assertRaises(fx.RatesUnavailable):
            self.price(Prices(ecb="30.00"))
        WatcherHealth.objects.filter(name="fx:EUR:CZK").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        return rails_models.FxLegRate.objects.get(base="EUR", quote="CZK")

    def stale(self):
        with self.assertLogs("apps.rails.fx", "WARNING"), self.assertRaises(fx.RatesUnavailable):
            self.price(Prices(day=timezone.now().date() - datetime.timedelta(days=8)))
        WatcherHealth.objects.filter(name="fx:EUR:CZK").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))

    def sign_in(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/verify/", {"token": token_for(self.device)}).status_code, 302)

    def test_the_jump_alert_says_quotes_are_refused_and_how_to_accept_a_real_move(self):
        from apps.rails import jobs

        self.jump()
        jobs.watcher_alert()
        (alert,) = mail.outbox
        self.assertIn("CZK", alert.subject)
        self.assertIn("Quotes in CZK are refused", alert.body)
        self.assertIn("24.353", alert.body)
        self.assertIn("30", alert.body)
        self.assertIn("Check the ECB's rate", alert.body)
        self.assertIn("accept it", alert.body)
        self.assertIn("https://pay.example/", alert.body)
        self.assertNotIn("not being seen", alert.body)
        self.assertNotIn("admin", alert.body)

    def test_the_stale_alert_says_wait_for_the_ecb_or_check_frankfurter(self):
        from apps.rails import jobs

        self.stale()
        jobs.watcher_alert()
        (alert,) = mail.outbox
        self.assertIn("Quotes in CZK are refused", alert.body)
        self.assertIn("wait for the ECB", alert.body)
        self.assertIn("FX_FRANKFURTER_URL", alert.body)
        self.assertNotIn("not being seen", alert.body)

    def test_a_payment_watcher_alongside_keeps_its_own_wording(self):
        from apps.rails import health, jobs

        self.jump()
        health.failed("scan:evm:arbitrum:1", "timeout")
        WatcherHealth.objects.filter(name="scan:evm:arbitrum:1").update(
            failing_since=timezone.now() - datetime.timedelta(minutes=11))
        jobs.watcher_alert()
        (alert,) = mail.outbox
        self.assertIn("watcher failing", alert.subject)
        unseen = alert.body.split("so payments on them are not being seen:\n\n")[1].split("\n\n")[0]
        self.assertEqual(unseen, "  scan:evm:arbitrum:1: timeout")  # the FX leg is not among them
        self.assertIn("Quotes in CZK are refused", alert.body)

    def test_needs_a_person_shows_the_refusal_and_the_accept_action(self):
        leg = self.jump()
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Exchange rates refused", page)
        self.assertIn("Quotes in CZK are refused", page)
        self.assertIn(f'action="/fx/{leg.pk}/accept/"', page)
        self.assertIn("24.353", page)
        self.assertIn("30", page)
        # Not "payments are not recorded": the leg stands in its own queue, and the watchers' queue,
        # empty, is folded into the all-clear line with its own sentence.
        self.assertIn("CZK", page.split('<section class="queue" id="fx">')[1].split("</section>")[0])
        self.assertNotIn('<section class="queue" id="watchers">', page)
        self.assertIn("Every watcher can look.", page.split('<details class="all-clear"')[1])

    def test_a_stale_leg_offers_no_accept(self):
        self.stale()
        self.sign_in()
        page = self.client.get("/").content.decode()
        self.assertIn("Quotes in CZK are refused", page)
        self.assertIn("wait for the ECB", page)
        self.assertNotIn("/accept/", page)

    def test_accepting_a_real_move_replaces_the_reference_and_is_audited(self):
        leg = self.jump()
        self.sign_in()
        url = f"/fx/{leg.pk}/accept/"
        self.assertEqual(self.client.post(url, {"note": ""}).status_code, 409)
        self.assertEqual(Decimal(rails_models.FxLegRate.objects.get(pk=leg.pk).rate), Decimal("24.353"))
        r = self.client.post(url, {"note": "ECB really published 30.00 after the devaluation"})
        self.assertEqual(r.status_code, 302)
        row = rails_models.FxLegRate.objects.get(pk=leg.pk)
        self.assertEqual((Decimal(row.rate), row.pending_rate), (Decimal("30.00"), ""))
        entry = AuditEntry.objects.get(action="fx_leg.accept")
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertEqual(entry.detail, {"base": "EUR", "quote": "CZK", "old_rate": "24.353", "new_rate": "30.0",
                                        "source_date": row.source_date.isoformat(),
                                        "note": "ECB really published 30.00 after the devaluation"})
        self.assertIsNone(WatcherHealth.objects.get(name="fx:EUR:CZK").failing_since)
        self.assertEqual(self.price(Prices(ecb="30.00")).rate, Decimal("69583") * Decimal("30.00"))
        self.assertEqual(self.client.post(url, {"note": "again"}).status_code, 409)  # nothing waits now
        self.assertEqual(AuditEntry.objects.filter(action="fx_leg.accept").count(), 1)

    def test_accepting_needs_a_verified_staff_post_with_csrf(self):
        from django.test import Client

        leg = self.jump()
        url = f"/fx/{leg.pk}/accept/"
        anonymous = self.client.post(url, {"note": "n"})
        self.assertTrue(anonymous["Location"].startswith("/login/"))
        self.client.force_login(self.user)  # a password but no TOTP
        self.assertTrue(self.client.post(url, {"note": "n"})["Location"].startswith("/verify/"))
        self.sign_in()
        self.assertEqual(self.client.get(url).status_code, 405)  # a GET does nothing
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        self.assertEqual(strict.post(url, {"note": "n"}).status_code, 403)
        row = rails_models.FxLegRate.objects.get(pk=leg.pk)
        self.assertEqual((Decimal(row.rate), Decimal(row.pending_rate)), (Decimal("24.353"), Decimal("30.00")))
        self.assertFalse(AuditEntry.objects.filter(action="fx_leg.accept").exists())
        self.assertIsNotNone(WatcherHealth.objects.get(name="fx:EUR:CZK").failing_since)
