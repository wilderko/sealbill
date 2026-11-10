from django.test import SimpleTestCase

from apps.rails import addresses, uris

EVM = "0x" + "ab" * 20
REF = "RF18539007547034"  # the ISO 11649 example reference
TON_USDT = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SOLANA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


class PaymentUriTests(SimpleTestCase):
    def test_eip681_token_uri_exact(self):
        self.assertEqual(
            uris.payment_uri("arbitrum", "USDC", EVM, 108270000, REF),
            "ethereum:0xaf88d065e77c8cc2239327c5edb3a432268e5831@42161/transfer"
            "?address=0xabababababababababababababababababababab&uint256=108270000",
        )
        # the recipient is written normalized, whatever case it was given in
        self.assertEqual(uris.payment_uri("arbitrum", "USDC", "0x" + "AB" * 20, 108270000, REF),
                         uris.payment_uri("arbitrum", "USDC", EVM, 108270000, REF))
        # 18 decimals on BSC: the integer is written whole, never in exponent form
        self.assertEqual(
            uris.payment_uri("bsc", "USDT", EVM, 108_270_000 * 10**12, REF),
            "ethereum:0x55d398326f99059ff775485246999027b3197955@56/transfer"
            f"?address={EVM}&uint256=108270000000000000000",
        )

    def test_native_eth_uri(self):
        self.assertEqual(uris.payment_uri("ethereum", "ETH", EVM, 40_000_000_000_000_000, REF),
                         f"ethereum:{EVM}@1?value=40000000000000000")
        self.assertEqual(uris.payment_uri("base", "ETH", EVM, 1, REF), f"ethereum:{EVM}@8453?value=1")

    def test_ton_uri_carries_reference(self):
        raw = addresses.normalize("ton", TON_USDT)  # stored form, as on a rail account
        to = addresses.ton_user_friendly(raw)
        self.assertTrue(to.startswith("UQ"))  # non-bounceable: the form wallets show for a wallet address
        self.assertEqual(addresses.normalize("ton", to), raw)
        self.assertEqual(
            uris.payment_uri("ton", "USDT", raw, 108270000, REF),
            f"ton://transfer/{to}?jetton={TON_USDT}&amount=108270000&text={REF}",
        )
        self.assertEqual(uris.payment_uri("ton", "USDT", raw, 1, "RF 18/5"), f"ton://transfer/{to}?jetton={TON_USDT}"
                                                                                "&amount=1&text=RF%2018%2F5")

    def test_ton_user_friendly_round_trips(self):
        raw = addresses.normalize("ton", TON_USDT)
        self.assertEqual(addresses.ton_user_friendly(raw, bounceable=True), TON_USDT)
        self.assertEqual(addresses.ton_user_friendly(TON_USDT, bounceable=True), TON_USDT)
        masterchain = "-1:" + "3" * 64
        self.assertEqual(addresses.normalize("ton", addresses.ton_user_friendly(masterchain)), masterchain)
        with self.assertRaises(ValueError):
            addresses.ton_user_friendly("0:12")

    def test_solana_pay_uri(self):
        self.assertEqual(
            uris.payment_uri("solana", "USDC", SOLANA, 108270000, REF),
            f"solana:{SOLANA}?amount=108.27&spl-token=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        )

    def test_tron_has_no_uri(self):
        self.assertEqual(uris.payment_uri("tron", "USDT", TRON, 108270000, REF), "")

    def test_unknown_network_asset_or_bad_address_refused(self):
        for args in (("nowhere", "USDC", EVM), ("base", "USDT", EVM), ("arbitrum", "USDC", "0x12"),
                     ("solana", "USDC", EVM)):
            with self.assertRaises(ValueError, msg=args):
                uris.payment_uri(*args, 1, REF)


class DecimalAmountTests(SimpleTestCase):
    def test_no_trailing_zeros_beyond_two_places(self):
        cases = [
            (108270000, 6, "108.27"),
            (108000000, 6, "108.00"),
            (108200000, 6, "108.20"),
            (1, 6, "0.000001"),
            (0, 6, "0.00"),
            (10**18, 18, "1.00"),
            (40_004_000_000_000_000, 18, "0.040004"),
            (123456789012345678901234567890, 18, "123456789012.34567890123456789"),
            (5, 0, "5.00"),
            (5, 1, "0.50"),
        ]
        for amount, decimals, text in cases:
            self.assertEqual(uris.decimal_amount(amount, decimals), text, (amount, decimals))
        with self.assertRaises(ValueError):
            uris.decimal_amount(-1, 6)
