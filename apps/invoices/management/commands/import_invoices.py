"""Import paid history from Zaprite or Request Finance, as NDJSON on stdin.

    payinv-manage import_invoices --source {zaprite,rf} --org <slug> [--dry-run] [--btc-check id,id,id] < export.ndjson

Prints counts, line numbers and ids of the form `zaprite:<id>` / `rf:<id>`. Never a name, an
address, an e-mail or an amount per client.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.invoices import importer
from apps.orgs.models import Organization
from apps.vault.vault import vault


class Command(BaseCommand):
    help = "Import contacts and invoices from NDJSON on stdin as paid, numbered history."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, choices=importer.SOURCES)
        parser.add_argument("--org", required=True, help="slug of the organization the invoices belong to")
        parser.add_argument("--dry-run", action="store_true", help="do everything, roll it back, print the counts")
        parser.add_argument("--allow-us-issuer", action="store_true",
                            help="allow Zaprite records into the US-resident issuer's organization")
        parser.add_argument("--btc-check", default="", help="comma-separated source ids to print as satoshis + URL")

    def handle(self, *args, **opts):
        if not vault.is_unsealed():
            raise CommandError("The vault is sealed. Unseal it first (payinv-manage unseal_vault), then run the "
                               "import again. Nothing was read or written.")
        try:
            org = Organization.objects.get(slug=opts["org"])
        except Organization.DoesNotExist:
            raise CommandError("no such organization")
        # "\n" only: str.splitlines() also breaks on U+2028, U+2029 and U+0085, which JSON leaves raw inside a
        # string, and one such character in a name used to split a record in two.
        lines = sys.stdin.read().split("\n")
        source = opts["source"]
        try:
            report = importer.run(lines, source=source, org=org, dry_run=opts["dry_run"],
                                  allow_us_issuer=opts["allow_us_issuer"])
        except importer.UsIssuerRefused:
            raise CommandError("this organization is the US-resident issuer: Zaprite records are not imported into "
                               "it unless --allow-us-issuer is given. Nothing was written.")
        except importer.ImportBusy:
            raise CommandError("another import is running; nothing was written. Wait for it, then run again.")
        w = self.stdout.write
        w(("DRY RUN: nothing was written" if opts["dry_run"] else "IMPORT DONE") + f" (source {source}, org {org.slug})")
        w(f"contacts: new {report.contacts_new}, merged {report.contacts_merged}, "
          f"without e-mail {report.contacts_without_email}, skipped (already imported) "
          f"{report.contacts_skipped_existing}")
        for (kind, currency), n in sorted(report.invoices_by_kind_currency.items()):
            w(f"invoices {kind} {currency}: {n}")
        for currency, total in sorted(report.totals_by_currency.items()):
            w(f"total {currency}: {total} minor units")
        w(f"skipped, already imported: {report.skipped_existing}")
        for status, n in sorted(report.source_status_counts.items()):
            w(f"source status {status}: {n}")
        for status, n in sorted(report.skipped_by_status.items()):
            w(f"skipped by source status {status}: {n}")
        w(f"number collisions: {len(report.collisions)}")
        for sid, number, assigned in report.collisions:
            w(f"  {source}:{sid} number {number} -> {assigned}")
        w(f"refused lines: {len(report.refused)}")
        for no, ref, reason in report.refused:
            w(f"  line {no} {ref}: {reason}" if ref else f"  line {no}: {reason}")
        for ref, why in report.skipped_refs:
            w(f"skipped {ref}: {why}")
        ids = [i.strip() for i in opts["btc_check"].split(",") if i.strip()]
        for sid, sats, url in importer.btc_lines(lines, ids) if ids else []:
            w(f"btc-check {sid} sats={sats if sats is not None else 'n/a'} url={url or 'n/a'}")
