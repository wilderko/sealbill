# 9. API stručne

Všetko, čo stránky pre zamestnancov robia s faktúrami, môže iný program urobiť cez JSON API cez HTTPS, základná adresa `https://<váš host>/api/v1`. Vaša inštalácia poskytuje vlastnú, vždy aktuálnu dokumentáciu:

| Adresa | Čo to je |
|---|---|
| `/api/v1/docs` | Dokumentácia pre ľudí (interaktívna stránka OpenAPI). |
| `/api/v1/schema` | Schéma OpenAPI 3.1 na generovanie klienta. |
| `/llms.txt` | Referencia v čistom texte pre programy a AI agentov: každý endpoint, každá chyba, udalosti webhookov a ich overenie. |

Táto kapitola je len najkratšia cesta dnu. Zvyšok si prečítajte v týchto troch a v README: [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## Autentifikácia

Vytvorte token pre jednu organizáciu (kapitola 2 alebo `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) a posielajte ho ako bearer token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Zlý, chýbajúci alebo zrušený token je 401, chýbajúci scope 403, viac než 120 požiadaviek za minútu na token 429. Kým je trezor zapečatený, každé volanie odpovie 503 `{"error": "vault_sealed"}`; skúste neskôr. Chyby majú vždy tvar `{"error": "<code>", "detail": …}`.

## Vytvorenie a odoslanie faktúry

Sumy sú **celé čísla v minor units** (centoch), množstvá desatinné reťazce, sadzby dane a zliav bázické body (2000 je 20 %). Pri vytváraní posielajte `Idempotency-Key`, aby opakovanie po timeoute nevytvorilo druhú faktúru:

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

Vznikne **koncept**: nič sa nepresúva a nič sa neodosiela. Potom ho odošlite:

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

Odpoveď nesie číslo a **raz** aj `view_url` a `pay_url`. `email_client: false` preskočí iba e-mail s faktúrou; `client_letters: false` znamená, že o tejto faktúre z Sealbillu nikdy neodíde žiadny e-mail klientovi (faktúra, potvrdenia, nové odkazy ani pripomienka po 30 dňoch), pre obchod, ktorý píše vlastným zákazníkom. Ak sa pýtajú obe na e-mail, protirečia si.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` povie, ktorá organizácia by faktúru vystavila (s uplatneným pravidlom pre USA) a aké metódy by ponúkla. Faktúra ukazuje `amount_paid_minor` a `balance_due_minor` a `GET /api/v1/payments?invoice=<id>` vypisuje jej platby.

## Scopes v kocke

| Scope | Typické použitie |
|---|---|
| `read` | reporting, párovanie, exporty |
| `invoices:write` | obchod alebo CRM, ktoré vystavuje faktúry; kontakty, kľúče, platobné odkazy |
| `payments:confirm` | back-office nástroj, ktorý potvrdzuje, odmieta, ruší alebo priraďuje platby. Mení stav peňazí. |
| `admin` | všetko vyššie plus webhooky |

Niektoré akcie sú **iba pre človeka** a každý token, aj `admin`, dostane `409 needs_person`: zaznamenanie ručnej platby a potvrdenie, priradenie alebo zamietnutie platby, ktorá čaká na človeka.

## Webhooky

Namiesto dopytovania zaregistrujte koncový bod (kapitola 2 alebo `POST /api/v1/webhooks` s tokenom `admin`). Každé doručenie je podpísané `POST`; skôr, než mu uveríte, overte HMAC-SHA256 surového tela svojím tajomstvom v `X-Pay-Signature`, odpovedzte 2xx do 10 sekúnd a deduplikujte podľa `X-Pay-Event-Id`. `/llms.txt` obsahuje udalosti a ukážku overenia.

## Pre AI agentov: server MCP

`mcp_server/` umožňuje AI agentovi pracovať s API cez Model Context Protocol, s ôsmimi nástrojmi a **bez** práva potvrdzovať platby, stornovať faktúry alebo spravovať webhooky. Vydajte jeho token iba s `read` a `invoices:write`. README ho popisuje: [MCP server](../../../README.md#mcp-server).
