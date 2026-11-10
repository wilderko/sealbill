# Sealbill-Benutzerhandbuch

Sealbill ist ein selbst gehostetes Rechnungssystem für alle, die Kryptowährungen, Banküberweisungen und Zahlungslinks nebeneinander annehmen und die Kundendaten im Ruhezustand verschlüsselt halten wollen. Dieses Handbuch richtet sich an die Menschen, die es betreiben: die Mitarbeitenden, die Rechnungen ausstellen, und die Person, die es installiert.

Sprachen: [English](../en/README.md) · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · **Deutsch** · [Español](../es/README.md) · [Français](../fr/README.md) · [Polski](../pl/README.md) · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Kapitel

1. [Erste Schritte](01-getting-started.md): Installation, der Tresor, Anmeldung, die erneute Bestätigung bei sensiblen Änderungen
2. [Einstellungen](02-settings.md): Organisationen, Bankkonten, Zahlungskanäle (Rails), API-Tokens, Webhooks, das Team
3. [Kontakte](03-contacts.md), einschließlich der Verschlüsselungsschlüssel einzelner Kontakte
4. [Rechnungen](04-invoices.md): erstellen, senden, die Kundenseiten, Kursangebote, Teil- und Überzahlungen, stornieren, neue Links, die Erinnerung
5. [Zahlungslinks](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (was einen Menschen braucht)
7. [Migration von Zaprite und Request Finance](07-migration.md)
8. [Exporte und die Buchhaltungswährung](08-exports.md)
9. [Die API in Kürze](09-api.md)

## So lesen Sie die Screenshots

- Die **Mitarbeiterseiten** (alles hinter der Anmeldung) sind immer auf Englisch, egal welche Sprache der Kunde spricht. Menü- und Schaltflächennamen stehen in diesem Handbuch genau so, wie die Anwendung sie zeigt.
- Die **Kundenseiten** (die Rechnungsseite `/i/…`, die Zahlungsseite `/p/…`, die Seiten der Zahlungslinks, die Entsperrseite und die E-Mails) sind in en, sk, cs, de, es, fr, pl, ko und ru übersetzt. Der Kunde sieht die Sprache des Kontakts, dann die der Organisation, dann die seines Browsers; mit `?lang=de` hinter einem Kundenlink sehen Sie ihn in einer anderen Sprache.
- Alle Namen, E-Mail-Adressen, Wallets und Beträge in den Screenshots sind erfunden: Sie stammen aus einer Demo-Organisation „Example Studio s.r.o." auf einer Wegwerf-Datenbank.
- Text in `Code` tippen oder kopieren Sie. Befehle stehen als `manage.py …`; bei Docker Compose heißt das `docker compose exec web python manage.py …`, unter systemd `payinv-manage …` (siehe die README des Repositorys).
