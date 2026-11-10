# Migrating from Zaprite or Request Finance

Sealbill can take over your history from Zaprite (zaprite.com) and Request Finance
(app.request.finance): every client and every invoice, with its original number, buyer, lines and
dates. It works in two steps:

1. **Export** with the tool for your source, `tools/migrate/zaprite_export.py` or
   `tools/migrate/request_finance_export.js`. Each writes NDJSON: one JSON object per line.
2. **Import** with `manage.py import_invoices`, first as a dry run, then for real.

Both steps are safe to repeat. Nothing is sent to your clients at any point.

## What the importer does

Read this before the real run, because it decides how your books will look.

- **Every invoice it imports is recorded as paid.** It gets status "paid", one confirmed manual
  payment for its total (none when the total is 0) dated on the source's paid date (the issue date
  when the source has none), and a note on that payment saying it was carried over. This is the
  rule even for an invoice the source still shows as open: the import is for history, not for
  collecting money. Leave out of the file whatever you do not want recorded as paid. The source's
  own status is kept with the invoice and is counted in the report.
- **Every imported invoice is marked as migrated and locked.** Staff and the API can read it, but
  nothing can be sent, paid, reversed, voided, reissued, quoted or deleted on it. It has no client
  links.
- **Nothing happens around it.** No letter, no webhook event, no quote. Your organization's next
  invoice number does not move. The audit trail records the import under the name "importer".
- **Numbers are kept.** When the organization already holds an invoice with the same number, a
  Request Finance invoice becomes `<number>-RF` (then `-RF2`, `-RF3`, and so on) and a Zaprite one
  `<number>-Z2` (then `-Z3`, and so on). Every such change is listed in the report. A source number
  that looks like one of your organization's own future numbers is refused, for you to decide.
  If you migrate from both sources, import the one whose numbers you want to keep unchanged first.
- **Clients are merged by e-mail address.** A contact whose address the organization already knows
  is updated rather than duplicated. A record whose invoices are newer than the contact's overwrites
  its fields; an older one only fills fields that are empty. A contact without an e-mail address is
  created once and recognised again on a re-run. Each invoice
  keeps, frozen in its snapshot, the buyer exactly as the source issued it to.
- **It is idempotent.** Each invoice is recorded as `zaprite:<source id>` or `rf:<source id>`, and
  a second run skips everything it already imported, contacts included. Each invoice is written in
  a transaction of its own, so a run that stops half-way is simply run again.
- **Drafts and cancelled invoices are skipped.** Request Finance invoices whose status is `draft`,
  `canceled`, `cancelled` or `rejected` are counted and named in the report, never imported.
- **US tax residents.** When the organization you import into is marked as the issuer for US tax
  residents, every contact it imports is marked as a US tax resident. A Zaprite import into such an
  organization is refused unless you add `--allow-us-issuer`.
- **The report holds no personal data.** It prints counts, line numbers, source ids and reasons,
  never a name, an address, an e-mail address or a client's amount. A line it cannot use is refused
  with its line number and the reason, and the rest of the file is still imported.
- **Requirements.** The organization (`--org <slug>`) must exist and the vault must be unsealed.
  Only one import runs at a time.

The export files hold your clients' names, addresses and e-mail addresses. Keep them off shared
machines, never commit them (the repository ignores `*.ndjson` in its root directory), and delete
them once the import is done.

## From Zaprite

You need an API key of your Zaprite organization and Python 3.11 or newer with `requests`
(`pip install requests`; inside this repository `uv run python3 ...` has it).

1. **Export.**

   ```bash
   read -rsp 'Zaprite API key: ' ZAPRITE_API_KEY; echo; export ZAPRITE_API_KEY
   python3 tools/migrate/zaprite_export.py zaprite.ndjson
   unset ZAPRITE_API_KEY
   ```

   The file is created with mode 0600. The tool only reads from Zaprite.

