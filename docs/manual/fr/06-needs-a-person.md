# 6. Needs a person

**Needs a person** est la première page, et celle qu'il faut garder à l'œil. Elle liste ce qu'aucun programme ne doit trancher seul. La barre indique combien d'éléments sont ouverts ; s'il n'y a rien à faire, la page le dit.

![Les files qui contiennent quelque chose](../img/needs-person.png)

Les files ne s'affichent que si elles ont des éléments ; **Show N queues** liste aussi les vides, pour que vous voyiez ce que la page surveille.

![Toutes les files](../img/needs-person-queues.png)

| File | Ce que c'est et quoi faire |
|---|---|
| **Declared payments** | Un client a appuyé sur « J'ai payé » sur une facture. Vérifiez votre banque ou la chaîne. Enregistrer le paiement sur la facture règle la déclaration ; **Dismiss declaration** sur la facture clôt une fausse. |
| **Payments to review** | De l'argent qui ne correspondait à aucune facture ou correspondait mal, avec le motif : le jeton n'est pas accepté, aucune facture ouverte ne correspond, plus d'une pourrait correspondre, il est arrivé après l'expiration du devis, un doublon, très au-dessus de ce qui était dû, la facture était déjà close, un très petit virement de « poussière » (probablement du spam d'empoisonnement d'adresse, replié), une transaction abandonnée ou un rapport de BTCPay sur une facture partielle, tardive, invalide ou marquée à la main. **Confirm** l'affecte à sa facture, **Reject** avec une note, ou **Assign to an invoice** (numéro de facture, montant et une note). On peut agir sur un virement de chaîne une fois qu'il est final. |
| **Waiting for staff confirmation** | Paiements Monero et Zcash (via BTCPay) et argent enregistré à la main : rien ne les surveille, vérifiez donc le portefeuille ou la banque, puis **Confirm** ou **Reject**. Seule une personne peut confirmer ; aucun jeton d'API. |
| **Gateway payments seen but not settled** | Des paiements BTCPay ou Lightning que la passerelle a vus il y a plus de six heures et n'a pas réglés. |
| **Refunds due** | Un paiement crypto supérieur à son devis. Renvoyez la différence à l'adresse indiquée, puis **Refunded** avec une note. |
| **Confirmed payments the gateway now calls invalid** | BTCPay l'a réglé puis déclaré invalide (double dépense, réorganisation). Rien n'est annulé par le code : annulez le paiement depuis sa facture, ou conservez-le. |
| **Exchange rates refused** | Une partie fiat des taux est périmée ou a sauté. Vérifiez le taux de la BCE ; **accept** un saut qui est réel. Les devis crypto dans cette devise sont refusés entre-temps. |
| **Watchers that could not look** | Un lecteur de chaîne ou de passerelle échoue depuis dix minutes ou plus (le personnel a aussi reçu un e-mail). |
| **Transfers seen but not recorded** | Un virement qu'un watcher a vu n'a pas encore pu être enregistré ; il est retenté à chaque passage. |
| **Blocks not scanned, or to check** | Une plage de blocs a été sautée. Vérifiez-la à la main, puis **Acknowledge** avec ce que vous avez vérifié. |
| **Overpaid invoices** | Remboursez hors du système ou **Keep the overpayment** (chapitre 4). |
| **US tax residents with invoices from another issuer** | Annulez la facture et recréez-la ; l'organisation américaine l'émettra alors. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Chapitre 3. |
| **E-mail not delivered** | Un message qui a échoué pendant environ seize heures. **Retry**, ou **Dismiss** avec un motif (il est alors annulé définitivement). |
| **Payment links** | Une commande payée a fait dépasser sa limite à un lien, l'émetteur ne propose aucune des méthodes d'un lien, une réponse américaine saisie contredit celle qui est enregistrée, ou aucune organisation n'est l'émetteur américain. Appuyez sur **Resolved** avec ce que vous avez vérifié. |
| **Webhooks not delivered** | Une livraison qui a échoué après toutes ses nouvelles tentatives. **Retry** ou **Dismiss** avec un motif. |

Deux règles valent pour toute la page :

- Un paiement qui **attend une personne** n'est confirmé ou affecté que par un membre du personnel connecté. Un jeton, même `admin`, reçoit `409 needs_person` et rien ne change. Enregistrer un paiement manuel est aussi l'affaire d'une personne.
- Chaque action demande une note là où cela compte, et chaque action figure dans le journal d'audit.

Les mêmes files peuvent être traitées par l'API avec `payments:confirm`, sauf celles réservées aux personnes (chapitre 9). Les deux listes qui servent à comprendre *pourquoi* quelque chose est bloqué sont **Settings > E-mail outbox** (chaque lettre, avec son état, ses tentatives et la raison de l'échec ; les destinataires et les contenus ne sont jamais affichés) et **Settings > Webhook deliveries**.

![La boîte d'envoi des e-mails](../img/outbox.png)

![Livraisons de webhooks](../img/deliveries.png)
