"""The credentials service shared by the command and the staff pages, and BTCPay webhook registration."""
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from django.test import TestCase, override_settings

from apps.audit.models import AuditEntry
from apps.orgs.models import Organization
from apps.rails import http
from apps.rails.credentials import CredentialRefused, register_btcpay_webhook, set_credentials
from apps.rails.gateways.lightning import nwc
from apps.rails.models import RailAccount
from apps.rails.tests_audit_gateways import macaroon
from apps.vault.testing import UnsealedVaultMixin

NWC = "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example&secret=" + "cd" * 32
RECEIVE = {"make_invoice", "lookup_invoice", "get_info"}
INVOICES_ONLY = {"permissions": ["btcpay.store.canviewinvoices:S1", "btcpay.store.cancreateinvoice:S1"]}
BROAD = {"permissions": ["btcpay.store.canviewinvoices:S1", "btcpay.store.canmodifystoresettings:S1"]}


class Base(UnsealedVaultMixin, TestCase):
    def setUp(self):
        super().setUp()
        public = [(2, 1, 6, "", ("93.184.216.34", 443))]  # an NWC credential's relays resolve to public addresses
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=public))
        self.org = Organization.objects.create(slug="eu", legal_name="Seller s.r.o.")
        self.btcpay = RailAccount.objects.create(org=self.org, kind="btcpay", label="B", assets=["BTC"],
                                                 config={"url": "https://btcpay.example", "store_id": "S1"})
        self.ln = RailAccount.objects.create(org=self.org, kind="lightning", label="LN", assets=["BTC"],
                                             config={"backend": "nwc"})


class SetCredentialsTests(Base):
    def test_spending_nwc_refused(self):
        with mock.patch.object(nwc, "capabilities", return_value=RECEIVE | {"pay_invoice"}):
            with self.assertRaises(CredentialRefused) as cm:
                set_credentials(self.ln, {"nwc_url": NWC})
        self.assertIn("pay_invoice", cm.exception.message)
        self.assertNotIn("cd" * 32, cm.exception.message)
        self.assertIsNone(RailAccount.objects.get(pk=self.ln.pk).credentials)

    def test_broad_btcpay_key_refused(self):
        with mock.patch("apps.rails.http.get_json", return_value=BROAD):
            with self.assertRaises(CredentialRefused):
                set_credentials(self.btcpay, {"api_key": "K"})
        self.assertIsNone(RailAccount.objects.get(pk=self.btcpay.pk).credentials)

    def test_lnd_macaroon_beyond_invoices_refused(self):
        lnd = RailAccount.objects.create(org=self.org, kind="lightning", label="L2", assets=["BTC"],
                                         config={"backend": "lnd", "url": "https://lnd.example"})
        with self.assertRaises(CredentialRefused):
            set_credentials(lnd, {"macaroon": macaroon([("offchain", ["write"])])})
        self.assertIsNone(RailAccount.objects.get(pk=lnd.pk).credentials)

    def test_lnbits_vouch_recorded_and_required(self):
        lb = RailAccount.objects.create(org=self.org, kind="lightning", label="L3", assets=["BTC"],
                                        config={"backend": "lnbits", "url": "https://lnbits.example"})
        with self.assertRaises(CredentialRefused):
            set_credentials(lb, {"invoice_key": "IK"})
        self.assertEqual(set_credentials(lb, {"invoice_key": "IK"}, lnbits_vouched=True), ["invoice_key"])
        entry = AuditEntry.objects.get(action="rail_account.credentials")
        self.assertTrue(entry.detail["lnbits_key_is_invoice_key"])

    def test_empty_value_keeps_stored_and_audit_has_names_only(self):
        with mock.patch("apps.rails.http.get_json", return_value=INVOICES_ONLY):
            set_credentials(self.btcpay, {"api_key": "KEY-1", "webhook_secret": "WS-1"})
            self.assertEqual(set_credentials(self.btcpay, {"api_key": "", "webhook_secret": "WS-2"}), ["webhook_secret"])
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials, {"api_key": "KEY-1", "webhook_secret": "WS-2"})
        for e in AuditEntry.objects.filter(action="rail_account.credentials"):
            self.assertNotIn("KEY-1", str(e.detail))
            self.assertNotIn("WS-2", str(e.detail))

    def test_actor_is_recorded(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        with mock.patch.object(nwc, "capabilities", return_value=RECEIVE):
            set_credentials(self.ln, {"nwc_url": NWC}, actor=user)
        self.assertEqual(AuditEntry.objects.get(action="rail_account.credentials").actor, user)


@override_settings(SITE_URL="https://pay.example/")
class WebhookTests(Base):
    HOOK = "https://pay.example/hooks/btcpay/{}/"

    def setUp(self):
        super().setUp()
        self.btcpay.credentials = {"api_key": "KEY-1"}
        self.btcpay.save(update_fields=["credentials"])

    def test_registers_once_and_stores_secret(self):
        with mock.patch("apps.rails.http.post_json", return_value={"id": "WH1"}) as post, \
                mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, [{"id": "x", "url": "https://other/"}]]):
            hook_id = register_btcpay_webhook(self.btcpay, actor=None)
        self.assertEqual(hook_id, "WH1")
        self.assertEqual(post.call_count, 1)
        url, body = post.call_args.args
        self.assertEqual(url, "https://btcpay.example/api/v1/stores/S1/webhooks")
        self.assertEqual(body["url"], self.HOOK.format(self.btcpay.pk))
        self.assertEqual(body["authorizedEvents"], {"everything": True})
        self.assertTrue(body["enabled"] and body["automaticRedelivery"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "token KEY-1")
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials["webhook_secret"], body["secret"])
        self.assertGreaterEqual(len(body["secret"]), 32)

    def test_already_registered_refused_without_post(self):
        listing = [{"id": "WH0", "url": self.HOOK.format(self.btcpay.pk)}]
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, listing]), \
                mock.patch("apps.rails.http.post_json") as post:
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        post.assert_not_called()
        self.assertIn("delete", cm.exception.message)
        self.btcpay.refresh_from_db()
        self.assertNotIn("webhook_secret", self.btcpay.credentials)

    def test_btcpay_down_refused(self):
        with mock.patch("apps.rails.http.get_json", side_effect=http.Unavailable("btcpay", "timeout")):
            with self.assertRaises(CredentialRefused):
                register_btcpay_webhook(self.btcpay, actor=None)


