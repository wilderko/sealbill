from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase
from datetime import timedelta

from django.utils import timezone

from apps.orgs.models import Organization
from apps.rails import health
from apps.rails.models import RailAccount, WatchCursor, WatcherHealth
from apps.vault.registry import encrypted_field_names
from apps.vault.testing import UnsealedVaultMixin

TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def make_org():
    return Organization.objects.create(slug="lt", legal_name="Liberation Travel s.r.o.")


class RailAccountTests(UnsealedVaultMixin, TestCase):
    def test_rail_account_credentials_encrypted_and_clean_rules(self):
        org = make_org()
        self.assertIn("credentials", encrypted_field_names(RailAccount))
        acct = RailAccount.objects.create(
            org=org, kind="btcpay", label="BTCPay", assets=["BTC", "XMR"],
            config={"url": "https://btcpay.example", "store_id": "s1", "speed_policy": "MediumSpeed"},
            credentials={"api_key": "k-SECRET", "webhook_secret": "w"},
        )
        acct.refresh_from_db()
        self.assertEqual(acct.credentials["api_key"], "k-SECRET")
        with connection.cursor() as c:
            c.execute("select credentials from rails_railaccount")
            self.assertNotIn("k-SECRET", c.fetchone()[0])
        self.assertEqual(acct.method_key, f"rail:{acct.pk}")
        self.assertEqual(acct.address, "")
        acct.full_clean()

        def bad(**kw):
            base = dict(org=org, kind="evm", label="x", address="0x" + "AB" * 20,
                        networks=["ethereum"], assets=["USDC"])
            base.update(kw)
            with self.assertRaises(ValidationError):
                RailAccount(**base).full_clean()

        ok = RailAccount(org=org, kind="evm", label="x", address="0x" + "AB" * 20,
                         networks=["ethereum", "base"], assets=["USDC"])
        ok.full_clean()
        self.assertEqual(ok.address, "0x" + "ab" * 20)
        ok.save()
        self.assertEqual(RailAccount.objects.get(pk=ok.pk).address, "0x" + "ab" * 20)
        bad(address="0x12")
        bad(networks=["tron"])
        bad(networks=[])
        bad(assets=["DAI"])
        bad(assets=[])
        bad(networks=["base"], assets=["USDT"])  # base has no USDT
        bad(kind="btcpay", address="", networks=[], assets=["DOGE"], config={})
        bad(kind="btcpay", address="", networks=["ethereum"], assets=["BTC"],
            config={"url": "u", "store_id": "s"})
        bad(kind="lightning", address="", networks=[], assets=["XMR"], config={"backend": "nwc"})
        bad(kind="lightning", address="", networks=[], assets=["BTC"], config={"backend": "foo"})
        bad(kind="tron", address=TRON, networks=["ethereum"], assets=["USDT"])
        bad(kind="tron", address=TRON, networks=["tron"], assets=["USDC"])
        RailAccount(org=org, kind="tron", label="t", address=TRON, networks=["tron"], assets=["USDT"]).full_clean()
        RailAccount(org=org, kind="lightning", label="l", assets=["BTC"], networks=[],
                    config={"backend": "nwc", "url": ""}).full_clean()

    def test_unique_cursor(self):
        WatchCursor.objects.create(network="ethereum", address="a", stream="erc20", position="1")
        with self.assertRaises(IntegrityError):
            WatchCursor.objects.create(network="ethereum", address="a", stream="erc20", position="2")


class HealthTests(TestCase):
    def test_health_ok_clears_failing_since(self):
        health.failed("scan:arbitrum:3", "timeout")
        health.failed("scan:arbitrum:3", "timeout")
        h = WatcherHealth.objects.get(name="scan:arbitrum:3")
        self.assertEqual(h.consecutive_failures, 2)
        self.assertIsNotNone(h.failing_since)
        first = h.failing_since
        health.failed("scan:arbitrum:3", "5xx")
        self.assertEqual(WatcherHealth.objects.get(name="scan:arbitrum:3").failing_since, first)
        WatcherHealth.objects.filter(pk=h.pk).update(alerted_at=timezone.now())
        health.ok("scan:arbitrum:3")
        h.refresh_from_db()
        self.assertIsNone(h.failing_since)
        self.assertIsNone(h.alerted_at)
        self.assertEqual(h.consecutive_failures, 0)
        self.assertIsNotNone(h.last_ok_at)

    def test_failing_filters_by_age(self):
        health.failed("a", "timeout")
        self.assertEqual(health.failing(min_age_s=3600).count(), 0)
        WatcherHealth.objects.update(failing_since=timezone.now() - timedelta(hours=2))
        self.assertEqual(list(health.failing(min_age_s=3600).values_list("name", flat=True)), ["a"])
