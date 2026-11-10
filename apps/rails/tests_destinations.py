"""Server-side requests to staff-chosen rail URLs (audit SEC-4): the webhook resolver's private-address
guard, and a node on this machine only on LOCAL_LIGHTNING_PORT."""
import socket
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.rails import http
from apps.rails.gateways import btcpay
from apps.rails.gateways.lightning.cln import ClnBackend
from apps.rails.gateways.lightning.lnbits import LnbitsBackend
from apps.rails.gateways.lightning.lnd import LndBackend

PUBLIC = "93.184.216.34"


def resolving(*addresses):
    infos = [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443))
             for a in addresses]
    return mock.patch("socket.getaddrinfo", return_value=infos)


def connecting():
    """The socket the guarded connection would open, refused, so nothing leaves the machine."""
    return mock.patch("urllib3.util.connection.create_connection", side_effect=ConnectionRefusedError("refused"))


@override_settings(RAILS_HTTP_ENABLED=True, LOCAL_LIGHTNING_PORT=None)
class GuardedRequestTests(SimpleTestCase):
    def call(self, url, guard=http.STAFF_URL):
        return http.get_json(url, headers={"Authorization": "token SECRET"}, service="btcpay", guard=guard)

    def refused(self, url, guard=http.STAFF_URL):
        with connecting() as connect, self.assertRaises(http.Unavailable) as cm:
            self.call(url, guard)
        connect.assert_not_called()
        return cm.exception.kind

    def test_private_loopback_link_local_and_reserved_addresses_are_refused_before_any_socket(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.2", "169.254.169.254", "::1", "fd00::1", "0.0.0.0",
                        "100.64.0.1"):
            with self.subTest(address), resolving(address):
                self.assertEqual(self.refused("https://btcpay.example/api"), "private_address")

    def test_one_private_address_among_public_ones_is_refused(self):
        with resolving(PUBLIC, "127.0.0.1"):
            self.assertEqual(self.refused("https://btcpay.example/api"), "private_address")

    def test_a_literal_private_address_is_refused(self):
        self.assertEqual(self.refused("https://127.0.0.1:8080/"), "private_address")
        self.assertEqual(self.refused("https://[::1]/"), "private_address")

    def test_a_public_address_is_connected_to_as_checked(self):
        with resolving(PUBLIC), connecting() as connect, self.assertRaises(http.Unavailable) as cm:
            self.call("https://btcpay.example/api")
        self.assertEqual(cm.exception.kind, "connection")
        self.assertEqual(connect.call_args.args[0], (PUBLIC, 443))  # the address just checked, never a new lookup

    def test_the_connection_checks_what_the_name_answers_then(self):
        """DNS rebinding: the name answers a public address when the URL is saved, loopback when the worker
        calls it. The call is refused and no socket is ever opened to loopback."""
        answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))],
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
        with mock.patch("socket.getaddrinfo", side_effect=lambda *a, **k: next(answers)) as lookup, \
                connecting() as connect:
            self.assertIsNone(http.destination_problem("https://btcpay.example/api", http.STAFF_URL))  # saved
            with self.assertRaises(http.Unavailable) as cm:
                self.call("https://btcpay.example/api")  # the worker's call looks the name up again
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(cm.exception.kind, "private_address")
        connect.assert_not_called()

    def test_a_second_call_is_checked_again(self):
        """Each call resolves and checks afresh: a name that turns to loopback between two calls is refused
        on the second, which never opens a socket there."""
        answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))],
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
        with mock.patch("socket.getaddrinfo", side_effect=lambda *a, **k: next(answers)), connecting() as connect:
            with self.assertRaises(http.Unavailable) as first:
                self.call("https://btcpay.example/api")
            with self.assertRaises(http.Unavailable) as second:
                self.call("https://btcpay.example/api")
        self.assertEqual((first.exception.kind, second.exception.kind), ("connection", "private_address"))
        self.assertEqual([c.args[0] for c in connect.call_args_list], [(PUBLIC, 443)])

    def test_an_unresolvable_name_is_unavailable(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such name")), connecting() as connect:
            with self.assertRaises(http.Unavailable) as cm:
                self.call("https://nowhere.example/")
        self.assertEqual(cm.exception.kind, "unresolvable")
        connect.assert_not_called()

    def test_plain_http_is_refused_for_a_staff_url(self):
        with resolving(PUBLIC):
            self.assertEqual(self.refused("http://btcpay.example/"), "scheme")

    def test_ambiguous_urls_are_refused(self):
        for url in ("https://evil.example\\@btcpay.example/", "https://user:pw@btcpay.example/", "https://x.example/a b"):
            with self.subTest(url), resolving(PUBLIC):
                self.assertEqual(self.refused(url), "bad_url")

    def test_the_log_names_the_kind_and_never_the_url(self):
        with resolving("127.0.0.1"), self.assertLogs("apps.rails.http", "WARNING") as logs:
            self.refused("https://secret-host.example/api?key=SECRET")
        text = "\n".join(logs.output)
        self.assertIn("private_address", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("SECRET", text)

    def test_calls_without_a_guard_are_unchanged(self):
        with mock.patch("apps.rails.http.requests.get") as get:
            get.return_value = mock.Mock(status_code=200, **{"json.return_value": {"ok": 1}})
            self.assertEqual(http.get_json("https://rpc.example/", service="evm"), {"ok": 1})
        get.assert_called_once()


@override_settings(RAILS_HTTP_ENABLED=True)
class LocalNodeTests(SimpleTestCase):
    def call(self, url, guard=http.LOCAL_NODE):
        return http.get_json(url, service="lightning", guard=guard)

    def attempt(self, url, guard=http.LOCAL_NODE):
        with connecting() as connect, self.assertRaises(http.Unavailable) as cm:
            self.call(url, guard)
        return cm.exception.kind, connect

    @override_settings(LOCAL_LIGHTNING_PORT=None)
    def test_no_port_configured_means_no_loopback_at_all(self):
        for url in ("http://127.0.0.1:8080/", "https://127.0.0.1:8080/", "http://localhost:10009/"):
            with self.subTest(url), resolving("127.0.0.1"):
                kind, connect = self.attempt(url)
                self.assertIn(kind, ("scheme", "private_address"))
                connect.assert_not_called()

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_the_configured_port_on_loopback_is_allowed(self):
        for url in ("http://127.0.0.1:10009/", "https://127.0.0.1:10009/", "http://[::1]:10009/"):
            with self.subTest(url):
                kind, connect = self.attempt(url)
                self.assertEqual(kind, "connection")
                self.assertEqual(connect.call_args.args[0][1], 10009)
        with resolving("127.0.0.1"):
            kind, connect = self.attempt("http://localhost:10009/v1/invoices")
        self.assertEqual((kind, connect.call_args.args[0]), ("connection", ("127.0.0.1", 10009)))

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_any_other_loopback_port_is_refused(self):
        for url in ("http://127.0.0.1:8080/", "http://127.0.0.1/", "https://127.0.0.1:3001/", "http://127.0.0.1:7233/"):
            with self.subTest(url):
                kind, connect = self.attempt(url)
                self.assertIn(kind, ("scheme", "private_address"))
                connect.assert_not_called()

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_plain_http_to_a_public_host_is_refused_even_on_that_port(self):
        with resolving(PUBLIC):
            kind, connect = self.attempt("http://lnd.example:10009/")
        self.assertEqual(kind, "scheme")
        connect.assert_not_called()

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_other_private_addresses_stay_refused_on_that_port(self):
        with resolving("10.0.0.5"):
            kind, connect = self.attempt("https://lnd.lan:10009/")
        self.assertEqual(kind, "private_address")
        connect.assert_not_called()

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_a_staff_url_never_reaches_loopback(self):
        kind, connect = self.attempt("https://127.0.0.1:10009/", guard=http.STAFF_URL)
        self.assertEqual(kind, "private_address")
        connect.assert_not_called()


class GatewaysUseTheGuardTests(SimpleTestCase):
    """Every gateway whose URL staff typed asks for the guard: BTCPay and LNbits for public addresses only,
    LND and CLN for the local node as well."""

    def guards(self, call):
        seen = []

        def record(*args, **kwargs):
            seen.append(kwargs.get("guard"))
            raise http.Unavailable("x", "test")

        with mock.patch("apps.rails.http.get_json", side_effect=record), \
                mock.patch("apps.rails.http.post_json", side_effect=record), \
                mock.patch("apps.rails.http.delete", side_effect=record):
            for fn in call:
                try:
                    fn()
                except http.Unavailable:
                    pass
        return seen

    def test_lightning_backends(self):
        lnd, cln, lnbits = LndBackend("http://127.0.0.1:10009", "mac"), ClnBackend("https://cln.example", "rune"), \
            LnbitsBackend("https://lnbits.example", "key")
        self.assertEqual(set(self.guards([lambda: lnd.create_invoice(1000, "x", 60), lambda: lnd.invoice_state("ab")])),
                         {http.LOCAL_NODE})
        self.assertEqual(set(self.guards([lambda: cln.create_invoice(1000, "x", 60), lambda: cln.invoice_state("ab")])),
                         {http.LOCAL_NODE})
        self.assertEqual(set(self.guards([lambda: lnbits.create_invoice(1000, "x", 60),
                                          lambda: lnbits.invoice_state("ab")])), {http.STAFF_URL})

    def test_btcpay(self):
        self.assertEqual(self.guards([lambda: btcpay.key_permissions("https://btcpay.example", "k")]), [http.STAFF_URL])


@override_settings(LOCAL_LIGHTNING_PORT=None)
class RailFormUrlTests(SimpleTestCase):
    """The rail form checks a URL as it is saved, the way the calls will: `url_problem`."""

    def problem(self, url, *, loopback_ok=False):
        from apps.staff.forms_settings import url_problem

        return url_problem(url, loopback_ok=loopback_ok)

    def test_public_https_is_fine(self):
        with resolving(PUBLIC):
            self.assertEqual(self.problem("https://btcpay.example/base"), "")
            self.assertEqual(self.problem("https://lnd.example:8080", loopback_ok=True), "")

    def test_private_names_and_addresses_are_refused_with_the_reason(self):
        with resolving("10.1.2.3"):
            self.assertIn("private, loopback or reserved", self.problem("https://btcpay.lan/"))
        self.assertIn("private, loopback or reserved", self.problem("https://127.0.0.1:3001/"))
        self.assertIn("private, loopback or reserved", self.problem("https://169.254.169.254/"))

    def test_an_unresolvable_name_is_refused(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such name")):
            self.assertEqual(self.problem("https://nowhere.example/"), "The host name could not be resolved.")

    def test_without_the_setting_no_node_on_this_machine(self):
        for url in ("http://127.0.0.1:8080", "https://127.0.0.1:8080", "http://localhost:10009/"):
            with self.subTest(url), resolving("127.0.0.1"):
                problem = self.problem(url, loopback_ok=True)
                self.assertTrue(problem)
        with resolving("127.0.0.1"):
            self.assertIn("LOCAL_LIGHTNING_PORT", self.problem("https://localhost:10009", loopback_ok=True))

    @override_settings(LOCAL_LIGHTNING_PORT=10009)
    def test_with_the_setting_only_its_port_and_only_for_lnd_and_cln(self):
        self.assertEqual(self.problem("http://127.0.0.1:10009", loopback_ok=True), "")
        self.assertEqual(self.problem("https://[::1]:10009/", loopback_ok=True), "")
        with resolving("127.0.0.1"):
            self.assertEqual(self.problem("http://localhost:10009/", loopback_ok=True), "")
        self.assertIn("port 10009", self.problem("http://127.0.0.1:8080", loopback_ok=True))
        self.assertIn("port 10009", self.problem("https://127.0.0.1:8080", loopback_ok=True))
        self.assertIn("private, loopback or reserved", self.problem("https://127.0.0.1:10009"))  # BTCPay, LNbits
        self.assertTrue(self.problem("http://127.0.0.1:10009"))


class RailLookupGateTests(SimpleTestCase):
    """The rail guard resolves through its own gate (review S5, M-3): `WEBHOOK_DNS_ENABLED`, which pauses
    webhook lookups, never reaches a BTCPay, LND, CLN or LNbits call."""

    @override_settings(RAILS_HTTP_ENABLED=True, RAILS_DNS_ENABLED=True, WEBHOOK_DNS_ENABLED=False)
    def test_the_webhook_switch_does_not_reach_the_rails(self):
        from apps.webhooks import delivery

        with resolving(PUBLIC) as lookup, mock.patch("apps.webhooks.delivery._SYSTEM_RESOLVER", lookup):
            with self.assertRaises(RuntimeError):
                delivery.check_destination("https://hooks.example.com/")  # the webhook gate is shut
            with connecting() as connect, self.assertRaises(http.Unavailable) as cm:
                http.get_json("https://btcpay.example/api", service="btcpay", guard=http.STAFF_URL)
        self.assertEqual(cm.exception.kind, "connection")  # the rails looked the name up and connected
        connect.assert_called_once()

    @override_settings(RAILS_HTTP_ENABLED=True)
    def test_under_the_test_runner_an_unmocked_lookup_fails_loudly(self):
        with self.assertRaises(RuntimeError) as cm:
            http.get_json("https://btcpay.example/api", service="btcpay", guard=http.STAFF_URL)
        self.assertIn("RAILS_DNS_ENABLED", str(cm.exception))

    def test_the_gate_is_open_outside_the_test_runner_and_no_environment_closes_it(self):
        from pathlib import Path

        base = (Path(__file__).resolve().parents[2] / "config" / "settings" / "base.py").read_text()
        self.assertTrue("\nRAILS_DNS_ENABLED = True" in base, "base settings must open the rail lookup gate")
        self.assertFalse('env.bool("RAILS_DNS_ENABLED"' in base, "no environment may close it")
