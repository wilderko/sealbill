# 8. Exporty a účetní měna

**Exports** v pruhu vlevo dává dva soubory CSV pro účetnictví.

![Exports](../img/exports.png)

Vyberte organizace (nic nezaškrtnuto znamená všechny), volitelné datum **From** a **To** a stáhněte:

- **invoices.csv**, jeden řádek na fakturu;
- **payments.csv**, jeden řádek na platbu, s jejím stavem.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | kdo a kdy |
| `subtotal, discount, tax, total, paid` | v měně faktury |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | táž částka v účetní měně organizace |
| `external_reference, payment_link, migrated_from` | odkud pochází: reference vašeho systému, platební odkaz nebo služba, ze které byla migrována |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | co dorazilo a jak, v přijatém aktivu |
| `invoice_currency, amount_invoice_currency, quote_rate` | částka v měně faktury a kurz nabídky, při kterém se platilo |
| `accounting_currency, accounting_rate, accounting_amount` | totéž v účetní měně |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | důkaz: transakce, kdy byla zaznamenána a potvrzena, kdo ji potvrdil |

## Účetní měna

Každá organizace má **účetní měnu** (Settings > Organizations; výchozí EUR): měnu, ve které vede účetnictví. Účetní sloupce přepočítají každou částku na ni **kurzem uloženým pro daný den**: datum vystavení u faktur a datum potvrzení u plateb. Worker plní mezipaměť každou hodinu (`accounting_rates`). Den bez uloženého kurzu nechá ty buňky **prázdné**; `rate_source` říká, odkud kurz pochází (`identity`, když jsou měny stejné). Export nikdy nevolá službu s kurzy, takže je rychlý a nemění se, když jej spustíte znovu.

Stejné soubory jsou dostupné přes API pro token s `read`: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` a `GET /api/v1/exports/payments.csv?…`, nejvýše 400 dnů najednou.
