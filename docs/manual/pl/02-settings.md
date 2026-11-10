# 2. Ustawienia

**Settings** to ostatnia pozycja paska. Jego strona główna wymienia wszystko poniżej, a do tego dziennik audytu, skrzynkę nadawczą e-maili (E-mail outbox) i dostarczenia webhooków (używa ich rozdział 6).

![Strona główna Settings](../img/settings-index.png)

Każda zmiana trafia do **Audit log** (Settings > Audit log): kto, co i który rekord, nigdy wpisane wartości.

![Dziennik audytu](../img/audit.png)

Otwórz wpis, aby przeczytać szczegóły. Zmiany wymienione w rozdziale 1 w części „Ponowne potwierdzenie" wymagają superużytkownika ze świeżym drugim składnikiem.

## Organizacje

**Organizacja** to podmiot prawny, który wystawia faktury: jego nazwa, adres, identyfikatory podatkowe, logo, numeracja faktur i należące do niego konta płatnicze. Druga organizacja przydaje się, gdy jedną instalację dzielą dwie firmy, np. jedna w UE i jedna w USA.

![Lista organizacji](../img/orgs-list.png)

**Settings > Organizations > New organization**, albo otwórz istniejącą, aby ją edytować:

| Pole | Co robi |
|---|---|
| Slug | Krótka nazwa używana w API i w poleceniach (`example-studio`). |
| Legal name, Address, Tax ids | Drukowane w bloku „Sprzedawca" na fakturze. Identyfikatory podatkowe to dowolny tekst. |
| Email from | Nadawca e-maili organizacji, np. `Example Studio <billing@example.com>`. |
| Default language | Język stron klienta i e-maili, gdy kontakt nie ma preferowanego języka. |
| Footer note, Logo | Drukowane na fakturze i w PDF. Logo to PNG lub JPEG do 1,9 MB, zapisane jako PNG bez metadanych. |
| Number format | W stylu Pythona, z `{year}` i `{seq}`: `{year}-{seq:04d}` daje `2026-0001`. Musi zawierać `{seq}`. |
| Next number | Ustawiany przez wystawianie faktur; nie można go edytować, bo numery zostałyby nadane ponownie. |
| Underpayment allowance | Płatność krypto, której brakuje najwyżej **mniejszej** z dwóch wartości: tego udziału (punkty bazowe; 50 to 0,5 %) i tej stałej kwoty w dolarach amerykańskich (domyślnie 1,00), nadal rozlicza ofertę kursową. Pochłania opłatę giełdy za wypłatę. |
| Overpayment ceiling | Płatność krypto, która przekracza kwotę z oferty kursowej o więcej niż ten udział (1000 to 10 %), nie jest rozliczana, lecz odkładana dla człowieka. |
| Accounting currency | Waluta kolumn księgowych w eksportach (rozdział 8). |
| Hosts a Back button may lead to | Jeden host na wiersz. Faktura utworzona przez API może mieć `return_url` dokładnie na te hosty. |

![Organizacja](../img/org-edit.png)

### Wystawca dla amerykańskich rezydentów

Zaznacz **US-resident issuer** najwyżej w jednej organizacji. To organizacja, która musi wystawiać faktury klientom będącym amerykańskimi rezydentami podatkowymi.

- Każdy kontakt ma odpowiedź **US tax resident**: yes, no lub unknown. Formularz faktury zmusza do wyboru; link do płatności pyta kupującego.
- Gdy inna organizacja zamawia fakturę dla kontaktu będącego amerykańskim rezydentem podatkowym, fakturę **wystawia organizacja-wystawca**, ze swoją numeracją, kontami bankowymi i kanałami. Wystawca prowadzi własną kopię kontaktu; Twój kontakt nigdy nie jest nadpisywany.
- Jeśli kontakt stanie się amerykańskim rezydentem podatkowym po wystawieniu faktury przez inną organizację, faktura trafia do kolejki **US tax residents with invoices from another issuer** w Needs a person: anuluj ją i utwórz ponownie.
- Gdy żadna organizacja nie jest oznaczona, takiej faktury w ogóle nie można utworzyć.

Zmiana znacznika lub hostów powrotu to zmiana z ponownym potwierdzeniem. Znacznik jest w dzienniku audytu i trafia e-mailem do superużytkowników.

## Konta bankowe i fiat linki

Na dole organizacji są dwie listy. Obie są **dezaktywowane, nigdy usuwane**: wystawiona faktura nadal pokazuje konto, które wskazywała.

![Konta bankowe i linki do płatności organizacji](../img/org-accounts.png)

