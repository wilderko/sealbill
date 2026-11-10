# 9. API w skrócie

Wszystko, co strony dla pracowników robią z fakturami, może zrobić inny program przez API JSON po HTTPS, z adresem bazowym `https://<Twój host>/api/v1`. Twoja instalacja sama udostępnia pełną, zawsze aktualną dokumentację:

| Adres | Co to jest |
|---|---|
| `/api/v1/docs` | Dokumentacja dla ludzi (interaktywna strona OpenAPI). |
| `/api/v1/schema` | Schemat OpenAPI 3.1, do wygenerowania klienta. |
| `/llms.txt` | Referencja w czystym tekście dla programów i agentów AI: każdy punkt końcowy, każdy błąd, zdarzenia webhooków i sposób ich weryfikacji. |

Ten rozdział to tylko najkrótsza droga do środka. Resztę przeczytaj w tych trzech miejscach i w README: [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Uwierzytelnianie

Utwórz token dla jednej organizacji (rozdział 2 lub `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) i wysyłaj go jako token bearer:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Zły, brakujący lub unieważniony token daje 401, brakujący scope 403, ponad 120 żądań na minutę na token 429. Dopóki sejf jest zapieczętowany, każde wywołanie odpowiada 503 `{"error": "vault_sealed"}`; spróbuj później. Błędy mają zawsze postać `{"error": "<code>", "detail": …}`.

## Tworzenie i wysyłanie faktury

Kwoty to **liczby całkowite w minor units** (groszach), ilości to ciągi dziesiętne, stawki podatku i rabatu to punkty bazowe (2000 to 20 %). Przy tworzeniu wysyłaj `Idempotency-Key`, aby ponowienie po przekroczeniu czasu nie utworzyło drugiej faktury:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

To tworzy **wersję roboczą**: żadne pieniądze się nie przesuwają i nic nie jest wysyłane. Potem ją wyślij:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

Odpowiedź niesie numer i **jednorazowo** `view_url` oraz `pay_url`. `email_client: false` pomija tylko list z fakturą; `client_letters: false` oznacza, że żaden list o tej fakturze nigdy nie opuści Sealbill do klienta (faktura, potwierdzenia, nowe linki i przypomnienie po 30 dniach), dla sklepu, który sam pisze do swoich klientów. Obie opcje sobie przeczą, jeśli obie żądają listu.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` mówi, która organizacja wystawiłaby fakturę (z zastosowaną zasadą dla USA) i jakie metody by zaoferowała. Faktura pokazuje `amount_paid_minor` i `balance_due_minor`, a `GET /api/v1/payments?invoice=<id>` wymienia jej płatności.

## Scopes w skrócie

| Scope | Typowe zastosowanie |
|---|---|
| `read` | raportowanie, uzgadnianie, eksporty |
| `invoices:write` | sklep lub CRM wystawiający faktury; kontakty, klucze, linki do płatności |
| `payments:confirm` | narzędzie back-office, które potwierdza, odrzuca, cofa lub przypisuje płatności. Zmienia stan pieniędzy. |
| `admin` | wszystko powyżej plus webhooki |

Niektóre akcje są **tylko dla człowieka** i każdy token, także `admin`, dostaje `409 needs_person`: rejestracja płatności ręcznej oraz potwierdzenie, przypisanie lub odrzucenie płatności, która czeka na człowieka.

## Webhooki

Zamiast odpytywać, zarejestruj punkt końcowy (rozdział 2 lub `POST /api/v1/webhooks` z tokenem `admin`). Każde dostarczenie to podpisany `POST`; zanim mu zaufasz, zweryfikuj HMAC-SHA256 surowej treści swoim sekretem w `X-Pay-Signature`, odpowiedz 2xx w ciągu 10 sekund i deduplikuj po `X-Pay-Event-Id`. `/llms.txt` zawiera zdarzenia i fragment weryfikacji.

## Dla agentów AI: serwer MCP

`mcp_server/` pozwala agentowi AI pracować z API przez Model Context Protocol, z ośmioma narzędziami i **bez** możliwości potwierdzania płatności, anulowania faktur czy zarządzania webhookami. Wydaj jego token tylko z `read` i `invoices:write`. Opisuje go README: [MCP server](../../../README.md#mcp-server).
