# 9. The API in short

Everything the staff pages do for invoices can be done by another program through a JSON API over HTTPS, base URL
`https://<your host>/api/v1`. Your own installation serves its full, always-current documentation:

| Address | What it is |
|---|---|
| `/api/v1/docs` | The human documentation (an interactive OpenAPI page). |
| `/api/v1/schema` | The OpenAPI 3.1 schema, for generating a client. |
| `/llms.txt` | A plain-text reference written for programs and AI agents: every endpoint, every error, the webhook events and how to verify them. |

This chapter is only the shortest path in. For the rest, read those three, and the README's
[Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Authenticate

Create a token for one organization (chapter 2, or `manage.py create_api_token --org <slug> --name <name> --scopes
read,invoices:write`) and send it as a bearer token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

A wrong, missing or revoked token is 401, a missing scope 403, more than 120 requests a minute per token 429. While the
vault is sealed every call answers 503 `{"error": "vault_sealed"}`; retry later. Errors are always
`{"error": "<code>", "detail": …}`.

## Create and send an invoice

Amounts are **integers in minor units** (cents), quantities decimal strings, tax and discount rates basis points
(2000 is 20 %). Send an `Idempotency-Key` on a create, so a retry after a timeout cannot make a second invoice:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

This makes a **draft**: no money moves and nothing is sent. Then send it:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

The answer carries the number and, **once**, `view_url` and `pay_url`. `email_client: false` skips only the invoice
letter; `client_letters: false` means no letter about this invoice ever leaves Sealbill (invoice, receipts, new links,
and the 30-day reminder), for a shop that writes to its own customers. The two contradict each other if both ask for a
letter.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` tells you which organization would issue the
invoice (the US rule applied) and the methods it would offer. An invoice shows `amount_paid_minor` and
`balance_due_minor`, and `GET /api/v1/payments?invoice=<id>` lists its payments.

## Scopes at a glance

| Scope | Typical use |
|---|---|
| `read` | reporting, reconciliation, exports |
| `invoices:write` | a shop or a CRM that issues invoices; contacts, keys, payment links |
| `payments:confirm` | a back-office tool that confirms, rejects, reverses or assigns payments. It moves money state. |
| `admin` | all of the above plus webhooks |

Some actions are **person-only** and every token, `admin` included, gets `409 needs_person`: recording a manual payment,
and confirming, assigning or rejecting a payment that is waiting for a person.

## Webhooks

Instead of polling, register an endpoint (chapter 2, or `POST /api/v1/webhooks` with an `admin` token). Each delivery is
a signed `POST`; verify the HMAC-SHA256 of the raw body with your secret in `X-Pay-Signature` before you trust it, answer
2xx within 10 seconds and deduplicate on `X-Pay-Event-Id`. `/llms.txt` has the events and a verification snippet.

## For AI agents: the MCP server

`mcp_server/` lets an AI agent work with the API over the Model Context Protocol, with eight tools and **no** power to
confirm payments, void invoices or manage webhooks. Issue its token with only `read` and `invoices:write`. The README
describes it: [MCP server](../../../README.md#mcp-server).
