"""An NWC credential's relays (review S5, M-2): nostr-sdk opens its own websocket to every `relay=` the
connection names, outside the http guard, so each must be wss:// on a public host when it is saved."""
import socket
from unittest import mock

from apps.rails.credentials import CredentialRefused, set_credentials
from apps.rails.gateways.lightning import nwc
from apps.rails.models import RailAccount
from apps.rails.tests_credentials_service import RECEIVE, Base

KEY, SECRET = "ab" * 32, "cd" * 32


def nwc_url(*relays):
    query = "&".join(f"relay={r}" for r in relays)
    return f"nostr+walletconnect://{KEY}?{query}{'&' if query else ''}secret={SECRET}"


def resolving(table):
    """`socket.getaddrinfo` answering each host from `table`."""
    def answer(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
    return mock.patch("socket.getaddrinfo", side_effect=answer)


ALBY = {"relay.getalby.com": "104.21.40.100", "relay2.getalby.com": "172.67.152.100"}


class NwcRelayTests(Base):
    def save(self, url):
        with mock.patch.object(nwc, "capabilities", return_value=RECEIVE) as asked:
            set_credentials(self.ln, {"nwc_url": url})
        return asked

    def refused(self, url, table=ALBY):
        with resolving(table), mock.patch.object(nwc, "capabilities", return_value=RECEIVE) as asked:
            with self.assertRaises(CredentialRefused) as cm:
                set_credentials(self.ln, {"nwc_url": url})
        asked.assert_not_called()  # the wallet is never asked over a relay that was refused
        self.assertIsNone(RailAccount.objects.get(pk=self.ln.pk).credentials)
        self.assertNotIn(SECRET, cm.exception.message)
        return cm.exception.message

    def test_the_production_relays_pass(self):
        for relays in (["wss://relay.getalby.com/v1"], ["wss://relay2.getalby.com/v1"],
                       ["wss://relay.getalby.com/v1", "wss://relay2.getalby.com/v1"]):
            with self.subTest(relays), resolving(ALBY):
                self.save(nwc_url(*relays)).assert_called_once()
            self.assertTrue(RailAccount.objects.get(pk=self.ln.pk).credentials)
            RailAccount.objects.filter(pk=self.ln.pk).update(credentials=None)

    def test_a_relay_that_is_not_wss_is_refused(self):
        for relay in ("ws://relay.getalby.com/v1", "https://relay.getalby.com/v1", "ws://127.0.0.1:8080"):
            with self.subTest(relay):
                self.assertIn("wss://", self.refused(nwc_url(relay)))

    def test_a_relay_on_this_machine_or_a_private_network_is_refused(self):
        for relay, table in (("wss://127.0.0.1:8080", ALBY), ("wss://[::1]:3001", ALBY),
                             ("wss://relay.lan", {"relay.lan": "10.0.0.5"}),
                             ("wss://localhost:7233", {"localhost": "127.0.0.1"})):
            with self.subTest(relay):
                self.assertIn("private", self.refused(nwc_url(relay), table))

    def test_one_bad_relay_among_good_ones_is_refused(self):
        self.refused(nwc_url("wss://relay.getalby.com/v1", "wss://127.0.0.1:8080"))

    def test_an_unresolvable_relay_or_none_at_all_is_refused(self):
        self.assertIn("could not be resolved", self.refused(nwc_url("wss://nowhere.example")))
        self.refused(nwc_url())

    def test_the_message_names_no_relay(self):
        message = self.refused(nwc_url("wss://relay.lan/secret-path"), {"relay.lan": "10.0.0.5"})
        self.assertNotIn("relay.lan", message)
