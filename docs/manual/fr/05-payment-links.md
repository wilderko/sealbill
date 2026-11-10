# 5. Liens de paiement

Un **lien de paiement** est une page, `/l/<token>/`, pour un service à prix fixe (ou à un montant que l'acheteur choisit), payé par beaucoup de gens : un bouton de boutique, un code QR sur une affiche, un lien dans une lettre d'information. Ouvrez **Payment links** dans la barre.

(Ce ne sont pas les *liens fiat* d'une organisation du chapitre 2, qui ne font que pointer vers Revolut, PayPal et consorts.)

![Liens de paiement](../img/links.png)

## Créer un lien

**New payment link** demande :

- **What is sold** : l'organisation qui vend, le document (*Invoice* ou *Payment request*), un titre et une description montrés aux acheteurs, et une image facultative (PNG, JPEG ou WebP de 1,5 Mo au plus, ré-encodée ; 960 × 540 convient à la page).
- **Price** : la devise, le prix et la taxe %, **Maximum quantity per order** (0 signifie illimité, jusqu'à 200), ou cochez **The customer chooses the amount** avec un minimum et un maximum facultatifs.
- **What the buyer is asked** : l'adresse e-mail et « Are you a US tax resident? » sont **toujours** demandées ; le nom, l'adresse de facturation, l'adresse de livraison et le téléphone sont off, optional ou required ; jusqu'à cinq questions supplémentaires.
- **Payment methods** : les méthodes par défaut de l'émetteur ou un **ensemble personnalisé**, par exemple « USDC on Arbitrum One », « Bank transfer in EUR » ou « Bitcoin over Lightning ». Un ensemble personnalisé nomme des types de paiement, pas des comptes ; l'émetteur américain propose donc ses propres comptes des mêmes types.
- **Availability** : active, *Valid until*, *Total uses*, **Paid orders need fulfilling** et **E-mail staff for each paid order**, et une note interne que seul le personnel voit.

![Le formulaire de nouveau lien](../img/link-new.png)

Le lien fonctionne dès qu'il est enregistré et actif.

## Le partager

La page du lien montre l'URL avec **Copy**, un **code QR** (PNG ou SVG), une **feuille imprimable** (PDF), le HTML d'un **bouton pour une page web** et **Replace the URL**, qui donne une nouvelle adresse et arrête l'ancienne aussitôt. Toute personne qui a l'URL peut acheter ; ajoutez `?promoCode=CODE` pour pré-remplir un coupon.

![Un lien, avec son code QR, ses coupons et ses commandes](../img/link-detail.png)

**Coupons** : un code, un pourcentage de remise sur la ligne (avant taxe, jamais une commande gratuite), éventuellement un nombre d'utilisations et une date de fin. **Copy link with code** donne une URL qui le pré-remplit.

## Ce que voit l'acheteur

![La page publique d'un lien](../img/link-public.png)

L'acheteur saisit une adresse e-mail, un nom si on le lui demande, répond à la question américaine et passe au paiement. La page où il arrive est la **page de paiement** ordinaire du chapitre 4.

![La page de paiement d'une commande](../img/link-checkout-pay.png)

- Chaque passage en caisse crée une **commande** : une facture ou une demande de paiement à une ligne, numérotée et envoyée aussitôt, avec un instantané figé. **Aucun e-mail n'est envoyé avant le paiement** ; la page de paiement dit de la conserver. La première lettre est le reçu, ou la lettre de paiement partiel, et elle porte les liens de la commande.
- **Résidents fiscaux américains.** La réponse que l'acheteur saisit décide quelle organisation émet la commande : un yes à l'émetteur américain, un no à l'organisation du lien. Ce qu'un contact a en mémoire ne décide jamais, un lien public ne révèle donc rien de ce qui est stocké. Une réponse saisie que l'indicateur enregistré contredit devient un élément de Needs a person, et pour un « no » contre un « yes » enregistré, la commande n'est pas proposée à l'exécution tant qu'une personne n'a pas vérifié auprès de l'acheteur.
- **Un formulaire public ne montre ni ne change jamais un contact.** Une adresse qui correspond à un client enregistré rattache la commande à ce client. Sinon un *link buyer* est créé à partir des données saisies. Les link buyers figurent dans Contacts avec un filtre, aucune recherche ne les trouve et rien ne les réutilise ; une personne peut en faire un client enregistré depuis sa page, après vérification.

  ![Contacts, link buyers seulement](../img/contacts-link-buyers.png)

## Commandes, utilisations et limites

- Un passage en caisse **retient** une des utilisations du lien (et du coupon) pendant 60 minutes ; le paiement la transforme en utilisation. Un paiement qui arrive après la fin de la retenue garde son prix ; si cela fait dépasser sa limite à un lien ou un coupon plafonné, le personnel reçoit un élément dans Needs a person.
- **Les commandes impayées sont annulées au bout de 7 jours.** L'argent qui arrive ensuite va en examen, comme pour toute facture annulée.
- **Traitement.** Avec *Paid orders need fulfilling* activé, les commandes payées sont listées sur la page du lien comme à traiter jusqu'à ce que quelqu'un appuie sur **Mark fulfilled**.
- **Limites** : 5 tentatives de passage en caisse par 10 minutes et par adresse, et 50 passages en caisse terminés par heure et par lien. Aucune page ne peut encadrer un lien.
- Créer ou modifier un lien par l'API relève de `invoices:write` ; l'URL du lien est donnée à la création, à la rotation et à la lecture d'un lien seul.
