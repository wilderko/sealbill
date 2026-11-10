from django.apps import AppConfig


class RailsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rails"

    def ready(self):
        from apps.rails.chains.base import register
        from apps.rails.chains.evm import EvmAdapter
        from apps.rails.chains.solana import SolanaAdapter
        from apps.rails.chains.ton import TonAdapter
        from apps.rails.chains.tron import TronAdapter

        register("evm", EvmAdapter())
        register("tron", TronAdapter())
        register("solana", SolanaAdapter())
        register("ton", TonAdapter())

        from apps.rails.gateways import btcpay, registry

        registry.register("btcpay", btcpay.factory)

        from apps.rails.gateways import lightning

        registry.register("lightning", lightning.factory)
