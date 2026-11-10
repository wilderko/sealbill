"""Freeze the invoices already sent (audit API-4).

Every invoice that is no longer a draft gets its snapshot from the rows it renders from today:
that is what its PDF and pages show now, and from here on they stop following those rows.

The values are encrypted, so this needs the vault. Migrations run when the web service starts,
and after a reboot the vault is sealed then: the invoices are left without a snapshot (how many
is logged), and `apps.invoices.snapshot.for_render` freezes each one from its current rows the
first time it is shown. A value the vault cannot open is left alone the same way, counted.

The builder is a copy of `apps.invoices.snapshot.build` as it is today, written against the
historical models so that this migration keeps working when the models move on.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def _address_lines(address):
    if not isinstance(address, dict):
        return [str(address)] if address else []
    city = " ".join(p for p in (address.get("zip"), address.get("city")) if p)
    parts = [address.get("line1"), address.get("line2"), city, address.get("state"), address.get("country")]
    return [p for p in parts if p]


def _instructions(raw):
    return dict(raw) if isinstance(raw, dict) else {}


def _methods(invoice, BankAccount, FiatLink, alias):
    out = []
    for key in invoice.offered_methods or []:
        kind, _, raw = str(key).partition(":")
        if not raw.isdigit():
            continue
        if kind == "bank":
            acct = BankAccount.objects.using(alias).filter(pk=int(raw), org_id=invoice.org_id).first()
            if acct is None:
                continue
            out.append({
                "kind": "bank", "label": acct.label or acct.bank_name,
                "beneficiary_name": acct.beneficiary_name, "beneficiary_address": acct.beneficiary_address,
                "account_number": acct.account_number,
                "bank_name": acct.bank_name, "bank_swift": acct.bank_swift, "bank_address": acct.bank_address,
                "intermediary_bank_name": acct.intermediary_bank_name,
                "intermediary_swift": acct.intermediary_swift,
                "intermediary_account": acct.intermediary_account,
                "instructions": _instructions(acct.instructions),
            })
        elif kind == "fiat":
            link = FiatLink.objects.using(alias).filter(pk=int(raw), org_id=invoice.org_id).first()
            if link is None:
                continue
            out.append({"kind": "fiat", "label": link.label or link.kind, "url": link.url,
                        "instructions": _instructions(link.instructions)})
    return out


def _snapshot(invoice, BankAccount, FiatLink, alias):
    org, contact = invoice.org, invoice.contact
    return {
        "version": 1,
        "seller": {"legal_name": org.legal_name or org.slug, "address": org.address, "tax_ids": org.tax_ids,
                   "footer_note": org.footer_note},
        "buyer": {"name": contact.legal_name or contact.display_name,
                  "address_lines": _address_lines(contact.address), "tax_ids": contact.tax_ids},
        "methods": _methods(invoice, BankAccount, FiatLink, alias),
    }


def backfill(apps, schema_editor):
    from apps.vault.fields import UndecryptableValue
    from apps.vault.vault import VaultSealed, vault

    alias = schema_editor.connection.alias
    Invoice = apps.get_model("invoices", "Invoice")
    pks = list(Invoice.objects.using(alias).exclude(status="draft").filter(snapshot__isnull=True)
               .order_by("pk").values_list("pk", flat=True))
    if not pks:
        return
    if not vault.is_unsealed():
        logger.warning("%d sent invoice(s) have no snapshot yet because the vault is sealed; each is frozen "
                       "from its current rows the first time it is shown", len(pks))
        return
    BankAccount, FiatLink = apps.get_model("orgs", "BankAccount"), apps.get_model("orgs", "FiatLink")
    frozen = unreadable = 0
    for pk in pks:
        try:
            invoice = Invoice.objects.using(alias).select_related("org", "contact").get(pk=pk)
            snap = _snapshot(invoice, BankAccount, FiatLink, alias)
        except (UndecryptableValue, VaultSealed):
            unreadable += 1
            continue
        frozen += Invoice.objects.using(alias).filter(pk=pk, snapshot__isnull=True).update(snapshot=snap)
    logger.info("froze %d sent invoice(s) from their current rows", frozen)
    if unreadable:
        logger.warning("%d sent invoice(s) could not be read with the current vault key and have no snapshot; "
                       "each is frozen the first time it is shown", unreadable)


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0006_invoice_snapshot_fingerprint_v2"),
        ("orgs", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
