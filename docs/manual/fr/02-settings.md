# 2. Paramètres

**Settings** est la dernière entrée de la barre. Sa page d'accueil liste tout ce qui suit, ainsi que le journal d'audit, la boîte d'envoi des e-mails (E-mail outbox) et les livraisons de webhooks (le chapitre 6 les utilise).

![La page d'accueil de Settings](../img/settings-index.png)

Chaque changement est consigné dans l'**Audit log** (Settings > Audit log) : qui, quoi et quel enregistrement, jamais les valeurs saisies.

![Le journal d'audit](../img/audit.png)

Ouvrez une entrée pour lire ses détails. Les changements énumérés au chapitre 1 sous « La reconfirmation » exigent un super-utilisateur disposant d'un second facteur récent.

## Organisations

Une **organisation** est une entité juridique qui émet des factures : son nom, son adresse, ses identifiants fiscaux, son logo, la numérotation des factures et les comptes de paiement qui lui appartiennent. Une seconde organisation est utile quand deux sociétés partagent une installation, par exemple une dans l'UE et une aux États-Unis.

![La liste des organisations](../img/orgs-list.png)

**Settings > Organizations > New organization**, ou ouvrez-en une pour la modifier :

| Champ | Ce qu'il fait |
|---|---|
| Slug | Le nom court utilisé dans l'API et dans les commandes (`example-studio`). |
| Legal name, Address, Tax ids | Imprimés dans le bloc « Émetteur » de la facture. Les identifiants fiscaux sont du texte libre. |
| Email from | L'expéditeur des e-mails de l'organisation, par exemple `Example Studio <billing@example.com>`. |
| Default language | La langue des pages client et des e-mails quand le contact n'a pas de langue préférée. |
| Footer note, Logo | Imprimés sur la facture et le PDF. Le logo est un PNG ou JPEG de 1,9 Mo au plus, stocké en PNG sans métadonnées. |
| Number format | Style Python, avec `{year}` et `{seq}` : `{year}-{seq:04d}` donne `2026-0001`. Il doit contenir `{seq}`. |
| Next number | Fixé par l'émission des factures ; il ne peut pas être modifié, sinon des numéros seraient réattribués. |
| Underpayment allowance | Un paiement crypto inférieur d'au plus la **plus petite** de ces deux valeurs, cette proportion (points de base ; 50 vaut 0,5 %) et ce montant fixe en dollars américains (1,00 par défaut), règle quand même le devis de change. Cela absorbe les frais de retrait d'une plateforme d'échange. |
| Overpayment ceiling | Un paiement crypto dépassant le montant du devis de change de plus de cette proportion (1000 vaut 10 %) n'est pas réglé, mais soumis à une personne. |
| Accounting currency | La devise des colonnes comptables des exports (chapitre 8). |
| Hosts a Back button may lead to | Un hôte par ligne. Une facture créée par l'API peut porter un `return_url` sur exactement ces hôtes. |

![Une organisation](../img/org-edit.png)

### L'émetteur pour résidents américains

Cochez **US-resident issuer** sur une organisation au plus. C'est l'organisation qui doit émettre les factures des clients résidents fiscaux américains.

- Chaque contact a une réponse **US tax resident** : yes, no ou unknown. Le formulaire de facture vous oblige à choisir ; un lien de paiement pose la question à l'acheteur.
- Quand une autre organisation demande une facture pour un contact résident fiscal américain, la facture est **émise par l'organisation émettrice**, avec sa numérotation, ses comptes bancaires et ses canaux. L'émetteur conserve sa propre copie du contact ; votre contact n'est jamais réécrit.
- Si un contact devient résident fiscal américain après qu'une facture a été émise par une autre organisation, la facture apparaît dans la file **US tax residents with invoices from another issuer** de Needs a person : annulez-la et recréez-la.
- Si aucune organisation n'est marquée, une telle facture ne peut pas être créée du tout.

Modifier l'indicateur ou les hôtes de retour est un changement soumis à reconfirmation. L'indicateur figure dans le journal d'audit et est envoyé par e-mail aux super-utilisateurs.

## Comptes bancaires et liens fiat

En bas d'une organisation figurent deux listes. Les deux sont **désactivées, jamais supprimées** : une facture émise continue d'afficher le compte qu'elle nommait.

![Comptes bancaires et liens de paiement d'une organisation](../img/org-accounts.png)

**Add bank account** demande un libellé, une devise, le bénéficiaire, l'IBAN ou le numéro de compte, la banque, son SWIFT/BIC et éventuellement une banque intermédiaire. Un compte bancaire est proposé sur les factures **dans sa propre devise**. Avec un IBAN, la page de paiement et la facture PDF affichent aussi des codes QR bancaires : en EUR pour l'IBAN d'un pays SEPA, en CZK pour un compte tchèque (chapitre 4). Les *instructions de paiement* existent une fois par langue : la page de paiement affiche celle de la langue du lecteur et se rabat sur l'anglais ; laissez une langue vide pour l'omettre.

![Un nouveau compte bancaire](../img/bank-new.png)

**Add payment link** (un *lien fiat*, à ne pas confondre avec les pages partageables du chapitre 5) est un lien vers un service qui encaisse ailleurs : Revolut, N26, PayPal, Wise ou « other ». Il faut une adresse `https://` et les devises qu'il peut accepter. La page de paiement l'affiche sous **Paiement en ligne**.

![Un nouveau lien fiat](../img/fiat-new.png)

## Canaux de paiement (rails)

Un **rail account** est une manière d'être payé en crypto ou par Lightning. Un compte actif est proposé sur chaque nouvelle facture de son organisation (sauf si la facture nomme elle-même ses méthodes). Ouvrez **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** demande d'abord l'organisation et le type.

![Choisir le type](../img/rail-new-choose.png)

| Type | Ce qu'il faut |
|---|---|
| **evm** | Une adresse ; les réseaux à surveiller (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) et les actifs à accepter (ETH, USDC, USDT). |
| **tron** | Une adresse ; USDT. Tout le réseau Tron est surveillé. |
| **solana** | Une adresse ; USDC et USDT. |
| **ton** | Désactivé pour l'instant : il ne peut être enregistré qu'inactif, car on ne peut pas encore prouver qu'un paiement TON est arrivé. |
| **btcpay** | L'`URL` et le `Store ID` de BTCPay, les actifs (BTC, XMR, ZEC) et une politique de vitesse : `MediumSpeed` (1 confirmation, par défaut), `LowMediumSpeed` (2) ou `LowSpeed` (6). `HighSpeed` est refusé. |
| **lightning** | Un backend : `nwc` (Nostr Wallet Connect, par exemple Alby Hub), `lnd`, `cln` ou `lnbits` ; une `URL` pour les trois derniers. Actif BTC. |

![Un nouveau compte EVM](../img/rail-new-evm.png)

![Un nouveau compte Lightning](../img/rail-new-lightning.png)

À savoir :

- **Utilisez des adresses que seul ce système utilise.** Un paiement sur la chaîne est rapproché de sa facture par son **montant** : chaque devis de change demande le montant de base plus quelques unités de « poussière », uniques parmi les devis qui retiennent cette adresse. L'argent envoyé là pour toute autre raison arrive dans Needs a person.
- L'URL d'un canal doit être une adresse `https://` sur un hôte qui ne se résout qu'en adresses publiques ; une adresse de bouclage ou privée est refusée (seule exception : un LND ou CLN sur la même machine, sur `LOCAL_LIGHTNING_PORT`).
- Tant qu'un client peut encore payer un devis de change, l'organisation, le type, l'adresse, les réseaux et les actifs du compte ne peuvent pas changer ; créez un nouveau compte. **Deactivate** cesse de le proposer, mais les devis déjà affichés restent surveillés jusqu'à leur libération.
- La colonne *Watcher* indique si le worker peut lire cette chaîne ou cette passerelle.

![Un compte BTCPay](../img/rail-edit-btcpay.png)

### Identifiants

Les secrets de BTCPay et de Lightning sont chiffrés avec la clé du coffre et **ne sont plus jamais affichés**. Ouvrez le compte et appuyez sur **Set credentials** (le coffre doit être déverrouillé), ou utilisez dans un shell `manage.py set_rail_credentials <id du compte rail>`. Un champ laissé vide conserve la valeur enregistrée.

Le système **ne dépense jamais**, il ne stocke donc qu'un identifiant dont on peut prouver qu'il ne le peut pas :

| Backend | Ce qu'il faut coller |
|---|---|
| BTCPay | Une `api_key` avec seulement `btcpay.store.canviewinvoices` et `btcpay.store.cancreateinvoice` (BTCPay est interrogé sur ce que la clé peut faire) et le `webhook_secret`. Le bouton **Register the BTCPay webhook** crée le webhook dans BTCPay et stocke son secret pour vous. |
| Lightning, NWC | L'`nwc_url` d'une connexion avec `make_invoice` et `lookup_invoice` et **sans** méthode `pay_*`. |
| Lightning, LND | Un `macaroon` créé avec `invoices:read invoices:write info:read` (le `invoice.macaroon` propre à LND est refusé). |
| Lightning, Core Lightning | Un `rune` limité à `invoice` et `listinvoices`. |
| Lightning, LNbits | L'**Invoice/read key** du portefeuille, jamais l'Admin key ; cochez la case indiquant que vous l'avez vérifié. |

![Identifiants d'un compte BTCPay](../img/rail-credentials-btcpay.png)

![Identifiants d'un compte Lightning](../img/rail-credentials-lightning.png)

Changer l'hôte de l'URL d'un canal efface ses identifiants enregistrés et le désactive, de sorte qu'une clé n'est jamais envoyée à un autre service. Le README ([Crypto rails](../../../README.md#crypto-rails)) donne les détails par backend, les variables d'environnement pour vos propres points d'accès RPC et ce que les watchers ne voient pas.

## Jetons d'API et scopes

**Settings > API tokens**. Un jeton appartient à **une organisation** et ne voit que ses données. Il est affiché **une seule fois**, à sa création ; s'il est perdu, révoquez-le et créez-en un nouveau. La révocation est définitive et la ligne reste.

![Jetons](../img/tokens.png)

| Scope | Autorise |
|---|---|
| `read` | Tout GET. |
| `invoices:write` | Créer et mettre à jour des contacts et leurs clés de chiffrement ; créer, envoyer et annuler des factures ; réémettre les liens ; gérer les liens de paiement. |
| `payments:confirm` | Confirmer, rejeter, annuler et affecter des paiements, acquitter des remboursements, écarter des déclarations. **Cela fait évoluer l'état de l'argent.** Il ne peut pas enregistrer un paiement manuel : seule une personne le peut. |
| `admin` | Tout ce qui précède, et les points d'accès des webhooks. |

Donnez à un jeton le plus petit ensemble nécessaire ; une intégration qui ne fait que créer et envoyer des factures a besoin de `read` et `invoices:write`. Créer, redéfinir les scopes et révoquer sont des changements soumis à reconfirmation.

![Un jeton, affiché une seule fois](../img/token-created.png)

Vous pouvez aussi en émettre un depuis un shell : `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooks

**Settings > Webhooks** enregistre un point d'accès qui est informé de ce qui arrive aux factures et aux paiements d'une organisation.

![Webhooks](../img/webhooks.png)

- L'adresse doit être en `https://`, sur le port 443, sur un hôte qui ne se résout qu'en adresses publiques. (Un environnement de développement peut assouplir cela avec `WEBHOOK_ALLOW_HTTP` et `WEBHOOK_ALLOW_PRIVATE`.)
- Cochez les événements voulus ; n'en cocher aucun signifie tous : `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert` et `ping`.
- Le **secret** est affiché une seule fois. Chaque livraison est un `POST` signé avec `X-Pay-Signature: sha256=<hex>`, un HMAC-SHA256 du corps brut avec ce secret ; vérifiez-le avant de faire confiance à l'appel. Le corps ne porte que des identifiants et des statuts, jamais de noms, d'adresses ni de montants : lisez le reste par l'API.
- Une livraison est retentée après 2, 5, 15, 30, 60, 120, 240 et 480 minutes, puis marquée en échec ; le personnel est prévenu une fois et elle figure sous **Webhooks not delivered** dans Needs a person.
- La page d'un point d'accès offre **Send a ping**, les dernières livraisons et **Delete**. Settings > Webhook deliveries liste chaque livraison avec **Retry now**.

![Un point d'accès et ses livraisons](../img/webhook-detail.png)

La liste complète des événements et le code de vérification se trouvent dans `/llms.txt` de votre installation (chapitre 9).

## L'équipe

**Settings > Team** liste toutes les personnes qui peuvent se connecter, avec le nom de leurs seconds facteurs et leur dernière connexion. Un super-utilisateur peut supprimer le facteur perdu d'un collègue (un changement soumis à reconfirmation, signalé par e-mail à chaque super-utilisateur) ; le collègue est alors bloqué jusqu'à ce que quelqu'un enregistre un nouvel authentificateur sur le serveur. Votre propre dernier facteur ne peut pas être supprimé.

![L'équipe](../img/team.png)