@override_settings(SITE_URL="https://pay.example/")
class WebhookFailureTests(Base):
    def setUp(self):
        super().setUp()
        self.btcpay.credentials = {"api_key": "KEY-1"}
        self.btcpay.save(update_fields=["credentials"])
        self.hook = f"https://pay.example/hooks/btcpay/{self.btcpay.pk}/"

    def gets(self, *answers):
        return mock.patch("apps.rails.http.get_json", side_effect=list(answers))

    def test_broad_key_never_writes(self):
        with self.gets(BROAD), mock.patch("apps.rails.http.post_json") as post:
            with self.assertRaises(CredentialRefused):
                register_btcpay_webhook(self.btcpay, actor=None)
        post.assert_not_called()

    def test_storing_fails_hook_is_deleted(self):
        with self.gets(INVOICES_ONLY, []), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH9"}), \
                mock.patch("apps.rails.http.delete") as delete, \
                mock.patch("apps.rails.credentials._store_credentials", side_effect=CredentialRefused("no")):
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        delete.assert_called_once()
        self.assertTrue(delete.call_args.args[0].endswith("/webhooks/WH9"))
        self.assertNotIn("nothing was registered", cm.exception.message.replace("Nothing is registered", ""))

    def test_storing_and_delete_both_fail_names_the_id(self):
        with self.gets(INVOICES_ONLY, []), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH9"}), \
                mock.patch("apps.rails.http.delete", side_effect=http.Unavailable("btcpay", "timeout")), \
                mock.patch("apps.rails.credentials._store_credentials", side_effect=CredentialRefused("no")):
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        self.assertIn("WH9", cm.exception.message)
        self.assertIn("Delete it in BTCPay", cm.exception.message)
        self.assertNotIn("nothing was registered", cm.exception.message.lower())

    def test_unknown_post_outcome_relists_and_stores(self):
        relisted = [{"id": "WH7", "url": self.hook}]
        with self.gets(INVOICES_ONLY, [], relisted), \
                mock.patch("apps.rails.http.post_json", side_effect=http.Unavailable("btcpay", "timeout")):
            self.assertEqual(register_btcpay_webhook(self.btcpay, actor=None), "WH7")
        self.btcpay.refresh_from_db()
        self.assertIn("webhook_secret", self.btcpay.credentials)

    def test_unknown_outcome_and_relist_down_names_the_url_not_nothing(self):
        with self.gets(INVOICES_ONLY, [], http.Unavailable("btcpay", "timeout")), \
                mock.patch("apps.rails.http.post_json", side_effect=http.Unavailable("btcpay", "timeout")):
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        self.assertIn(self.hook, cm.exception.message)
        self.assertNotIn("nothing was registered", cm.exception.message.lower())

    def test_other_spelling_of_the_url_is_detected(self):
        listing = [{"id": "W", "url": f"HTTPS://Pay.Example/hooks/btcpay/{self.btcpay.pk}"}]
        with self.gets(INVOICES_ONLY, listing), mock.patch("apps.rails.http.post_json") as post:
            with self.assertRaises(CredentialRefused):
                register_btcpay_webhook(self.btcpay, actor=None)
        post.assert_not_called()

    def test_webhook_id_audited_without_secret(self):
        with self.gets(INVOICES_ONLY, []), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH1"}) as post:
            register_btcpay_webhook(self.btcpay, actor=None)
        entry = AuditEntry.objects.get(action="rail_account.btcpay_webhook")
        self.assertEqual(entry.detail, {"rail_account": self.btcpay.pk, "webhook_id": "WH1"})
        self.assertNotIn(post.call_args.args[1]["secret"], str(entry.detail))


