# 7. Migration depuis Zaprite et Request Finance

Si vous utilisiez auparavant Zaprite ou Request Finance, vous pouvez apporter vos clients et l'historique de vos factures, pour que Sealbill devienne l'unique endroit où tout se retrouve. La procédure pas à pas, y compris la façon de sortir les données de chaque service, est dans [docs/migration.md](../../migration.md). Ce chapitre dit ce que fait l'import et ce qu'il ne fait pas.

## Le principe : de l'historique, pas des factures vivantes

**Chaque facture migrée est enregistrée comme déjà payée.** Rien de ce qui était ouvert ailleurs n'est payable dans Sealbill, un client ne peut donc jamais payer deux fois et personne n'est contacté. Chaque facture conserve son numéro d'origine, sa date d'émission, sa devise, ses lignes, son titre et sa note de bas de page.

- Chacune reçoit exactement un paiement : le moyen *Recorded by hand*, confirmé, pour tout le total, daté de la date de paiement de la source (ou de la date d'émission). Le paiement porte une note qui nomme la source et le numéro d'origine.
- Le statut d'origine (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) est conservé pour le personnel et n'est jamais montré au client. Le CSV des factures a une colonne **migrated_from** (par exemple `zaprite:COMPLETE`), pour que la comptabilité distingue un paiement qu'un client a réellement fait d'une facture que vous avez déclarée payée.
- Une facture migrée est **en lecture seule** : on ne peut rien en envoyer, payer, annuler, invalider, réémettre, coter ni supprimer, ni par le personnel ni par l'API. Sa page dit d'où elle vient et renvoie vers l'original tant que ce compte existe.
- **Rien ne se déclenche.** L'import n'envoie pas d'e-mail, n'émet pas de webhook, ne crée ni devis ni lien et ne déplace pas le prochain numéro de l'organisation.

## Ce qu'il advient des contacts

Les contacts sont **fusionnés par adresse e-mail au sein d'une organisation** (sans tenir compte de la casse) : une personne qui apparaît dans Zaprite et dans Request Finance devient un seul contact, complété à partir de la source qui a les données. Un contact sans e-mail est conservé et n'est rattaché qu'aux factures qui le désignent ; il n'est jamais fusionné par le nom. Les enregistrements de Zaprite et de Request Finance vont dans l'organisation que vous nommez ; l'émetteur américain refuse les enregistrements de Zaprite sauf si vous ajoutez `--allow-us-issuer`.

## Numéros

Le numéro propre de la source est conservé. Quand le numéro existe déjà dans l'organisation (Zaprite et Request Finance numérotaient tous deux simplement), celui qui arrive en second reçoit l'étiquette de la source : `591-RF` (ou `-Z`). L'exécution à blanc liste chaque collision avant que quoi que ce soit soit écrit. Un numéro qui a la forme de la numérotation propre de l'organisation et n'est pas inférieur à son prochain numéro est refusé, à trancher à la main.

## Lancer l'import

L'importeur lit sur l'entrée standard du **JSON délimité par des sauts de ligne** : un enregistrement `contact` ou `invoice` par ligne.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Les montants sont en minor units ; `total_minor` doit être égal à ce que calculent les lignes (la taxe et les remises par ligne sont reprises), sinon l'enregistrement est refusé.
- L'`address` d'un contact a exactement les clés `line1`, `line2`, `city`, `zip`, `state` et `country` (un code à deux lettres). Toute autre clé est refusée nommément, de sorte qu'un champ ne se perd jamais sans que personne le voie.
- Chaque facture est écrite dans son état final dans sa propre transaction.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

Le coffre doit être déverrouillé. **Lancez toujours d'abord `--dry-run`** : il fait tout, annule tout et n'affiche que des totaux :

```text
DRY RUN: nothing was written (source zaprite, org example-studio)
contacts: new 1, merged 0, without e-mail 0, skipped (already imported) 0
invoices invoice EUR: 1
invoices payment_request USD: 1
total EUR: 133700 minor units
total USD: 5000 minor units
skipped, already imported: 0
source status COMPLETE: 1
source status PAID: 1
number collisions: 0
refused lines: 1
  line 4 zaprite:bad: the lines do not add up to total_minor
```

- La sortie n'affiche jamais un nom, une adresse, un e-mail ni un montant par client. Un enregistrement refusé ou ignoré est nommé `<source>:<source_id>` avec son numéro de ligne, pour que vous le retrouviez dans l'export.
- **Il peut être relancé.** Une facture ou un contact dont l'id source a déjà été importé est ignoré, une exécution interrompue à mi-chemin se relance donc simplement. Deux exécutions simultanées sont refusées.
- Les enregistrements de brouillons, de factures annulées et rejetées de Request Finance sont ignorés : personne ne les devait.
- Le journal d'audit a une entrée par exécution avec des totaux seulement, et une par facture avec la source et son id.
- `--btc-check id,id` affiche, pour les ids source que vous nommez, le montant en satoshis et l'URL de la source, pour recouper une facture bitcoin avec la comptabilité.

Après l'import, vérifiez une ou deux factures migrées dans la page **Invoices** et téléchargez le CSV des factures de la période (chapitre 8) ; les totaux par devise affichés par l'exécution à blanc devraient correspondre aux chiffres de la source.
