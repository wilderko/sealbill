# 7. Migration von Zaprite und Request Finance

Wenn Sie früher Zaprite oder Request Finance genutzt haben, können Sie Ihre Kunden und Ihre Rechnungshistorie übernehmen, damit Sealbill der eine Ort wird, an dem alles zu finden ist. Das Verfahren Schritt für Schritt, einschließlich der Frage, wie die Daten aus dem jeweiligen Dienst herauskommen, steht in [docs/migration.md](../../migration.md). Dieses Kapitel sagt, was der Import tut und was nicht.

## Das Prinzip: Historie, keine lebenden Rechnungen

**Jede migrierte Rechnung wird als bereits bezahlt erfasst.** Nichts, was anderswo offen war, ist in Sealbill zahlbar, ein Kunde kann also nie doppelt zahlen, und niemand wird angeschrieben. Jede Rechnung behält ihre ursprüngliche Nummer, ihr Ausstellungsdatum, ihre Währung, ihre Positionen, ihren Titel und die Fußnote.

- Jede bekommt genau eine Zahlung: die Art *Recorded by hand*, bestätigt, über die gesamte Summe, datiert auf das Zahlungsdatum der Quelle (oder das Ausstellungsdatum). Die Zahlung trägt eine Notiz mit der Quelle und der ursprünglichen Nummer.
- Der Quellstatus (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) wird für die Mitarbeitenden aufbewahrt und dem Kunden nie gezeigt. Die Rechnungs-CSV hat eine Spalte **migrated_from** (zum Beispiel `zaprite:COMPLETE`), damit die Buchhaltung eine Zahlung, die ein Kunde wirklich geleistet hat, von einer Rechnung unterscheiden kann, die Sie für bezahlt erklärt haben.
- Eine migrierte Rechnung ist **schreibgeschützt**: Nichts lässt sich von ihr senden, bezahlen, rückgängig machen, stornieren, neu verlinken, mit Angebot versehen oder löschen, weder durch Mitarbeitende noch über die API. Ihre Seite sagt, woher sie stammt, und verlinkt auf das Original, solange es dieses Konto gibt.
- **Nichts löst aus.** Der Import sendet keine E-Mail, löst keinen Webhook aus, erstellt kein Angebot und keinen Link und verschiebt die nächste Nummer der Organisation nicht.

## Was mit den Kontakten geschieht

Kontakte werden **innerhalb einer Organisation nach E-Mail-Adresse zusammengeführt** (ohne Beachtung der Groß- und Kleinschreibung): Eine Person, die in Zaprite und in Request Finance vorkommt, wird ein Kontakt, ergänzt aus der Quelle, die die Daten hat. Ein Kontakt ohne E-Mail bleibt erhalten und wird nur den Rechnungen zugeordnet, die auf ihn verweisen; er wird nie nach Namen zusammengeführt. Datensätze aus Zaprite und Request Finance gehen in die Organisation, die Sie nennen; der US-Issuer lehnt Zaprite-Datensätze ab, es sei denn, Sie fügen `--allow-us-issuer` hinzu.

## Nummern

Die eigene Nummer der Quelle bleibt erhalten. Existiert die Nummer in der Organisation schon (Zaprite und Request Finance haben beide schlicht nummeriert), bekommt die, die als zweite eintrifft, das Kürzel der Quelle angehängt: `591-RF` (oder `-Z`). Der Probelauf listet jede Kollision auf, bevor etwas geschrieben wird. Eine Nummer, die wie die eigene Nummerierung der Organisation aussieht und nicht unter deren nächster Nummer liegt, wird abgelehnt und von Hand entschieden.

## Den Import ausführen

Der Importer liest auf der Standardeingabe **zeilenweises JSON** (newline-delimited): pro Zeile ein Datensatz `contact` oder `invoice`.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Beträge stehen in minor units; `total_minor` muss dem entsprechen, was die Positionen ergeben (Steuer und Rabatte je Position werden übernommen), sonst wird der Datensatz abgelehnt.
- Die `address` eines Kontakts hat genau die Schlüssel `line1`, `line2`, `city`, `zip`, `state` und `country` (ein zweibuchstabiger Code). Jeder andere Schlüssel wird namentlich abgelehnt, damit nie ein Feld unbemerkt verloren geht.
- Jede Rechnung wird im Endzustand in einer eigenen Transaktion geschrieben.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

Der Tresor muss entsperrt sein. **Führen Sie immer zuerst `--dry-run` aus**: Er tut alles, macht es rückgängig und gibt nur Zahlen aus:

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

- Die Ausgabe nennt nie einen Namen, eine Adresse, eine E-Mail oder einen Betrag pro Kunde. Ein abgelehnter oder übersprungener Datensatz heißt `<source>:<source_id>` mit seiner Zeilennummer, damit Sie ihn im Export wiederfinden.
- **Er lässt sich erneut ausführen.** Eine Rechnung oder ein Kontakt, deren Quell-ID schon importiert wurde, wird übersprungen; ein auf halbem Weg abgebrochener Lauf wird also einfach wiederholt. Zwei Läufe gleichzeitig werden abgelehnt.
- Datensätze von Entwürfen, stornierten und abgelehnten Rechnungen aus Request Finance werden übersprungen: Niemand schuldete sie.
- Das Audit-Log hat einen Eintrag pro Lauf nur mit Zahlen und einen pro Rechnung mit Quelle und ihrer ID.
- `--btc-check id,id` gibt für die genannten Quell-IDs den Satoshi-Betrag und die Quell-URL aus, um eine Bitcoin-Rechnung gegen die Buchhaltung zu prüfen.

Prüfen Sie nach dem Import ein oder zwei migrierte Rechnungen auf der Seite **Invoices** und laden Sie die Rechnungs-CSV für den Zeitraum herunter (Kapitel 8); die Summen je Währung aus dem Probelauf sollten mit den Zahlen der Quelle übereinstimmen.
