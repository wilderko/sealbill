"""Set the encrypted credentials of a BTCPay or Lightning rail account.

The secrets are asked for one by one: without echo (`getpass`) at a terminal, or one per line
from standard input otherwise, as `unseal_vault` reads its password:

    printf '%s\n' "$API_KEY" "$WEBHOOK_SECRET" | manage.py set_rail_credentials 3

They never travel in argv, are never printed and never logged. The vault must be unsealed: the
credentials are encrypted with its key. An empty answer keeps the value already stored.

Only a credential that provably cannot spend is stored (audit DATA-3): an NWC connection with no
`pay*` method, an LND macaroon within invoices:read, invoices:write and info:read, a Core
Lightning rune whose methods are limited to invoice, listinvoices, waitinvoice and getinfo, and a
BTCPay key whose permissions, as BTCPay lists them, are within creating and reading invoices,
managing webhooks and viewing the store's settings. An LNbits
invoice key cannot be told from its admin key, so the operator vouches for it with
`--lnbits-key-is-invoice-key`, and the audit entry records that they did.
"""
import getpass
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.rails.credentials import LNBITS_FLAG, CredentialRefused, fields_for, set_credentials
from apps.rails.models import RailAccount
from apps.vault.vault import VaultSealed, vault

ACTOR = "manage.py set_rail_credentials"


def _ask(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().rstrip("\r\n").strip()


class Command(BaseCommand):
    help = ("Set the credentials of a BTCPay or Lightning rail account. The secrets are prompted for "
            "(or read from stdin, one per line), never taken from the command line.")

    def add_arguments(self, parser):
        parser.add_argument("account_pk", type=int, help="the rail account's id")
        parser.add_argument(
            LNBITS_FLAG, action="store_true", dest="lnbits_key_is_invoice_key",
            help="LNbits only: you pasted the wallet's Invoice/read key, not its Admin key. They cannot be told "
                 "apart without asking LNbits, so the command takes your word for it and audits that you gave it.")

    def handle(self, *args, account_pk, lnbits_key_is_invoice_key=False, **options):
        if not vault.is_unsealed():
            raise CommandError("The vault is sealed: unseal it first (unseal_vault). The credentials are "
                               "encrypted with its key.")
        try:
            account = RailAccount.objects.filter(pk=account_pk).first()
            if account is None:
                raise CommandError(f"no rail account #{account_pk}")
            names = fields_for(account)
            stored = dict(account.credentials or {})
        except VaultSealed:
            raise CommandError("The vault sealed meanwhile: unseal it and try again.") from None
        except CredentialRefused as exc:
            raise CommandError(exc.message) from None
        backend = (account.config or {}).get("backend") if account.kind == "lightning" else None
        if lnbits_key_is_invoice_key and backend != "lnbits":
            raise CommandError(f"{LNBITS_FLAG} is only for an LNbits account")
        given = {}
        for name in names:
            value = _ask(f"{name} (empty keeps the stored one): " if stored.get(name) else f"{name}: ")
            if value:
                given[name] = value
            elif not stored.get(name):
                raise CommandError(f"{name} is required")
        try:
            saved = set_credentials(account, given, actor=None, actor_name=ACTOR,
                                    lnbits_vouched=lnbits_key_is_invoice_key)
        except CredentialRefused as exc:
            raise CommandError(exc.message) from None
        if not saved:
            self.stdout.write("Nothing changed.")
            return
        self.stdout.write(f"Saved {', '.join(saved)} for rail account #{account.pk} ({account.kind}).")