@override_settings(SITE_URL="https://pay.example/")
class LockTests(Base):
    def setUp(self):
        super().setUp()
        self.btcpay.credentials = {"api_key": "KEY-1"}
        self.btcpay.save(update_fields=["credentials"])

    def run_both(self):
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "W"}):
            set_credentials(self.btcpay, {"webhook_secret": "WS"})
            register_btcpay_webhook(self.btcpay, actor=None)

    def test_both_operations_take_a_no_key_lock_inside_a_transaction(self):
        if connection.vendor == "postgresql":
            self.skipTest("the SQLite spy test applies; the SQL test is PostgreSQL-only")
        real = RailAccount.objects.select_for_update
        seen = []

        def spy(*a, **kw):
            seen.append((connection.in_atomic_block, kw.get("no_key")))
            return real(*a, **kw)
        with mock.patch.object(RailAccount.objects, "select_for_update", side_effect=spy):
            self.run_both()
        self.assertEqual(seen, [(True, True), (True, True)])

    def test_lock_sql_on_postgres(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL only")
        with CaptureQueriesContext(connection) as ctx:
            self.run_both()
        locks = [q["sql"] for q in ctx.captured_queries if "FOR " in q["sql"] and "UPDATE" in q["sql"]]
        self.assertEqual(len(locks), 2)
        self.assertTrue(all("FOR NO KEY UPDATE" in q for q in locks))

    def test_null_id_is_stored_as_empty(self):
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", return_value={"id": None}):
            self.assertEqual(register_btcpay_webhook(self.btcpay, actor=None), "")
        self.assertEqual(AuditEntry.objects.get(action="rail_account.btcpay_webhook").detail["webhook_id"], "")

    def test_key_checked_once_in_registration(self):
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]) as get, \
                mock.patch("apps.rails.http.post_json", return_value={"id": "W"}):
            register_btcpay_webhook(self.btcpay, actor=None)
        self.assertEqual(get.call_count, 2)  # permissions once, one listing


@override_settings(SITE_URL="https://pay.example/")
class WebhookEdgeTests(Base):
    def setUp(self):
        super().setUp()
        self.btcpay.credentials = {"api_key": "KEY-1"}
        self.btcpay.save(update_fields=["credentials"])
        self.hook = f"https://pay.example/hooks/btcpay/{self.btcpay.pk}/"

    def test_none_listed_yet_wording_no_delete_nothing_stored(self):
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, [], []]), \
                mock.patch("apps.rails.http.post_json", side_effect=http.Unavailable("btcpay", "timeout")), \
                mock.patch("apps.rails.http.delete") as delete:
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        self.assertEqual(cm.exception.message,
                         f"BTCPay did not confirm the webhook and none is listed yet; if a webhook for {self.hook} "
                         f"appears in BTCPay, delete it there before trying again.")
        delete.assert_not_called()
        self.btcpay.refresh_from_db()
        self.assertEqual(self.btcpay.credentials, {"api_key": "KEY-1"})

    def test_failure_after_the_post_removes_the_hook(self):
        # raised by the audit write, i.e. inside the transaction, after the hook exists; the same exit
        # as a failure at commit
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", return_value={"id": "WH5"}), \
                mock.patch("apps.rails.http.delete") as delete, \
                mock.patch("apps.rails.credentials.audit.record", side_effect=RuntimeError("db gone")):
            with self.assertRaises(CredentialRefused):
                register_btcpay_webhook(self.btcpay, actor=None)
        self.assertTrue(delete.call_args.args[0].endswith("/webhooks/WH5"))
        self.btcpay.refresh_from_db()
        self.assertNotIn("webhook_secret", self.btcpay.credentials)

    def test_failure_at_commit_names_the_hook_when_delete_fails(self):
        from django.db import transaction
        real = transaction.Atomic.__exit__
        state = {"posted": False, "failed": False}

        def post(*a, **kw):
            state["posted"] = True
            return {"id": "WH6"}

        def exit_fail(self_, *exc):
            result = real(self_, *exc)
            # the registration's own block, left cleanly after the POST: stands in for the commit
            if state["posted"] and not state["failed"] and exc[0] is None:
                state["failed"] = True
                raise RuntimeError("commit failed")
            return result
        with mock.patch("apps.rails.http.get_json", side_effect=[INVOICES_ONLY, []]), \
                mock.patch("apps.rails.http.post_json", side_effect=post), \
                mock.patch("apps.rails.http.delete", side_effect=http.Unavailable("btcpay", "timeout")), \
                mock.patch.object(transaction.Atomic, "__exit__", exit_fail):
            with self.assertRaises(CredentialRefused) as cm:
                register_btcpay_webhook(self.btcpay, actor=None)
        self.assertIn("WH6", cm.exception.message)
