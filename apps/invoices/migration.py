"""Markers of an invoice carried over from Zaprite or Request Finance.

The importer writes `invoice.metadata["migration"]`; this module only reads it. A migrated invoice is
history: everything it owed is recorded as paid, so nothing may be sent, paid, reversed, voided,
reissued, quoted or deleted from it, by staff or through the API.

Every mutation path asks `refusal(invoice_id)`: the one place that decides. It fails closed: if the
metadata cannot be read (vault sealed, value undecryptable) the answer is a refusal, because "cannot tell
whether this is history" must never read as "it is not".
"""
from apps.vault.fields import UndecryptableValue
from apps.vault.vault import VaultSealed

MIGRATED_MESSAGE = "This invoice was carried over from another system as already paid. It cannot be changed here."
SEALED_MESSAGE = ("Whether this invoice was carried over from another system cannot be read while the vault is "
                  "sealed. Unlock the vault first.")
UNREADABLE_MESSAGE = ("Whether this invoice was carried over from another system cannot be read: its stored "
                      "migration data cannot be decrypted. Nothing was changed.")


def migration_info(invoice) -> dict | None:
    metadata = invoice.metadata
    info = metadata.get("migration") if isinstance(metadata, dict) else None
    return info if isinstance(info, dict) and info else None


def is_migrated(invoice) -> bool:
    return migration_info(invoice) is not None


def read_state(invoice_id) -> tuple[dict | None, str | None]:
    """(info, problem) read fresh from the database. `problem` is "sealed" or "unreadable" when the
    metadata could not be read; `info` is then None."""
    from apps.invoices.models import Invoice

    try:
        stored = Invoice.objects.filter(pk=invoice_id).values_list("metadata", flat=True).first()
    except VaultSealed:
        return None, "sealed"
    except UndecryptableValue:
        return None, "unreadable"
    if isinstance(stored, dict) and stored.get("_encrypted") is True and "migration" not in stored:
        return None, "sealed"
    info = stored.get("migration") if isinstance(stored, dict) else None
    return (info if isinstance(info, dict) and info else None), None


def refusal(invoice_id) -> str | None:
    """The reason a mutation of this invoice must be refused, or None when it may go ahead."""
    info, problem = read_state(invoice_id)
    if problem == "sealed":
        return SEALED_MESSAGE
    if problem == "unreadable":
        return UNREADABLE_MESSAGE
    return MIGRATED_MESSAGE if info is not None else None


NOTES = {
    "en": {
        "zaprite_request": "Paid payment request migrated from Zaprite.",
        "zaprite": "Paid invoice migrated from Zaprite (original number {n}).",
        "rf_bare": "Paid invoice migrated from Request Finance.",
        "rf": "Paid invoice migrated from Request Finance (original number {n}).",
    },
    "sk": {
        "zaprite_request": "Zaplatená platobná požiadavka prenesená zo Zaprite.",
        "zaprite": "Zaplatená faktúra prenesená zo Zaprite (pôvodné číslo {n}).",
        "rf_bare": "Zaplatená faktúra prenesená z Request Finance.",
        "rf": "Zaplatená faktúra prenesená z Request Finance (pôvodné číslo {n}).",
    },
}


def staff_note(info: dict) -> str:
    """The note stored on the payment of an imported invoice, in `settings.MIGRATION_NOTE_LANGUAGE`
    (`en` by default; an unknown value falls back to English)."""
    from django.conf import settings

    texts = NOTES.get(getattr(settings, "MIGRATION_NOTE_LANGUAGE", "en"), NOTES["en"])
    number = info.get("source_number")
    if info.get("source") == "zaprite":
        return texts["zaprite"].format(n=number) if number else texts["zaprite_request"]
    return texts["rf"].format(n=number) if number else texts["rf_bare"]
