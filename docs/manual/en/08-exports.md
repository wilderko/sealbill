# 8. Exports and the accounting currency

**Exports** in the rail gives two CSV files for the books.

![Exports](../img/exports.png)

Choose the organizations (none ticked means all), an optional **From** and **To** date, and download:

- **invoices.csv**, one row per invoice;
- **payments.csv**, one row per payment, with its state.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | who and when |
| `subtotal, discount, tax, total, paid` | in the invoice currency |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | the same total in the organization's accounting currency |
| `external_reference, payment_link, migrated_from` | where it came from: your system's reference, a payment link, or the service it was migrated from |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | what arrived and how, in the asset received |
| `invoice_currency, amount_invoice_currency, quote_rate` | the amount in the invoice currency, and the rate of the quote it was paid at |
| `accounting_currency, accounting_rate, accounting_amount` | the same in the accounting currency |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | evidence: the transaction, when it was seen and confirmed, who confirmed it |

## The accounting currency

Each organization has an **accounting currency** (Settings > Organizations; the default is EUR): the currency its books
are kept in. The accounting columns convert every amount to it, using **the rate cached for the day**: the issue date for
invoices and the confirmation date for payments. The worker fills the cache hourly (`accounting_rates`). A day with no
cached rate leaves those cells **empty**; `rate_source` says where a rate came from (`identity` when the currencies
are the same). An export never calls out to a rate service, so it is quick and does not change when you run it again.

The same files are available through the API, for a token with `read`:
`GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` and `GET /api/v1/exports/payments.csv?…`, at most 400 days
at a time.