**Add bank account** prosi o etykietę, walutę, beneficjenta, IBAN lub numer konta, bank, jego SWIFT/BIC i opcjonalnie bank pośredniczący. Konto bankowe jest oferowane na fakturach **w swojej walucie**. Z numerem IBAN strona płatności i faktura PDF pokazują też bankowe kody QR: w EUR dla IBAN kraju SEPA, w CZK dla czeskiego konta (rozdział 4). *Instrukcje płatności* istnieją po jednej na język: strona płatności pokazuje tę w języku czytelnika i wraca do angielskiej; zostaw język pusty, aby go pominąć.

![Nowe konto bankowe](../img/bank-new.png)

**Add payment link** (*fiat link*, nie mylić z udostępnialnymi stronami z rozdziału 5) to link do usługi, która przyjmuje płatność gdzie indziej: Revolut, N26, PayPal, Wise lub „other". Wymaga adresu `https://` i walut, które może przyjąć. Strona płatności pokazuje go w zakładce **Płatność online**.

![Nowy fiat link](../img/fiat-new.png)

## Kanały płatności (rails)

**Rail account** to jeden ze sposobów otrzymania zapłaty w krypto lub przez Lightning. Aktywne konto jest oferowane na każdej nowej fakturze swojej organizacji (chyba że faktura sama nazwie swoje metody). Otwórz **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** najpierw pyta o organizację i rodzaj.

![Wybór rodzaju](../img/rail-new-choose.png)

| Rodzaj | Czego wymaga |
|---|---|
| **evm** | Jednego adresu; sieci do obserwowania (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) i aktywów do przyjmowania (ETH, USDC, USDT). |
| **tron** | Jednego adresu; USDT. Obserwowana jest cała sieć Tron. |
| **solana** | Jednego adresu; USDC i USDT. |
| **ton** | Na razie wyłączone: można je zapisać tylko jako nieaktywne, bo nie da się jeszcze udowodnić, że płatność przez TON dotarła. |
| **btcpay** | `URL` i `Store ID` BTCPay, aktywa (BTC, XMR, ZEC) i polityki szybkości: `MediumSpeed` (1 potwierdzenie, domyślnie), `LowMediumSpeed` (2) lub `LowSpeed` (6). `HighSpeed` jest odrzucane. |
| **lightning** | Backendu: `nwc` (Nostr Wallet Connect, np. Alby Hub), `lnd`, `cln` lub `lnbits`; dla trzech ostatnich także `URL`. Aktywo BTC. |

![Nowe konto EVM](../img/rail-new-evm.png)

![Nowe konto Lightning](../img/rail-new-lightning.png)

Warto wiedzieć:

- **Używaj adresów, z których korzysta tylko ten system.** Płatność w łańcuchu jest dopasowywana do faktury po **kwocie**: każda oferta kursowa żąda kwoty podstawowej plus kilku jednostek „pyłu", unikalnych wśród ofert zajmujących ten adres. Pieniądze wysłane tam z innego powodu trafiają do Needs a person.
- URL kanału musi być adresem `https://` na hoście, który rozwiązuje się wyłącznie na adresy publiczne; adres pętli zwrotnej lub prywatny jest odrzucany (jedyny wyjątek to LND lub CLN na tej samej maszynie, na `LOCAL_LIGHTNING_PORT`).
- Dopóki klient może jeszcze zapłacić ofertę, nie można zmienić organizacji, rodzaju, adresu, sieci ani aktywów konta; utwórz zamiast tego nowe konto. **Deactivate** przestaje je oferować, ale oferty już pokazane są obserwowane do czasu zwolnienia.
- Kolumna *Watcher* mówi, czy worker potrafi odczytać dany łańcuch lub bramkę.

![Konto BTCPay](../img/rail-edit-btcpay.png)

### Dane dostępowe

Sekrety BTCPay i Lightning są szyfrowane kluczem sejfu i **nigdy więcej nie są pokazywane**. Otwórz konto i naciśnij **Set credentials** (sejf musi być odblokowany) albo użyj w powłoce `manage.py set_rail_credentials <id konta rail>`. Puste pole zachowuje zapisaną wartość.

System **nigdy nie wydaje**, więc zapisuje tylko dane, o których da się dowieść, że tego nie potrafią:

| Backend | Co wkleić |
|---|---|
| BTCPay | `api_key` tylko z `btcpay.store.canviewinvoices` i `btcpay.store.cancreateinvoice` (BTCPay jest pytany, co klucz może) oraz `webhook_secret`. Przycisk **Register the BTCPay webhook** tworzy webhook w BTCPay i zapisuje jego sekret za Ciebie. |
| Lightning, NWC | `nwc_url` połączenia z `make_invoice` i `lookup_invoice` i **bez** metody `pay_*`. |
| Lightning, LND | `macaroon` wygenerowany z `invoices:read invoices:write info:read` (własny `invoice.macaroon` LND jest odrzucany). |
| Lightning, Core Lightning | `rune` ograniczony do `invoice` i `listinvoices`. |
| Lightning, LNbits | **Invoice/read key** portfela, nigdy Admin key; zaznacz pole, że to sprawdzono. |

![Dane dostępowe BTCPay](../img/rail-credentials-btcpay.png)

![Dane dostępowe Lightning](../img/rail-credentials-lightning.png)

Zmiana hosta w URL kanału czyści jego zapisane dane i go dezaktywuje, aby klucz nigdy nie trafił do innej usługi. README ([Crypto rails](../../../README.md#crypto-rails)) zawiera szczegóły dla każdego backendu, zmienne środowiskowe dla własnych punktów RPC i to, czego watchery nie widzą.

## Tokeny API i zakresy (scopes)

**Settings > API tokens**. Token należy do **jednej organizacji** i widzi tylko jej dane. Jest pokazywany **raz**, przy utworzeniu; jeśli zostanie zgubiony, unieważnij go i utwórz nowy. Unieważnienie jest trwałe, a wiersz zostaje.

![Tokeny](../img/tokens.png)

| Scope | Pozwala |
|---|---|
| `read` | Każde GET. |
| `invoices:write` | Tworzyć i aktualizować kontakty oraz ich klucze szyfrujące; tworzyć, wysyłać i anulować faktury; wydawać nowe linki; zarządzać linkami do płatności. |
| `payments:confirm` | Potwierdzać, odrzucać, cofać i przypisywać płatności, potwierdzać zwroty, odrzucać zgłoszenia. **To zmienia stan pieniędzy.** Nie może zarejestrować płatności ręcznej: może to zrobić tylko człowiek. |
| `admin` | Wszystko powyżej oraz punkty końcowe webhooków. |

Daj tokenowi najmniejszy potrzebny zestaw; integracja, która tylko tworzy i wysyła faktury, potrzebuje `read` i `invoices:write`. Tworzenie, zmiana zakresów i unieważnianie to zmiany z ponownym potwierdzeniem.

![Token, pokazany raz](../img/token-created.png)

Token można też wydać z powłoki: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooki

**Settings > Webhooks** rejestruje punkt końcowy, który jest powiadamiany o tym, co dzieje się z fakturami i płatnościami organizacji.

![Webhooki](../img/webhooks.png)

- Adres musi być `https://`, na porcie 443, na hoście rozwiązującym się wyłącznie na adresy publiczne. (Środowisko deweloperskie może to poluzować przez `WEBHOOK_ALLOW_HTTP` i `WEBHOOK_ALLOW_PRIVATE`.)
- Zaznacz potrzebne zdarzenia; brak zaznaczenia oznacza wszystkie: `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert` i `ping`.
- **Sekret** jest pokazywany raz. Każde dostarczenie to podpisany `POST` z `X-Pay-Signature: sha256=<hex>`, czyli HMAC-SHA256 surowej treści tym sekretem; sprawdź go, zanim zaufasz wywołaniu. Treść niesie tylko identyfikatory i statusy, nigdy nazwisk, adresów ani kwot: resztę odczytaj przez API.
- Dostarczenie jest ponawiane po 2, 5, 15, 30, 60, 120, 240 i 480 minutach, potem oznaczane jako nieudane; pracownicy dowiadują się o tym raz, a ono trafia do **Webhooks not delivered** w Needs a person.
- Strona punktu końcowego ma **Send a ping**, ostatnie dostarczenia i **Delete**. Settings > Webhook deliveries wymienia każde dostarczenie z **Retry now**.

![Punkt końcowy i jego dostarczenia](../img/webhook-detail.png)

Pełna lista zdarzeń i kod weryfikacji są w `/llms.txt` Twojej instalacji (rozdział 9).

## Zespół

**Settings > Team** wymienia wszystkich, którzy mogą się zalogować, z nazwami ich drugich składników i ostatnim logowaniem. Superużytkownik może usunąć zgubiony składnik kolegi (zmiana z ponownym potwierdzeniem, zgłaszana e-mailem każdemu superużytkownikowi); kolega jest wtedy zablokowany, dopóki ktoś nie zarejestruje na serwerze nowej aplikacji uwierzytelniającej. Własnego ostatniego składnika nie można usunąć.

![Zespół](../img/team.png)