2. **Read what it printed.** The summary goes to stderr and names every order it left out, by id:

   | Line | Meaning |
   |---|---|
   | `contacts=… (embedded_only=…) orders=…` | records written; embedded contacts are buyers an order names that the contact list did not hold |
   | `anonymous_payment_links=…` | payment-link payments with no buyer data at all, filed under one placeholder contact, "Anonymous payer (Zaprite payment link)" |
   | `pending=…` | PENDING orders, left out: abandoned checkouts and drafts, not money received |
   | `no_contact=…` | invoices that name no buyer anywhere, left out |
   | `no_total=…`, `no_date=…` | orders without a total or without any date, left out |
   | `tax_orders=…`, `promo_orders=…` | worth a look: an order-level promo has no field in Sealbill, so such an invoice's lines will not add up to its total and the importer refuses it |
   | `order_status …`, `invoice_status …` | how many orders Zaprite reported in each status |

   Every order Zaprite lists that is not PENDING is exported, whatever its status (COMPLETE, PAID,
   OVERPAID, UNDERPAID, PROCESSING) and will be imported as paid. An order with an invoice keeps its
   lines, discounts and tax rates; its total is the order's own `totalAmount`. Any other order
   (payment link, API order, point of sale) becomes a payment request with one line at its total,
   because Zaprite's order items carry no price.

3. **Dry run** on the Sealbill server, with the file on stdin:

   ```bash
   python manage.py import_invoices --source zaprite --org <slug> --dry-run < zaprite.ndjson
   ```

   On a server installed as in the operations manual, run `payinv-manage import_invoices ...`
   instead of `python manage.py import_invoices ...`.

4. **Read the report** (below). Check a few BTC invoices against their Zaprite pages with
   `--btc-check <id>,<id>,<id>`, which prints each one's amount in satoshis and its source URL.

5. **Back up**, then **import for real**: the same command without `--dry-run`.

6. **Delete** `zaprite.ndjson`.

## From Request Finance

Request Finance's API is not available on every plan, so the export runs in your browser, in the
page where you are already logged in. Nothing is sent anywhere: the script uses the page's own
requests and hands you a file.

1. **Log in** at https://app.request.finance and open **Get paid > Sent**. If you have several
   Request Finance accounts, export each one separately, logged in to it.
2. **Read** `tools/migrate/request_finance_export.js`. You are about to run it in a page that can
   see your invoices, so know what it does.
3. **Open the developer console** (F12, then the Console tab). Chrome asks you to type
   `allow pasting` the first time.
4. **Paste the whole script and press Enter.** Keep the tab in front. The list moves through its
   pages, about six seconds each, and then the browser offers
   `request-finance-export-<date>.ndjson` for download. The console shows a summary without
   personal data, and a table of the invoices it left out, by Request Finance id, with the reason:

   | Reason | What to do |
   |---|---|
   | `currency … is not in RF_CURRENCIES` | Sealbill cannot carry that currency yet. If Sealbill has it, add a line to `RF_CURRENCIES` at the top of the script with Request Finance's decimals from its public currency list. |
   | `an amount does not convert exactly …` | the amount has more precision than Sealbill keeps for that currency; record that invoice by hand |
   | `a line has a fixed tax amount …` | Sealbill carries tax as a rate only; record that invoice by hand |
   | `the lines are in more than one currency`, `a line has a quantity with more than two decimals`, `no invoice lines`, `no creation date` | record that invoice by hand, or leave it out |

   If the script stops with a message instead, nothing was written; follow the message and run it
   again. Reloading the page undoes everything the script did.

5. **Move the file** out of your Downloads folder to the Sealbill server, then **dry run**:

   ```bash
   python manage.py import_invoices --source rf --org <slug> --dry-run < request-finance-export-<date>.ndjson
   ```

6. **Read the report.** Request Finance's list carries no invoice total, so the script computes each
   total from the lines the way Sealbill does (discount per line, tax on the rest, rounded half up).
   Open a few invoices with discounts or tax in Request Finance and compare their totals.

