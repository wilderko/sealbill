# Manuel d'utilisation de Sealbill

Sealbill est un système de facturation auto-hébergé pour celles et ceux qui acceptent à la fois cryptomonnaies, virements bancaires et liens de paiement, et qui veulent que les données des clients soient chiffrées au repos. Ce manuel s'adresse aux personnes qui l'exploitent : l'équipe qui émet les factures et la personne qui l'installe.

Langues : [English](../en/README.md) · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) · [Español](../es/README.md) · **Français** · [Polski](../pl/README.md) · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Chapitres

1. [Prise en main](01-getting-started.md) : installation, le coffre, connexion, la reconfirmation pour les changements sensibles
2. [Paramètres](02-settings.md) : organisations, comptes bancaires, canaux de paiement (rails), jetons d'API, webhooks, l'équipe
3. [Contacts](03-contacts.md), y compris les clés de chiffrement par contact
4. [Factures](04-invoices.md) : créer, envoyer, les pages du client, devis de change, paiements partiels et trop-perçus, annuler, nouveaux liens, le rappel
5. [Liens de paiement](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (ce qui demande une personne)
7. [Migration depuis Zaprite et Request Finance](07-migration.md)
8. [Exports et devise comptable](08-exports.md)
9. [L'API en bref](09-api.md)

## Comment lire les captures d'écran

- Les **pages du personnel** (tout ce qui se trouve derrière la connexion) sont toujours en anglais, quelle que soit la langue du client. Les noms de menus et de boutons sont écrits dans ce manuel exactement comme l'application les affiche.
- Les **pages du client** (la page de facture `/i/…`, la page de paiement `/p/…`, les pages des liens de paiement, la page de déverrouillage et les e-mails) sont traduites en en, sk, cs, de, es, fr, pl, ko et ru. Le client voit la langue du contact, puis celle de l'organisation, puis celle de son navigateur ; ajoutez `?lang=de` à un lien client pour le voir dans une autre langue.
- Tous les noms, adresses e-mail, portefeuilles et montants des captures sont fictifs : ils proviennent d'une organisation de démonstration, « Example Studio s.r.o. », sur une base de données jetable.
- Le texte en `code` est ce que vous tapez ou copiez. Les commandes s'écrivent `manage.py …` ; avec Docker Compose, c'est `docker compose exec web python manage.py …`, et sous systemd `payinv-manage …` (voir le README du dépôt).
