# 8. Exporty a účtovná mena

**Exports** v pruhu vľavo dáva dva súbory CSV pre účtovníctvo.

![Exports](../img/exports.png)

Vyberte organizácie (nič nezaškrtnuté znamená všetky), voliteľný dátum **From** a **To** a stiahnite:

- **invoices.csv**, jeden riadok na faktúru;
- **payments.csv**, jeden riadok na platbu, s jej stavom.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | kto a kedy |
| `subtotal, discount, tax, total, paid` | v mene faktúry |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | tá istá suma v účtovnej mene organizácie |
| `external_reference, payment_link, migrated_from` | odkiaľ pochádza: referencia vášho systému, platobný odkaz alebo služba, z ktorej bola migrovaná |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | čo dorazilo a ako, v prijatom aktíve |
| `invoice_currency, amount_invoice_currency, quote_rate` | suma v mene faktúry a kurz ponuky, pri ktorom sa platilo |
| `accounting_currency, accounting_rate, accounting_amount` | to isté v účtovnej mene |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | dôkaz: transakcia, kedy bola zaznamenaná a potvrdená, kto ju potvrdil |

## Účtovná mena

Každá organizácia má **účtovnú menu** (Settings > Organizations; štandardne EUR): menu, v ktorej vedie účtovníctvo. Účtovné stĺpce prepočítajú každú sumu na ňu **kurzom uloženým pre daný deň**: dátum vystavenia pri faktúrach a dátum potvrdenia pri platbách. Worker plní vyrovnávaciu pamäť každú hodinu (`accounting_rates`). Deň bez uloženého kurzu nechá tie bunky **prázdne**; `rate_source` hovorí, odkiaľ kurz pochádza (`identity`, keď sú meny rovnaké). Export nikdy nevolá službu s kurzami, takže je rýchly a nemení sa, keď ho spustíte znova.

Rovnaké súbory sú dostupné cez API pre token s `read`: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` a `GET /api/v1/exports/payments.csv?…`, najviac 400 dní naraz.
