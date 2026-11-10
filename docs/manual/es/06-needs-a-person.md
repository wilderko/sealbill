# 6. Needs a person

**Needs a person** es la primera página y la que conviene vigilar. Enumera lo que ningún programa debe decidir por sí solo. La barra muestra cuántos elementos hay abiertos; si no hay nada que hacer, la página lo dice.

![Las colas con algo dentro](../img/needs-person.png)

Las colas solo se muestran cuando tienen elementos; **Show N queues** enumera también las vacías, para que vea qué vigila la página.

![Todas las colas](../img/needs-person-queues.png)

| Cola | Qué es y qué hacer |
|---|---|
| **Declared payments** | Un cliente pulsó «He pagado» en una factura. Compruebe su banco o la cadena. Registrar el pago en la factura resuelve la declaración; **Dismiss declaration** en la factura cierra una falsa. |
| **Payments to review** | Dinero que no coincidió con ninguna factura o coincidió mal, con el motivo: el token no se acepta, ninguna factura abierta coincide, podría coincidir más de una, llegó tras caducar la cotización, un duplicado, muy por encima de lo adeudado, la factura ya estaba cerrada, una transferencia muy pequeña de «polvo» (probablemente spam de envenenamiento de direcciones, plegado), una transacción descartada o un informe de BTCPay de una factura parcial, tardía, inválida o marcada a mano. **Confirm** la asigna a su factura, **Reject** con una nota, o **Assign to an invoice** (número de factura, importe y una nota). Con una transferencia en cadena se puede actuar cuando es final. |
| **Waiting for staff confirmation** | Pagos de Monero y Zcash (por BTCPay) y dinero registrado a mano: nada los vigila, así que compruebe la cartera o el banco y luego **Confirm** o **Reject**. Solo una persona puede confirmar; ningún token de API. |
| **Gateway payments seen but not settled** | Pagos de BTCPay o Lightning que la pasarela vio hace más de seis horas y no ha liquidado. |
| **Refunds due** | Un pago en cripto superior a su cotización. Devuelva la diferencia a la dirección indicada y luego **Refunded** con una nota. |
| **Confirmed payments the gateway now calls invalid** | BTCPay lo liquidó y luego lo declaró inválido (doble gasto, reorg). El código no deshace nada: revierta el pago desde su factura, o consérvelo. |
| **Exchange rates refused** | Un tramo fiat de los tipos está obsoleto o ha saltado. Compruebe el tipo del BCE; **accept** un salto que sea real. Entretanto se rechazan las cotizaciones en cripto en esa moneda. |
| **Watchers that could not look** | Un lector de cadena o pasarela falla desde hace diez minutos o más (el personal también recibió un correo). |
| **Transfers seen but not recorded** | Una transferencia que vio un watcher aún no se pudo registrar; se reintenta en cada pasada. |
| **Blocks not scanned, or to check** | Se omitió un tramo de bloques. Compruébelo a mano y luego **Acknowledge** con lo que comprobó. |
| **Overpaid invoices** | Devuelva fuera del sistema o **Keep the overpayment** (capítulo 4). |
| **US tax residents with invoices from another issuer** | Anule la factura y créela de nuevo; la organización de EE. UU. la emitirá entonces. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Capítulo 3. |
| **E-mail not delivered** | Un mensaje que falló durante unas dieciséis horas. **Retry**, o **Dismiss** con un motivo (queda entonces cancelado para siempre). |
| **Payment links** | Un pedido pagado superó el límite de un enlace, el emisor no ofrece ninguno de los métodos de un enlace, una respuesta escrita sobre EE. UU. contradice la guardada, o ninguna organización es el emisor de EE. UU. Pulse **Resolved** con lo que comprobó. |
| **Webhooks not delivered** | Una entrega que falló tras todos sus reintentos. **Retry** o **Dismiss** con un motivo. |

Dos reglas recorren toda la página:

- Un pago que **espera a una persona** solo lo confirma, asigna o rechaza un miembro del personal con sesión iniciada. Un token, incluso `admin`, recibe `409 needs_person` y no cambia nada. Registrar un pago manual también es tarea de una persona.
- Cada acción pide una nota donde importa, y cada acción queda en el registro de auditoría.

Las mismas colas se pueden atender por la API con `payments:confirm`, salvo las reservadas a personas (capítulo 9). Las dos listas que usará para averiguar *por qué* algo está atascado son **Settings > E-mail outbox** (cada carta, con su estado, intentos y el motivo del fallo; los destinatarios y los contenidos nunca se muestran) y **Settings > Webhook deliveries**.

![La bandeja de salida de correo](../img/outbox.png)

![Entregas de webhooks](../img/deliveries.png)
