from django.test import SimpleTestCase

from apps.rails import addresses, networks

TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TON_USDT = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


class NetworkTests(SimpleTestCase):
    def test_bsc_stablecoins_have_18_decimals(self):
        for sym in ("USDT", "USDC"):
            self.assertEqual(networks.NETWORKS["bsc"].assets[sym].decimals, 18)

    def test_optimism_legacy_usdt_not_allowed(self):
        self.assertIsNone(networks.asset_by_contract("optimism", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"))
        self.assertEqual(
            networks.NETWORKS["optimism"].assets["USDT"].contract, "0x01bFF41798a0BcF287b996046Ca68b395DbC1071"
        )

    def test_asset_by_contract_case_insensitive_on_evm_only(self):
        usdc = networks.NETWORKS["ethereum"].assets["USDC"]
        self.assertEqual(networks.asset_by_contract("ethereum", usdc.contract.lower()), usdc)
        sol = networks.NETWORKS["solana"].assets["USDC"]
        self.assertEqual(networks.asset_by_contract("solana", sol.contract), sol)
        self.assertIsNone(networks.asset_by_contract("solana", sol.contract.lower()))
        self.assertIsNone(networks.asset_by_contract("nowhere", "x"))

    def test_step_is_one_cent_for_stablecoins_and_1e12_wei_for_eth(self):
        for net in networks.NETWORKS.values():
            for a in net.assets.values():
                if a.symbol == "ETH":
                    self.assertEqual(a.step, 10**12)
                    self.assertEqual(a.contract, "")
                else:
                    self.assertEqual(a.step, 10 ** (a.decimals - 2))

    def test_explorer_url(self):
        self.assertIn("abc", networks.explorer_tx_url("ethereum", "abc"))


class AddressTests(SimpleTestCase):
    def test_normalize_evm_lowercases_and_rejects_short(self):
        self.assertEqual(addresses.normalize("evm", "0x" + "AB" * 20), "0x" + "ab" * 20)
        with self.assertRaises(ValueError):
            addresses.normalize("evm", "0x1234")
        with self.assertRaises(ValueError):
            addresses.normalize("evm", "ab" * 20)

    def test_normalize_tron_accepts_good_and_rejects_bad_checksum(self):
        self.assertEqual(addresses.normalize("tron", TRON_USDT), TRON_USDT)
        bad = TRON_USDT[:-1] + ("7" if TRON_USDT[-1] != "7" else "8")
        with self.assertRaises(ValueError):
            addresses.normalize("tron", bad)
        with self.assertRaises(ValueError):
            addresses.normalize("tron", "0x" + "ab" * 20)

    def test_normalize_solana(self):
        sol = networks.NETWORKS["solana"].assets["USDC"].contract
        self.assertEqual(addresses.normalize("solana", sol), sol)
        with self.assertRaises(ValueError):
            addresses.normalize("solana", "1111")

    def test_normalize_ton_user_friendly_to_raw(self):
        raw = addresses.normalize("ton", TON_USDT)
        wc, h = raw.split(":")
        self.assertEqual(wc, "0")
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(addresses.normalize("ton", raw), raw)
        std = TON_USDT.replace("_", "/").replace("-", "+")
        self.assertEqual(addresses.normalize("ton", std), raw)

    def test_normalize_ton_rejects_bad_crc(self):
        bad = TON_USDT[:-2] + ("AA" if not TON_USDT.endswith("AA") else "BB")
        with self.assertRaises(ValueError):
            addresses.normalize("ton", bad)

    def test_unknown_family(self):
        with self.assertRaises(ValueError):
            addresses.normalize("dogecoin", "x")

    def test_tron_hex_to_base58_roundtrip(self):
        h = addresses._b58decode(TRON_USDT)[:21].hex()
        self.assertEqual(addresses.tron_hex_to_base58(h), TRON_USDT)
        self.assertEqual(addresses.tron_hex_to_base58(h[2:]), TRON_USDT)
        self.assertEqual(addresses.tron_hex_to_base58("0x" + h[2:]), TRON_USDT)
