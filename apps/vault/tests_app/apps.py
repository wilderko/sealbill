from django.apps import AppConfig


class VaultTestsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.vault.tests_app"
    label = "vault_tests"
