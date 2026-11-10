import sys

from django.core.management.base import BaseCommand, CommandError

from apps.vault import unlock_attempts
from apps.vault.vault import vault


class Command(BaseCommand):
    help = "Unseal the vault. The password is read from stdin."

    def handle(self, *args, **options):
        password = sys.stdin.readline().rstrip("\n")
        if not vault.unseal(password):
            raise CommandError("Wrong password or vault not initialised.")
        unlock_attempts.unsealed()
        self.stdout.write("Vault unsealed.")
