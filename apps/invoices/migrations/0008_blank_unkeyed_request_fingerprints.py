"""Blank the unkeyed request fingerprints (audit DATA-1).

They were a plain SHA-256 over the client's e-mail address, name and the line texts, stored in
plaintext, so anyone holding the database could confirm offline who an invoice is for. New ones
are "v2:" + a keyed HMAC. An invoice whose fingerprint is blank is replayed as it was before there
were fingerprints: its Idempotency-Key or external reference returns it, whatever the body. How
many were blanked is logged, never which.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def blank(apps, schema_editor):
    Invoice = apps.get_model("invoices", "Invoice")
    count = (Invoice.objects.using(schema_editor.connection.alias).exclude(request_fingerprint="")
             .exclude(request_fingerprint__startswith="v2:").update(request_fingerprint=""))
    if count:
        logger.info("blanked %d unkeyed request fingerprint(s)", count)


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0007_backfill_invoice_snapshots"),
    ]

    operations = [
        migrations.RunPython(blank, migrations.RunPython.noop),
    ]
