# 7. Migracja z Zaprite i Request Finance

Jeśli wcześniej korzystałeś z Zaprite lub Request Finance, możesz przenieść swoich klientów i historię faktur, aby Sealbill stał się jedynym miejscem, w którym da się znaleźć wszystko. Procedura krok po kroku, razem z tym, jak wydobyć dane z każdej usługi, jest w [docs/migration.md](../../migration.md). Ten rozdział mówi, co import robi, a czego nie.

## Zasada: historia, nie żywe faktury

**Każda zmigrowana faktura jest zapisana jako już opłacona.** Nic, co było gdzie indziej otwarte, nie jest do zapłaty w Sealbill, więc klient nigdy nie zapłaci dwa razy i nikt nie jest kontaktowany. Każda faktura zachowuje oryginalny numer, datę wystawienia, walutę, pozycje, tytuł i stopkę.

- Każda dostaje dokładnie jedną płatność: sposób *Recorded by hand*, potwierdzoną, na całą sumę, z datą zapłaty ze źródła (lub datą wystawienia). Płatność niesie notatkę z nazwą źródła i oryginalnym numerem.
- Status źródłowy (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) jest zachowywany dla pracowników i nigdy nie jest pokazywany klientowi. CSV faktur ma kolumnę **migrated_from** (np. `zaprite:COMPLETE`), aby księgowość mogła odróżnić płatność, którą klient rzeczywiście zrobił, od faktury, którą uznałeś za opłaconą.
- Zmigrowana faktura jest **tylko do odczytu**: nic nie można z niej wysłać, zapłacić, cofnąć, anulować, wydać ponownie linków, wycenić ani usunąć, ani przez pracowników, ani przez API. Jej strona mówi, skąd pochodzi, i prowadzi do oryginału, dopóki to konto istnieje.
- **Nic się nie uruchamia.** Import nie wysyła e-maila, nie emituje webhooka, nie tworzy oferty ani linku i nie przesuwa następnego numeru organizacji.

## Co dzieje się z kontaktami

Kontakty są **łączone po adresie e-mail w obrębie organizacji** (bez rozróżniania wielkości liter): jedna osoba, która występuje w Zaprite i w Request Finance, staje się jednym kontaktem, uzupełnionym ze źródła, które ma dane. Kontakt bez e-maila jest zachowywany i przypisywany tylko do faktur, które na niego wskazują; nigdy nie jest łączony po nazwie. Rekordy z Zaprite i Request Finance trafiają do organizacji, którą wskażesz; wystawca dla USA odrzuca rekordy z Zaprite, chyba że dodasz `--allow-us-issuer`.

## Numery

Własny numer źródła jest zachowywany. Gdy numer już istnieje w organizacji (Zaprite i Request Finance numerowały oba prosto), ten, który przychodzi jako drugi, dostaje dopisany znacznik źródła: `591-RF` (lub `-Z`). Próbny przebieg wymienia każdą kolizję, zanim cokolwiek zostanie zapisane. Numer, który ma kształt własnej numeracji organizacji i nie jest poniżej jej następnego numeru, jest odrzucany i rozstrzyga się go ręcznie.

## Uruchomienie importu

Importer czyta na standardowym wejściu **JSON rozdzielany znakami nowej linii** (newline-delimited): jeden rekord `contact` lub `invoice` na wiersz.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Kwoty są w minor units; `total_minor` musi być równe temu, co wyliczą pozycje (podatek i rabaty na pozycję są przenoszone), w przeciwnym razie rekord jest odrzucany.
- `address` kontaktu ma dokładnie klucze `line1`, `line2`, `city`, `zip`, `state` i `country` (dwuliterowy kod). Każdy inny klucz jest odrzucany z nazwy, aby pole nigdy nie zginęło niezauważone.
- Każda faktura jest zapisywana w stanie końcowym w osobnej transakcji.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

Sejf musi być odblokowany. **Zawsze najpierw uruchom `--dry-run`**: robi wszystko, wycofuje i wypisuje tylko liczby:

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

- Wynik nigdy nie wypisuje nazwiska, adresu, e-maila ani kwoty na klienta. Odrzucony lub pominięty rekord jest nazwany `<source>:<source_id>` i numerem wiersza, abyś znalazł go w eksporcie.
- **Można go uruchomić ponownie.** Faktura lub kontakt, których identyfikator źródłowy już zaimportowano, są pomijane, więc przerwany w połowie przebieg po prostu się powtarza. Dwa jednoczesne przebiegi są odrzucane.
- Rekordy wersji roboczych, anulowanych i odrzuconych faktur z Request Finance są pomijane: nikt ich nie był winien.
- Dziennik audytu ma jeden wpis na przebieg tylko z liczbami i jeden na fakturę ze źródłem i jego identyfikatorem.
- `--btc-check id,id` wypisuje dla wskazanych identyfikatorów źródłowych kwotę w satoshi i URL źródła, aby zweryfikować fakturę w bitcoinach z księgowością.

Po imporcie sprawdź jedną lub dwie zmigrowane faktury na stronie **Invoices** i pobierz CSV faktur za okres (rozdział 8); sumy według waluty wypisane przez próbny przebieg powinny zgadzać się z liczbami źródła.
