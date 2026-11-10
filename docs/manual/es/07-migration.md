# 7. Migración desde Zaprite y Request Finance

Si antes usaba Zaprite o Request Finance, puede traer sus clientes y su historial de facturas, de modo que Sealbill sea el único lugar donde se pueda encontrar todo. El procedimiento paso a paso, incluido cómo sacar los datos de cada servicio, está en [docs/migration.md](../../migration.md). Este capítulo dice qué hace la importación y qué no.

## El principio: historial, no facturas vivas

**Cada factura migrada se registra como ya pagada.** Nada de lo que estaba abierto en otro sitio se puede pagar en Sealbill, de modo que un cliente nunca puede pagar dos veces y no se contacta a nadie. Cada factura conserva su número original, fecha de emisión, moneda, líneas, título y nota de pie.

- Cada una recibe exactamente un pago: la vía *Recorded by hand*, confirmado, por el total, con la fecha de pago de la fuente (o la de emisión). El pago lleva una nota que nombra la fuente y el número original.
- El estado de origen (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) se conserva para el personal y nunca se muestra al cliente. El CSV de facturas tiene una columna **migrated_from** (por ejemplo `zaprite:COMPLETE`), de modo que la contabilidad pueda distinguir un pago que un cliente hizo de verdad de una factura que usted declaró pagada.
- Una factura migrada es **de solo lectura**: no se puede enviar, pagar, revertir, anular, reemitir, cotizar ni borrar nada desde ella, ni por el personal ni por la API. Su página dice de dónde viene y enlaza con el original mientras exista esa cuenta.
- **No se dispara nada.** La importación no envía correos, no emite webhooks, no crea cotizaciones ni enlaces y no mueve el siguiente número de la organización.

## Qué pasa con los contactos

Los contactos se **fusionan por dirección de correo dentro de una organización** (sin distinguir mayúsculas): una persona que aparece en Zaprite y en Request Finance se convierte en un contacto, completado con la fuente que tenga los datos. Un contacto sin correo se conserva y solo se asocia a las facturas que lo señalan; nunca se fusiona por el nombre. Los registros de Zaprite y de Request Finance van a la organización que usted nombre; el emisor de EE. UU. rechaza los registros de Zaprite salvo que añada `--allow-us-issuer`.

## Números

Se conserva el número propio de la fuente. Cuando el número ya existe en la organización (Zaprite y Request Finance numeraban ambos de forma simple), el que llega en segundo lugar recibe la etiqueta de la fuente: `591-RF` (o `-Z`). La ejecución de prueba enumera cada colisión antes de escribir nada. Un número con la forma de la numeración propia de la organización que no esté por debajo de su siguiente número se rechaza y se decide a mano.

## Ejecutar la importación

El importador lee por la entrada estándar **JSON delimitado por saltos de línea**: un registro `contact` o `invoice` por línea.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Los importes van en minor units; `total_minor` debe ser igual a lo que calculan las líneas (el impuesto y los descuentos por línea se trasladan), o el registro se rechaza.
- La `address` de un contacto tiene exactamente las claves `line1`, `line2`, `city`, `zip`, `state` y `country` (un código de dos letras). Cualquier otra clave se rechaza por su nombre, de modo que un campo nunca se pierde sin que nadie lo vea.
- Cada factura se escribe en su estado final en una transacción propia.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

La bóveda debe estar desbloqueada. **Ejecute siempre primero `--dry-run`**: lo hace todo, lo revierte e imprime solo recuentos:

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

- La salida nunca imprime un nombre, una dirección, un correo ni un importe por cliente. Un registro rechazado u omitido se nombra `<source>:<source_id>` y su número de línea, para que pueda localizarlo en la exportación.
- **Se puede volver a ejecutar.** Una factura o un contacto cuyo id de origen ya se importó se omite, de modo que una ejecución interrumpida a medias simplemente se repite. Dos ejecuciones a la vez se rechazan.
- Los registros de borradores, facturas canceladas y rechazadas de Request Finance se omiten: nadie los debía.
- El registro de auditoría tiene una entrada por ejecución solo con recuentos y una por factura con la fuente y su id.
- `--btc-check id,id` imprime, para los ids de origen que nombre, el importe en satoshis y la URL de la fuente, para cotejar una factura en bitcoin con la contabilidad.

Tras la importación, compruebe una o dos facturas migradas en la página **Invoices** y descargue el CSV de facturas del periodo (capítulo 8); los totales por moneda que imprimió la ejecución de prueba deberían coincidir con las cifras de la fuente.
