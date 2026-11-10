# 8. Exportaciones y la moneda contable

**Exports** en la barra da dos archivos CSV para la contabilidad.

![Exports](../img/exports.png)

Elija las organizaciones (ninguna marcada significa todas), una fecha **From** y **To** opcionales, y descargue:

- **invoices.csv**, una fila por factura;
- **payments.csv**, una fila por pago, con su estado.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | quién y cuándo |
| `subtotal, discount, tax, total, paid` | en la moneda de la factura |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | el mismo total en la moneda contable de la organización |
| `external_reference, payment_link, migrated_from` | de dónde viene: la referencia de su sistema, un enlace de pago o el servicio desde el que se migró |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | qué llegó y cómo, en el activo recibido |
| `invoice_currency, amount_invoice_currency, quote_rate` | el importe en la moneda de la factura y el tipo de la cotización con la que se pagó |
| `accounting_currency, accounting_rate, accounting_amount` | lo mismo en la moneda contable |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | la prueba: la transacción, cuándo se vio y se confirmó, quién la confirmó |

## La moneda contable

Cada organización tiene una **moneda contable** (Settings > Organizations; por defecto EUR): la moneda en que lleva sus libros. Las columnas contables convierten cada importe a ella con **el tipo almacenado para el día**: la fecha de emisión en las facturas y la fecha de confirmación en los pagos. El worker rellena la caché cada hora (`accounting_rates`). Un día sin tipo almacenado deja esas celdas **vacías**; `rate_source` dice de dónde viene un tipo (`identity` cuando las monedas son la misma). Una exportación nunca llama a un servicio de tipos, así que es rápida y no cambia si la vuelve a ejecutar.

Los mismos archivos están disponibles por la API para un token con `read`: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` y `GET /api/v1/exports/payments.csv?…`, como máximo 400 días cada vez.
