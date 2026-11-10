# 8. Экспорт и учётная валюта

**Exports** на панели даёт два файла CSV для бухгалтерии.

![Exports](../img/exports.png)

Выберите организации (если не отмечена ни одна, значит все), необязательные даты **From** и **To** и скачайте:

- **invoices.csv**, одна строка на счёт;
- **payments.csv**, одна строка на платёж, с его состоянием.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | кто и когда |
| `subtotal, discount, tax, total, paid` | в валюте счёта |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | та же сумма в учётной валюте организации |
| `external_reference, payment_link, migrated_from` | откуда он: ссылка вашей системы, платёжная ссылка или сервис, из которого он перенесён |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | что поступило и как, в полученном активе |
| `invoice_currency, amount_invoice_currency, quote_rate` | сумма в валюте счёта и курс котировки, по которой платили |
| `accounting_currency, accounting_rate, accounting_amount` | то же в учётной валюте |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | свидетельство: транзакция, когда она замечена и подтверждена, кто её подтвердил |

## Учётная валюта

У каждой организации есть **учётная валюта** (Settings > Organizations; по умолчанию EUR): валюта, в которой она ведёт книги. Учётные столбцы пересчитывают каждую сумму в неё по **курсу, сохранённому в кэше на этот день**: по дате выставления для счетов и по дате подтверждения для платежей. Воркер пополняет кэш ежечасно (`accounting_rates`). День без сохранённого курса оставляет эти ячейки **пустыми**; `rate_source` указывает, откуда взят курс (`identity`, когда валюты одинаковы). Экспорт никогда не обращается к сервису курсов, поэтому он быстрый и не меняется при повторном запуске.

Те же файлы доступны через API для токена с `read`: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` и `GET /api/v1/exports/payments.csv?…`, не более 400 дней за раз.
