from django.core.management.base import BaseCommand

from apps.vault.vault import vault


class Command(BaseCommand):
    help = ("Seal the vault now, in every process: the key file is removed, and the web workers and the "
            "worker drop the key on their next use. Unseal again with unseal_vault or /unlock/.")

    def handle(self, *args, **options):
        vault.seal()
        self.stdout.write("Vault sealed. Every process drops the key on its next use; unseal with unseal_vault "
                          "or at /unlock/.")
