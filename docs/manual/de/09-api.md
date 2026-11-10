# 9. Die API in Kürze

Alles, was die Mitarbeiterseiten mit Rechnungen tun, kann ein anderes Programm über eine JSON-API über HTTPS tun, Basis-URL `https://<Ihr Host>/api/v1`. Ihre eigene Installation liefert ihre vollständige, stets aktuelle Dokumentation:

| Adresse | Was es ist |
|---|---|
| `/api/v1/docs` | Die Dokumentation für Menschen (eine interaktive OpenAPI-Seite). |
| `/api/v1/schema` | Das OpenAPI-3.1-Schema, um einen Client zu generieren. |
| `/llms.txt` | Eine Klartext-Referenz für Programme und KI-Agenten: jeder Endpunkt, jeder Fehler, die Webhook-Ereignisse und wie man sie prüft. |

Dieses Kapitel ist nur der kürzeste Weg hinein. Den Rest lesen Sie in diesen dreien und in der README: [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Authentifizieren

Erstellen Sie einen Token für eine Organisation (Kapitel 2, oder `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) und senden Sie ihn als Bearer-Token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Ein falscher, fehlender oder widerrufener Token ergibt 401, ein fehlender Scope 403, mehr als 120 Anfragen pro Minute und Token 429. Solange der Tresor versiegelt ist, antwortet jeder Aufruf mit 503 `{"error": "vault_sealed"}`; versuchen Sie es später. Fehler haben immer die Form `{"error": "<code>", "detail": …}`.

## Eine Rechnung erstellen und senden

Beträge sind **ganze Zahlen in minor units** (Cent), Mengen Dezimal-Strings, Steuer- und Rabattsätze Basispunkte (2000 sind 20 %). Senden Sie beim Erstellen einen `Idempotency-Key`, damit eine Wiederholung nach einem Timeout keine zweite Rechnung erzeugt:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

Das erzeugt einen **Entwurf**: Es bewegt sich kein Geld, und nichts wird gesendet. Dann senden Sie ihn:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

Die Antwort enthält die Nummer und **einmalig** `view_url` und `pay_url`. `email_client: false` überspringt nur den Rechnungsbrief; `client_letters: false` bedeutet, dass über diese Rechnung nie ein Brief von Sealbill an den Kunden geht (Rechnung, Quittungen, neue Links und die Erinnerung nach 30 Tagen), für einen Shop, der seinen eigenen Kunden schreibt. Verlangen beide einen Brief, widersprechen sie sich.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` sagt, welche Organisation die Rechnung ausstellen würde (mit angewandter US-Regel) und welche Methoden sie anböte. Eine Rechnung zeigt `amount_paid_minor` und `balance_due_minor`, und `GET /api/v1/payments?invoice=<id>` listet ihre Zahlungen.

## Scopes auf einen Blick

| Scope | Typische Verwendung |
|---|---|
| `read` | Reporting, Abgleich, Exporte |
| `invoices:write` | ein Shop oder CRM, das Rechnungen ausstellt; Kontakte, Schlüssel, Zahlungslinks |
| `payments:confirm` | ein Back-Office-Werkzeug, das Zahlungen bestätigt, ablehnt, rückgängig macht oder zuordnet. Es ändert den Geldzustand. |
| `admin` | alles oben plus Webhooks |

Manche Aktionen sind **nur für Menschen**, und jeder Token, auch `admin`, bekommt `409 needs_person`: eine manuelle Zahlung zu erfassen und eine Zahlung zu bestätigen oder zuzuordnen, die auf einen Menschen wartet.

## Webhooks

Statt abzufragen, registrieren Sie einen Endpunkt (Kapitel 2, oder `POST /api/v1/webhooks` mit einem `admin`-Token). Jede Auslieferung ist ein signierter `POST`; prüfen Sie den HMAC-SHA256 des rohen Bodys mit Ihrem Geheimnis in `X-Pay-Signature`, bevor Sie ihm trauen, antworten Sie innerhalb von 10 Sekunden mit 2xx und deduplizieren Sie anhand von `X-Pay-Event-Id`. `/llms.txt` enthält die Ereignisse und ein Prüfbeispiel.

## Für KI-Agenten: der MCP-Server

`mcp_server/` lässt einen KI-Agenten über das Model Context Protocol mit der API arbeiten, mit acht Werkzeugen und **ohne** die Macht, Zahlungen zu bestätigen, Rechnungen zu stornieren oder Webhooks zu verwalten. Stellen Sie seinen Token nur mit `read` und `invoices:write` aus. Die README beschreibt ihn: [MCP server](../../../README.md#mcp-server).
