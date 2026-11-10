# 9. L'API en bref

Tout ce que les pages du personnel font avec les factures, un autre programme peut le faire par une API JSON sur HTTPS, d'URL de base `https://<votre hôte>/api/v1`. Votre propre installation sert sa documentation complète et toujours à jour :

| Adresse | Ce que c'est |
|---|---|
| `/api/v1/docs` | La documentation pour les humains (une page OpenAPI interactive). |
| `/api/v1/schema` | Le schéma OpenAPI 3.1, pour générer un client. |
| `/llms.txt` | Une référence en texte brut écrite pour les programmes et les agents d'IA : chaque point d'accès, chaque erreur, les événements de webhook et la manière de les vérifier. |

Ce chapitre n'est que le plus court chemin pour y entrer. Pour le reste, lisez ces trois pages et le README : [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api).

## S'authentifier

Créez un jeton pour une organisation (chapitre 2, ou `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) et envoyez-le comme jeton bearer :

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

Un jeton erroné, manquant ou révoqué donne 401, un scope manquant 403, plus de 120 requêtes par minute et par jeton 429. Tant que le coffre est scellé, chaque appel répond 503 `{"error": "vault_sealed"}` ; réessayez plus tard. Les erreurs ont toujours la forme `{"error": "<code>", "detail": …}`.

## Créer et envoyer une facture

Les montants sont des **entiers en minor units** (centimes), les quantités des chaînes décimales, les taux de taxe et de remise des points de base (2000 vaut 20 %). Envoyez un `Idempotency-Key` à la création, pour qu'une nouvelle tentative après un délai dépassé ne crée pas une seconde facture :

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

Cela crée un **brouillon** : aucun argent ne bouge et rien n'est envoyé. Puis envoyez-le :

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

La réponse porte le numéro et, **une seule fois**, `view_url` et `pay_url`. `email_client: false` ne saute que la lettre de facture ; `client_letters: false` signifie qu'aucune lettre au sujet de cette facture ne quitte jamais Sealbill vers le client (facture, reçus, nouveaux liens et rappel à 30 jours), pour une boutique qui écrit elle-même à ses clients. Les deux se contredisent si l'une et l'autre demandent une lettre.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com` indique quelle organisation émettrait la facture (règle américaine appliquée) et quelles méthodes elle proposerait. Une facture montre `amount_paid_minor` et `balance_due_minor`, et `GET /api/v1/payments?invoice=<id>` liste ses paiements.

## Les scopes d'un coup d'œil

| Scope | Usage typique |
|---|---|
| `read` | reporting, rapprochement, exports |
| `invoices:write` | une boutique ou un CRM qui émet des factures ; contacts, clés, liens de paiement |
| `payments:confirm` | un outil de back-office qui confirme, rejette, annule ou affecte des paiements. Il fait évoluer l'état de l'argent. |
| `admin` | tout ce qui précède plus les webhooks |

Certaines actions sont **réservées aux personnes** et tout jeton, même `admin`, reçoit `409 needs_person` : enregistrer un paiement manuel, et confirmer ou affecter un paiement qui attend une personne.

## Webhooks

Au lieu d'interroger, enregistrez un point d'accès (chapitre 2, ou `POST /api/v1/webhooks` avec un jeton `admin`). Chaque livraison est un `POST` signé ; vérifiez le HMAC-SHA256 du corps brut avec votre secret dans `X-Pay-Signature` avant de lui faire confiance, répondez 2xx en 10 secondes et dédoublonnez sur `X-Pay-Event-Id`. `/llms.txt` contient les événements et un extrait de vérification.

## Pour les agents d'IA : le serveur MCP

`mcp_server/` permet à un agent d'IA de travailler avec l'API via le Model Context Protocol, avec huit outils et **sans** le pouvoir de confirmer des paiements, d'annuler des factures ou de gérer des webhooks. Émettez son jeton avec seulement `read` et `invoices:write`. Le README le décrit : [MCP server](../../../README.md#mcp-server).
