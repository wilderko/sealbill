import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.rails import fx
from apps.rails.http import Unavailable

FIX = Path(__file__).parent / "fixtures" / "fx"


def load(name):
    return json.loads((FIX / name).read_text())


def bitstamp(price):
    return {"timestamp": "1794260704", "open": str(price), "high": str(price), "low": str(price),
            "last": str(price), "volume": "1.0", "vwap": str(price), "bid": str(price), "ask": str(price)}


def coinbase(price):
    d = load("coinbase_btc_eur.json")
    d["data"]["amount"] = str(price)
    return d


def frank(rate):
    d = load("frankfurter_eur_czk.json")
    d["rates"] = {"CZK": rate}
    d["date"] = timezone.now().date().isoformat()  # an answer more than 5 days old is refused (MONEY-9)
    return d


class Fake:
    """Answers by URL; `prices` maps (asset, fiat) -> (bitstamp, coinbase)."""

    def __init__(self, prices, ecb=24.353):
        self.prices, self.ecb, self.urls = prices, ecb, []

    def __call__(self, url, *, params=None, headers=None, service, timeout=10):
        self.urls.append(url)
        if "bitstamp" in url:
            pair = url.rstrip("/").rsplit("/", 1)[1].upper()
            a, f = (pair[:4], pair[4:]) if pair[:4] in ("USDT", "USDC") else (pair[:3], pair[3:])
            return bitstamp(self.prices[(a, f)][0])
        if "coinbase" in url:
            a, f = url.split("/prices/")[1].split("/")[0].split("-")
            return coinbase(self.prices[(a, f)][1])
        return frank(self.ecb)


class FxTests(TestCase):  # the fiat leg is checked against the last stored one (MONEY-9)
    def setUp(self):
        cache.clear()

    def run_with(self, fake, asset, cur):
        with mock.patch("apps.rails.fx.http.get_json", side_effect=fake):
            return fx.price_in(asset, cur)

    def test_two_sources_agree_returns_primary(self):
        p = self.run_with(Fake({("BTC", "EUR"): (69583, 69412.485)}), "BTC", "EUR")
        self.assertEqual(p.rate, Decimal("69583"))
        self.assertEqual(p.second, Decimal("69412.485"))
        self.assertEqual(p.source, "bitstamp+coinbase")

    def test_divergence_over_100_bps_refused(self):
        with self.assertRaises(fx.RatesDiverge) as cm:
            self.run_with(Fake({("BTC", "EUR"): (70000, 69000)}), "BTC", "EUR")
        self.assertEqual(cm.exception.bps, 143)

    def test_exactly_100_bps_passes(self):
        self.run_with(Fake({("BTC", "EUR"): (10000, 9900)}), "BTC", "EUR")

    def test_one_source_down_is_unavailable(self):
        fake = Fake({("BTC", "EUR"): (69583, 69412)})

        def down(url, **kw):
            if "coinbase" in url:
                raise Unavailable("coinbase", "timeout")
            return fake(url, **kw)

        with mock.patch("apps.rails.fx.http.get_json", side_effect=down):
            with self.assertRaises(fx.RatesUnavailable):
                fx.price_in("BTC", "EUR")

    def test_unparsable_is_unavailable(self):
        with mock.patch("apps.rails.fx.http.get_json", return_value={"nope": 1}):
            with self.assertRaises(fx.RatesUnavailable):
                fx.price_in("BTC", "EUR")

    def test_czk_goes_through_ecb(self):
        p = self.run_with(Fake({("BTC", "EUR"): (69583, 69412.485)}, ecb=24.353), "BTC", "CZK")
        self.assertEqual(p.rate, Decimal("69583") * Decimal("24.353"))
        self.assertEqual(p.second, Decimal("69412.485") * Decimal("24.353"))
        self.assertEqual(p.source, "bitstamp+coinbase×ecb")

    def test_btc_invoice_paid_in_usdt_pivots_via_usd(self):
        fake = Fake({("USDT", "USD"): (1, 1), ("BTC", "USD"): (80000, 80100)})
        p = self.run_with(fake, "USDT", "BTC")
        self.assertEqual(p.rate, Decimal(1) / Decimal(80000))
        self.assertEqual(p.second, Decimal(1) / Decimal(80100))
        self.assertTrue(any("USDTUSD" in u or "USDT-USD" in u for u in fake.urls))

    def test_chained_legs_compounding_past_the_bound_are_refused(self):
        # each leg is 80 bps apart, in opposite directions: composite is ~160 bps
        fake = Fake({("USDT", "USD"): (1.0000, 1.0080), ("BTC", "USD"): (80000, 80000 * 0.992)})
        with self.assertRaises(fx.RatesDiverge) as cm:
            self.run_with(fake, "USDT", "BTC")
        self.assertGreater(cm.exception.bps, 100)

    def test_same_asset_is_one_without_http(self):
        with mock.patch("apps.rails.fx.http.get_json") as g:
            p = fx.price_in("BTC", "BTC")
        g.assert_not_called()
        self.assertEqual((p.rate, p.second), (Decimal(1), Decimal(1)))

    def test_cached_for_60_seconds(self):
        fake = Fake({("BTC", "EUR"): (69583, 69412)})
        with mock.patch("apps.rails.fx.http.get_json", side_effect=fake):
            fx.price_in("BTC", "EUR")
            n = len(fake.urls)
            with mock.patch("apps.rails.fx.cache.set", wraps=fx.cache.set) as s:
                fx.price_in("BTC", "EUR")
            s.assert_not_called()
        self.assertEqual(len(fake.urls), n)
        self.assertEqual(n, 2)

    def test_cache_timeout_is_60(self):
        fake = Fake({("BTC", "EUR"): (69583, 69412)})
        with mock.patch("apps.rails.fx.http.get_json", side_effect=fake):
            with mock.patch("apps.rails.fx.cache.set", wraps=fx.cache.set) as s:
                fx.price_in("BTC", "EUR")
        self.assertEqual(s.call_args.kwargs.get("timeout", s.call_args.args[2:3] and s.call_args.args[2]), 60)

    def test_kraken_is_never_asked(self):
        """Owner 2026-11-09: rates come from Bitstamp and Coinbase; Kraken is no longer a source."""
        fake = Fake({("BTC", "EUR"): (69583, 69412)})
        with mock.patch("apps.rails.fx.http.get_json", side_effect=fake):
            fx.price_in("BTC", "EUR")
        self.assertTrue(any(u == "https://www.bitstamp.net/api/v2/ticker/btceur/" for u in fake.urls), fake.urls)
        self.assertFalse(any("kraken" in u for u in fake.urls))

    def test_bitstamp_down_is_unavailable(self):
        fake = Fake({("BTC", "EUR"): (69583, 69412)})

        def down(url, **kw):
            if "bitstamp" in url:
                raise Unavailable("bitstamp", "503")
            return fake(url, **kw)

        with mock.patch("apps.rails.fx.http.get_json", side_effect=down):
            with self.assertRaises(fx.RatesUnavailable):
                fx.price_in("BTC", "EUR")

    def test_only_eur_usd_gbp_are_direct(self):
        self.assertEqual(fx.DIRECT_FIAT, {"EUR", "USD", "GBP"})