7. **Back up**, then **import for real**: the same command without `--dry-run`. Delete the file.

How the script maps an invoice:

- Each distinct buyer block (e-mail address, names, address, tax id) becomes one contact, with the
  source id `b:` and the first 16 hex digits of a SHA-1 of that block. A buyer whom only drafts or
  cancelled invoices name gets no contact.
- Amounts are converted exactly from Request Finance's decimals to Sealbill's: EUR and USD keep 2,
  BTC 8, USDT and USDC 6, ETH (18 at Request Finance) is kept at 8, DAI (18) at 2. Crypto currencies
  other than BTC can be imported for history but never used for a new invoice.
- The paid date is the latest "payment received" declaration, else the latest "payment sent"
  declaration, else, for a paid invoice, its first tracked transaction.
- The source URL is the invoice's Request Finance page (`https://app.request.finance/<request id>`).

## Reading the import report

```text
DRY RUN: nothing was written (source rf, org acme)
contacts: new 12, merged 3, without e-mail 1, skipped (already imported) 0
invoices invoice EUR: 40
invoices invoice USDT: 5
total EUR: 1234500 minor units
total USDT: 250000000 minor units
skipped, already imported: 0
source status paid: 41
source status open: 4
skipped by source status canceled: 2
number collisions: 1
  rf:6a1b... number 101 -> 101-RF
refused lines: 1
  line 57 rf:6a7c...: the lines do not add up to total_minor
skipped rf:6a9d...: source status canceled
```

- **contacts**: `new` were created, `merged` matched an existing contact by e-mail address,
  `without e-mail` were created without one, `skipped` were imported by an earlier run.
- **invoices** and **total**: what was (or, in a dry run, would be) imported, per kind and currency.
  Totals are in minor units: cents, satoshis, and so on.
- **source status**: the status each imported invoice had in the source. All of them are imported
  as paid.
- **number collisions**: every number that was changed, and to what.
- **refused lines**: records that were not imported, with the line number in the file, the source
  id and the reason. Fix the cause or record those invoices by hand, then run the import again; it
  imports only what is missing.

## The NDJSON format

Both exporters write the same two record types, and you can write them yourself to import from
anywhere else (use `--source zaprite` or `--source rf` for the closest match). Contacts come before
the invoices that name them. The full rules are in `apps/invoices/importer.py`.

**contact**

| Field | |
|---|---|
| `type` | `"contact"` |
| `source_id` | required, unique in the file, at most 100 characters |
| `email` | or `null` |
| `legal_name`, `display_name`, `tax_id` | text or `null` |
| `address` | `{line1, line2, city, zip, state, country}`, `country` an ISO 3166 alpha-2 code; any other key is refused |
| `language` | optional; one of `en sk cs de es fr pl ko ru`, else the organization's default |

**invoice**

| Field | |
|---|---|
| `type` | `"invoice"` |
| `source_id` | required, at most 100 characters |
| `number` | the source's number, or `null` (at most 50 characters) |
| `kind` | `"invoice"` or `"payment_request"` |
| `contact_source_id` | the `source_id` of a contact record in the same file |
| `currency` | an invoice currency Sealbill knows (`apps/invoices/currencies.py`) |
| `issue_date`, `paid_date` | `YYYY-MM-DD`; `paid_date` may be `null` |
| `due_days` | whole days after the issue date, or `null` |
| `title`, `footer` | text or `null`; the footer becomes the invoice's note to the client |
| `lines` | `[{name, description, quantity, unit_price_minor, discount_amount_minor, discount_rate_bps, tax_rate_bps}]`; quantity with at most two decimals, amounts in minor units, rates in basis points, the last three optional |
| `total_minor` | must equal the total Sealbill computes from the lines, or the invoice is refused |
| `source_status` | required; Request Finance's `draft`, `canceled`, `cancelled` and `rejected` are skipped |
| `source_url` | optional, kept with the invoice for staff |
