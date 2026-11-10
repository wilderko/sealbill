# 8. Eksporty i waluta księgowa

**Exports** na pasku daje dwa pliki CSV dla księgowości.

![Exports](../img/exports.png)

Wybierz organizacje (żadna niezaznaczona oznacza wszystkie), opcjonalną datę **From** i **To** i pobierz:

- **invoices.csv**, jeden wiersz na fakturę;
- **payments.csv**, jeden wiersz na płatność, z jej stanem.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | kto i kiedy |
| `subtotal, discount, tax, total, paid` | w walucie faktury |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | ta sama suma w walucie księgowej organizacji |
| `external_reference, payment_link, migrated_from` | skąd pochodzi: odnośnik Twojego systemu, link do płatności lub usługa, z której zmigrowano |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | co wpłynęło i jak, w otrzymanym aktywie |
| `invoice_currency, amount_invoice_currency, quote_rate` | kwota w walucie faktury i kurs oferty, po którym zapłacono |
| `accounting_currency, accounting_rate, accounting_amount` | to samo w walucie księgowej |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | dowód: transakcja, kiedy ją zauważono i potwierdzono, kto ją potwierdził |

## Waluta księgowa

Każda organizacja ma **walutę księgową** (Settings > Organizations; domyślnie EUR): walutę, w której prowadzi księgi. Kolumny księgowe przeliczają każdą kwotę na nią według **kursu zapisanego w pamięci podręcznej dla danego dnia**: daty wystawienia w przypadku faktur i daty potwierdzenia w przypadku płatności. Worker uzupełnia pamięć podręczną co godzinę (`accounting_rates`). Dzień bez zapisanego kursu zostawia te komórki **puste**; `rate_source` mówi, skąd pochodzi kurs (`identity`, gdy waluty są takie same). Eksport nigdy nie wywołuje usługi kursów, więc jest szybki i nie zmienia się przy ponownym uruchomieniu.

Te same pliki są dostępne przez API dla tokenu z `read`: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` oraz `GET /api/v1/exports/payments.csv?…`, najwyżej 400 dni naraz.
