# Podręcznik użytkownika Sealbill

Sealbill to samodzielnie hostowany system fakturowania dla tych, którzy przyjmują kryptowaluty, przelewy bankowe i linki do płatności obok siebie i chcą, aby dane klientów były zaszyfrowane w spoczynku. Ten podręcznik jest dla osób, które go obsługują: pracowników wystawiających faktury i osoby, która go instaluje.

Języki: [English](../en/README.md) · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) · [Español](../es/README.md) · [Français](../fr/README.md) · **Polski** · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Rozdziały

1. [Pierwsze kroki](01-getting-started.md): instalacja, sejf, logowanie, ponowne potwierdzenie przy wrażliwych zmianach
2. [Ustawienia](02-settings.md): organizacje, konta bankowe, kanały płatności (rails), tokeny API, webhooki, zespół
3. [Kontakty](03-contacts.md), w tym klucze szyfrujące poszczególnych kontaktów
4. [Faktury](04-invoices.md): tworzenie, wysyłanie, strony klienta, oferty kursowe, płatności częściowe i nadpłaty, anulowanie, nowe linki, przypomnienie
5. [Linki do płatności](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (co wymaga człowieka)
7. [Migracja z Zaprite i Request Finance](07-migration.md)
8. [Eksporty i waluta księgowa](08-exports.md)
9. [API w skrócie](09-api.md)

## Jak czytać zrzuty ekranu

- **Strony dla pracowników** (wszystko za logowaniem) są zawsze po angielsku, niezależnie od języka klienta. Nazwy menu i przycisków są w tym podręczniku zapisane dokładnie tak, jak pokazuje je aplikacja.
- **Strony dla klienta** (strona faktury `/i/…`, strona płatności `/p/…`, strony linków do płatności, strona odblokowania i e-maile) są przetłumaczone na en, sk, cs, de, es, fr, pl, ko i ru. Klient widzi język kontaktu, potem organizacji, potem swojej przeglądarki; dodanie `?lang=de` do linku klienta pokaże go w innym języku.
- Wszystkie nazwiska, adresy e-mail, portfele i kwoty na zrzutach są zmyślone: pochodzą z demonstracyjnej organizacji „Example Studio s.r.o." na jednorazowej bazie danych.
- Tekst w `kodzie` to to, co wpisujesz lub kopiujesz. Polecenia zapisano jako `manage.py …`; w Docker Compose jest to `docker compose exec web python manage.py …`, a pod systemd `payinv-manage …` (zob. README repozytorium).
