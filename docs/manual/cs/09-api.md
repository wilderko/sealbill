# 9. API ve zkratce

Všechno, co stránky pro zaměstnance dělají s fakturami, může jiný program udělat přes JSON API přes HTTPS, základní adresa `https://<váš host>/api/v1`. Vaše instalace poskytuje vlastní, vždy aktuální dokumentaci:

| Adresa | Co to je |
|---|---|
| `/api/v1/docs` | Dokumentace pro lidi (interaktivní stránka OpenAPI). |
| `/api/v1/schema` | Schéma OpenAPI 3.1 pro generování klienta. |
| `/llms.txt` | Reference v čistém textu pro programy a AI agenty: každý endpoint, každá chyba, události webhooků a jejich ověření. |

Tato kapitola je jen nejkratší cesta dovnitř. Zbytek si přečtěte v těchto třech a v README: [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Autentizace

Vytvořte token pro jednu organizaci (kapitola 2 nebo `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) a posílejte jej jako bearer token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Špatný, chybějící nebo zrušený token je 401, chybějící scope 403, více než 120 požadavků za minutu na token 429. Dokud je trezor zapečetěný, každé volání odpoví 503 `{"error": "vault_sealed"}`; zkuste později. Chyby mají vždy tvar `{"error": "<code>", "detail": …}`.

## Vytvoření a odeslání faktury

Částky jsou **celá čísla v minor units** (centech), množství desetinné řetězce, sazby daně a slev bazické body (2000 je 20 %). Při vytváření posílejte `Idempotency-Key`, aby opakování po timeoutu nevytvořilo druhou fakturu:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

Vznikne **koncept**: nic se nepřesouvá a nic se neodesílá. Pak jej odešlete:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

Odpověď nese číslo a **jednou** také `view_url` a `pay_url`. `email_client: false` přeskočí jen e-mail s fakturou; `client_letters: false` znamená, že o této faktuře ze Sealbillu nikdy neodejde žádný e-mail klientovi (faktura, potvrzení, nové odkazy ani připomínka po 30 dnech), pro obchod, který píše vlastním zákazníkům. Ptají-li se obě na e-mail, odporují si.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` řekne, která organizace by fakturu vystavila (s uplatněným pravidlem pro USA) a jaké metody by nabídla. Faktura ukazuje `amount_paid_minor` a `balance_due_minor` a `GET /api/v1/payments?invoice=<id>` vypisuje její platby.

## Scopes v kostce

| Scope | Typické použití |
|---|---|
| `read` | reporting, párování, exporty |
| `invoices:write` | obchod nebo CRM, které vystavuje faktury; kontakty, klíče, platební odkazy |
| `payments:confirm` | back-office nástroj, který potvrzuje, odmítá, ruší nebo přiřazuje platby. Mění stav peněz. |
| `admin` | vše výše plus webhooky |

Některé akce jsou **jen pro člověka** a každý token, i `admin`, dostane `409 needs_person`: zaznamenání ruční platby a potvrzení nebo přiřazení platby, která čeká na člověka.

## Webhooky

Místo dotazování zaregistrujte koncový bod (kapitola 2 nebo `POST /api/v1/webhooks` s tokenem `admin`). Každé doručení je podepsaný `POST`; dřív, než mu uvěříte, ověřte HMAC-SHA256 surového těla svým tajemstvím v `X-Pay-Signature`, odpovězte 2xx do 10 sekund a deduplikujte podle `X-Pay-Event-Id`. `/llms.txt` obsahuje události a ukázku ověření.

## Pro AI agenty: server MCP

`mcp_server/` umožňuje AI agentovi pracovat s API přes Model Context Protocol, s osmi nástroji a **bez** práva potvrzovat platby, stornovat faktury nebo spravovat webhooky. Vydejte jeho token jen s `read` a `invoices:write`. README jej popisuje: [MCP server](../../../README.md#mcp-server).
