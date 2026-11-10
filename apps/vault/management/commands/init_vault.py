import sys

from django.core.management.base import BaseCommand, CommandError

from apps.vault.vault import vault


class Command(BaseCommand):
    help = "Create the vault config. The password (16 characters or more) is read from stdin."

    def handle(self, *args, **options):
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise CommandError("Send the password on stdin.")
        try:
            vault.init(password)
        except (FileExistsError, ValueError) as exc:
            raise CommandError(str(exc))
        self.stdout.write("Vault initialised.")
