# 7. Migrating from Zaprite and Request Finance

If you used Zaprite or Request Finance before, you can bring your clients and your invoice history over, so that
Sealbill becomes the one place where all of them can be found. The step-by-step procedure, including how to get the data
out of each service, is in [docs/migration.md](../../migration.md). This chapter says what the import does and what it
does not.

## The principle: history, not live invoices

**Every migrated invoice is recorded as already paid.** Nothing is payable in Sealbill that was open elsewhere, so a
client can never pay twice, and nobody is contacted. Each invoice keeps its original number, issue date, currency,
lines, title and footer note.

- Each gets exactly one payment: the way *Recorded by hand*, confirmed, for the whole total, dated the source's paid
  date (or the issue date). The payment carries a note naming the source and the original number.
- The source status (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) is kept for staff,
  never shown to the client. The invoices CSV has a **migrated_from** column (for example `zaprite:COMPLETE`), so the books
  can tell a payment a client really made from an invoice you declared paid.
- A migrated invoice is **read-only**: nothing can be sent, paid, reversed, voided, reissued, quoted or deleted from it,
  by staff or through the API. Its page says where it came from and links to the original while that account exists.
- **Nothing fires.** The import sends no e-mail, emits no webhook, makes no quote, creates no link and does not move the
  organization's next number.

## What happens to contacts

Contacts are **merged by e-mail address within an organization** (case-insensitive): one person who appears in Zaprite and
in Request Finance becomes one contact, filled in from whichever source has the data. A contact with no e-mail is kept and
matched only to the invoices that point to it; it is never merged by name. Zaprite and Request Finance records go to the
organization you name; the US-resident issuer refuses Zaprite records unless you add `--allow-us-issuer`.

## Numbers

The source's own number is kept. When the number already exists in the organization (Zaprite and Request Finance both
numbered plainly), the one that arrives second gets the source tag appended: `591-RF` (or `-Z`). The dry run lists every
collision before anything is written. A number that looks like the organization's own numbering and is not below its next
number is refused, to be decided by hand.

## Running the import

The importer reads **newline-delimited JSON** on standard input: one `contact` or `invoice` record per line.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Amounts are in minor units; `total_minor` must equal what the lines compute (tax and discounts per line are carried
  over), or the record is refused.
- A contact `address` has exactly the keys `line1`, `line2`, `city`, `zip`, `state` and `country` (a two-letter code).
  Any other key is refused by name, so a field is never lost without anyone seeing it.
- Each invoice is written in its final state in its own transaction.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

The vault must be unsealed. **Always run `--dry-run` first**: it does everything, rolls it back and prints counts only:

```text
DRY RUN: nothing was written (source zaprite, org example-studio)
contacts: new 1, merged 0, without e-mail 0, skipped (already imported) 0
invoices invoice EUR: 1
invoices payment_request USD: 1
total EUR: 133700 minor units
total USD: 5000 minor units
skipped, already imported: 0
source status COMPLETE: 1
source status PAID: 1
number collisions: 0
refused lines: 1
  line 4 zaprite:bad: the lines do not add up to total_minor
```

- The output never prints a name, an address, an e-mail or an amount per client. A refused or skipped record is named
  `<source>:<source_id>` and its line number, so you can trace it in the export.
- **It can be run again.** An invoice or a contact whose source id was already imported is skipped, so a run that stopped
  half-way is simply repeated. Two runs at once are refused.
- Request Finance records of drafts, cancelled and rejected invoices are skipped: nobody owed them.
- The audit log has one entry per run with counts only, and one per invoice with the source and its id.
- `--btc-check id,id` prints, for source ids you name, the satoshi amount and the source URL, to check a Bitcoin invoice
  against the books.

After the import, check one or two migrated invoices on the **Invoices** page and download the invoices CSV for the
period (chapter 8); the totals per currency printed by the dry run should match the source's own figures.
