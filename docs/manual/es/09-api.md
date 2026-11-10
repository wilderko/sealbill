# 9. La API en resumen

Todo lo que las páginas del personal hacen con las facturas puede hacerlo otro programa mediante una API JSON sobre HTTPS, con URL base `https://<su host>/api/v1`. Su propia instalación sirve su documentación completa y siempre actual:

| Dirección | Qué es |
|---|---|
| `/api/v1/docs` | La documentación para personas (una página OpenAPI interactiva). |
| `/api/v1/schema` | El esquema OpenAPI 3.1, para generar un cliente. |
| `/llms.txt` | Una referencia en texto plano para programas y agentes de IA: cada endpoint, cada error, los eventos de webhook y cómo verificarlos. |

Este capítulo es solo el camino más corto para entrar. Para el resto, lea esos tres y el README: [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Autenticarse

Cree un token para una organización (capítulo 2, o `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) y envíelo como token bearer:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Un token incorrecto, ausente o revocado da 401, un scope que falta 403, más de 120 peticiones por minuto y token 429. Mientras la bóveda está sellada, toda llamada responde 503 `{"error": "vault_sealed"}`; reintente más tarde. Los errores tienen siempre la forma `{"error": "<code>", "detail": …}`.

## Crear y enviar una factura

Los importes son **enteros en minor units** (céntimos), las cantidades cadenas decimales, los tipos de impuesto y descuento puntos básicos (2000 es 20 %). Envíe un `Idempotency-Key` al crear, para que un reintento tras un timeout no cree una segunda factura:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

Esto crea un **borrador**: no se mueve dinero y no se envía nada. Después, envíelo:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

La respuesta lleva el número y, **una sola vez**, `view_url` y `pay_url`. `email_client: false` omite solo la carta de la factura; `client_letters: false` significa que ninguna carta sobre esta factura sale jamás de Sealbill hacia el cliente (factura, recibos, enlaces nuevos y el recordatorio a los 30 días), para una tienda que escribe a sus propios clientes. Las dos opciones se contradicen si ambas piden una carta.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` dice qué organización emitiría la factura (con la regla de EE. UU. aplicada) y qué métodos ofrecería. Una factura muestra `amount_paid_minor` y `balance_due_minor`, y `GET /api/v1/payments?invoice=<id>` enumera sus pagos.

## Scopes de un vistazo

| Scope | Uso típico |
|---|---|
| `read` | informes, conciliación, exportaciones |
| `invoices:write` | una tienda o CRM que emite facturas; contactos, claves, enlaces de pago |
| `payments:confirm` | una herramienta de back-office que confirma, rechaza, revierte o asigna pagos. Mueve el estado del dinero. |
| `admin` | todo lo anterior más los webhooks |

Algunas acciones son **solo para personas** y todo token, incluso `admin`, recibe `409 needs_person`: registrar un pago manual y confirmar, asignar o rechazar un pago que espera a una persona.

## Webhooks

En lugar de consultar, registre un endpoint (capítulo 2, o `POST /api/v1/webhooks` con un token `admin`). Cada entrega es un `POST` firmado; verifique el HMAC-SHA256 del cuerpo en bruto con su secreto en `X-Pay-Signature` antes de fiarse, responda 2xx en 10 segundos y deduplique por `X-Pay-Event-Id`. `/llms.txt` tiene los eventos y un fragmento de verificación.

## Para agentes de IA: el servidor MCP

`mcp_server/` permite a un agente de IA trabajar con la API mediante el Model Context Protocol, con ocho herramientas y **sin** poder para confirmar pagos, anular facturas ni gestionar webhooks. Emita su token solo con `read` e `invoices:write`. El README lo describe: [MCP server](../../../README.md#mcp-server).
