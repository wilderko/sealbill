"""A transfer is final only once a second operator's endpoint agrees (audit 2026-11-11, high 2).

One RPC endpoint, compromised or lying, could otherwise invent a transfer to our address, its receipt
and a finalized block, and the invoice would be confirmed paid for money that never moved."""
from django.test import override_settings

from apps.payments.e2e_helpers import EvmNode
from apps.payments.models import Payment
from apps.payments.tests_e2e_evm import K0, MINE, PAYER, USDC_ARB, EvmBase
from apps.rails.chains import evm

PRIMARY = "https://rpc.arbitrum.test"
SECOND = "https://rpc.second-operator.example"
THIRD = "https://rpc.third-operator.example"


class SecondOpinionTests(EvmBase):
    def two_operators(self, *urls):
        return override_settings(EVM_RPC_URLS={**{k: [f"https://rpc.{k}.test"] for k in self.networks},
                                               "arbitrum": [PRIMARY, *urls]})

    def test_a_transfer_only_one_endpoint_knows_is_never_confirmed(self):
        honest = EvmNode(self.internet, "arbitrum")  # another operator, with the real chain
        self.internet.serve(SECOND, honest)
        with self.two_operators(SECOND):
            pk, pay = self.sent_invoice()
            self.quote(pay, self.method, "arbitrum", "USDC")
            tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)  # the primary invents it
            self.nodes["arbitrum"].finalize()
            self.scan()
            payment = Payment.objects.get(tx_hash=tx)
            self.assertEqual((payment.state, payment.final), ("detected", False))
            self.work("finality_refresh")
            self.work("finality_refresh")
            self.assertEqual(Payment.objects.get(pk=payment.pk).state, "detected")
        self.assertNotSettled(pk, pay)

    def test_two_operators_that_agree_confirm_it(self):
        self.internet.serve(SECOND, self.nodes["arbitrum"])  # the same chain behind another operator
        with self.two_operators(SECOND):
            pk, pay = self.sent_invoice()
            self.quote(pay, self.method, "arbitrum", "USDC")
            tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)
            self.nodes["arbitrum"].finalize()
            self.scan()
            self.assertEqual(Payment.objects.get(tx_hash=tx).state, "confirmed")

    def test_one_operator_down_of_three_still_confirms(self):
        self.internet.serve(SECOND, self.nodes["arbitrum"])
        self.internet.serve(THIRD, self.nodes["arbitrum"])
        self.internet.down(SECOND, "timeout")
        with self.two_operators(SECOND, THIRD):
            pk, pay = self.sent_invoice()
            self.quote(pay, self.method, "arbitrum", "USDC")
            tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)
            self.nodes["arbitrum"].finalize()
            self.scan()
            self.assertEqual(Payment.objects.get(tx_hash=tx).state, "confirmed")

    def test_the_second_endpoint_must_call_the_block_final_too(self):
        lagging = EvmNode(self.internet, "arbitrum")
        self.internet.serve(SECOND, lagging)
        with self.two_operators(SECOND):
            pk, pay = self.sent_invoice()
            self.quote(pay, self.method, "arbitrum", "USDC")
            tx = self.nodes["arbitrum"].send_token(USDC_ARB, MINE, K0, sender=PAYER)
            self.nodes["arbitrum"].finalize()
            lagging.receipts[tx] = self.nodes["arbitrum"].receipts[tx]  # it has the receipt...
            self.scan()                                                  # ...in a block not final for it
            self.assertEqual(Payment.objects.get(tx_hash=tx).state, "detected")

    def test_operators_are_counted_by_domain(self):
        with override_settings(EVM_RPC_URLS={"base": ["https://a.publicnode.com/x", "https://b.publicnode.com/y",
                                                      "https://base.drpc.org"]}):
            self.assertEqual(evm.operators("base"), ["https://a.publicnode.com/x", "https://base.drpc.org"])

    def test_every_default_network_has_three_operators(self):
        for network, urls in evm.DEFAULT_RPC_URLS.items():
            with self.subTest(network), override_settings(EVM_RPC_URLS={}):
                self.assertEqual(len(evm.operators(network)), 3)
