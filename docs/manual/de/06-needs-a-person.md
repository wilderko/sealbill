# 6. Needs a person

**Needs a person** ist die erste Seite und die, auf die man ein Auge haben sollte. Sie listet auf, was kein Programm allein entscheiden soll. Die Leiste zeigt, wie viele Einträge offen sind; gibt es nichts zu tun, sagt die Seite das.

![Die Warteschlangen mit Einträgen](../img/needs-person.png)

Die Warteschlangen werden nur gezeigt, wenn sie Einträge haben; **Show N queues** listet auch die leeren auf, damit Sie sehen, was die Seite überwacht.

![Alle Warteschlangen](../img/needs-person-queues.png)

| Warteschlange | Was es ist und was zu tun ist |
|---|---|
| **Declared payments** | Ein Kunde hat bei einer Rechnung „Ich habe bezahlt" gedrückt. Prüfen Sie Ihre Bank oder die Chain. Die Zahlung auf der Rechnung zu erfassen erledigt die Erklärung; **Dismiss declaration** auf der Rechnung schließt eine falsche. |
| **Payments to review** | Geld, das zu keiner Rechnung oder schlecht passte, mit Grund: Der Token wird nicht akzeptiert, keine offene Rechnung passt, mehr als eine könnte passen, es kam nach Ablauf des Angebots, ein Duplikat, weit über dem Geschuldeten, die Rechnung war schon geschlossen, eine sehr kleine „Dust"-Überweisung (wohl Address-Poisoning-Spam, eingeklappt), eine verworfene Transaktion oder eine BTCPay-Meldung über eine teilweise, späte, ungültige oder von Hand markierte Rechnung. **Confirm** bucht sie auf ihre Rechnung, **Reject** mit Notiz, oder **Assign to an invoice** (Rechnungsnummer, Betrag und eine Notiz). Mit einer Chain-Überweisung kann man handeln, sobald sie final ist. |
| **Waiting for staff confirmation** | Monero- und Zcash-Zahlungen (über BTCPay) und von Hand erfasstes Geld: Nichts überwacht sie, prüfen Sie also die Wallet oder die Bank, dann **Confirm** oder **Reject**. Nur ein Mensch darf bestätigen; kein API-Token. |
| **Gateway payments seen but not settled** | BTCPay- oder Lightning-Zahlungen, die das Gateway vor mehr als sechs Stunden gesehen und nicht abgeschlossen hat. |
| **Refunds due** | Eine Krypto-Zahlung über ihrem Kursangebot. Schicken Sie die Differenz an die angegebene Adresse zurück, dann **Refunded** mit einer Notiz. |
| **Confirmed payments the gateway now calls invalid** | BTCPay hat sie abgeschlossen und dann für ungültig erklärt (Double Spend, Reorg). Nichts wird per Code rückgängig gemacht: Machen Sie die Zahlung auf ihrer Rechnung rückgängig oder behalten Sie sie. |
| **Exchange rates refused** | Ein Fiat-Teil der Kurse ist veraltet oder gesprungen. Prüfen Sie den EZB-Kurs; **accept** einen Sprung, der echt ist. Krypto-Angebote in dieser Währung werden inzwischen abgelehnt. |
| **Watchers that could not look** | Ein Chain- oder Gateway-Leser fällt seit zehn Minuten oder länger aus (die Mitarbeitenden wurden auch per E-Mail informiert). |
| **Transfers seen but not recorded** | Eine Überweisung, die ein Watcher gesehen hat, ließ sich noch nicht erfassen; sie wird bei jedem Durchgang erneut versucht. |
| **Blocks not scanned, or to check** | Eine Blockstrecke wurde übersprungen. Prüfen Sie sie von Hand, dann **Acknowledge** mit dem, was Sie geprüft haben. |
| **Overpaid invoices** | Außerhalb des Systems erstatten oder **Keep the overpayment** (Kapitel 4). |
| **US tax residents with invoices from another issuer** | Rechnung stornieren und neu erstellen; die US-Organisation stellt sie dann aus. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Kapitel 3. |
| **E-mail not delivered** | Eine Nachricht, die etwa sechzehn Stunden lang scheiterte. **Retry**, oder **Dismiss** mit Grund (sie ist dann endgültig abgebrochen). |
| **Payment links** | Eine bezahlte Bestellung hat das Limit eines Links überschritten, der Issuer bietet keine der Methoden eines Links an, eine eingetippte US-Antwort widerspricht der gespeicherten, oder keine Organisation ist der US-Issuer. Drücken Sie **Resolved** mit dem, was Sie geprüft haben. |
| **Webhooks not delivered** | Eine Auslieferung, die nach allen Wiederholungen scheiterte. **Retry** oder **Dismiss** mit Grund. |

Zwei Regeln gelten auf der ganzen Seite:

- Eine Zahlung, die **auf einen Menschen wartet**, wird nur von angemeldeten Mitarbeitenden bestätigt oder zugeordnet. Ein Token, auch ein `admin`-Token, bekommt `409 needs_person`, und nichts ändert sich. Auch eine manuelle Zahlung zu erfassen ist Sache eines Menschen.
- Jede Aktion verlangt eine Notiz, wo es darauf ankommt, und jede Aktion steht im Audit-Log.

Dieselben Warteschlangen lassen sich über die API mit `payments:confirm` bearbeiten, außer den nur für Menschen bestimmten (Kapitel 9). Die beiden Listen, mit denen Sie herausfinden, *warum* etwas hängt, sind **Settings > E-mail outbox** (jeder Brief mit Status, Versuchen und Fehlergrund; Empfänger und Inhalte werden nie gezeigt) und **Settings > Webhook deliveries**.

![Der E-Mail-Postausgang](../img/outbox.png)

![Webhook-Auslieferungen](../img/deliveries.png)
