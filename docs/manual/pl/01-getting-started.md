# 1. Pierwsze kroki

## Co uruchamiasz

Sealbill to jedna aplikacja webowa i jeden worker działający w tle.

- **Aplikacja webowa** obsługuje strony dla pracowników (w katalogu głównym witryny, np. `https://pay.example.org/`), strony klienta (`/i/…` pokazuje fakturę, `/p/…` to miejsce, w którym się płaci, `/l/…` to link do płatności), API (`/api/v1/…`) i stronę odblokowania (`/unlock/`).
- **Worker** (`manage.py run_worker`) robi wszystko, co musi się dziać bez kliknięcia: wysyła e-maile z kolejki, oznacza faktury jako przeterminowane, obserwuje blockchainy i bramki w poszukiwaniu płatności, wygasza oferty kursowe, wysyła jedno przypomnienie o płatności i dostarcza webhooki. **Bez workera nic nie jest wysyłane e-mailem i żadna płatność krypto nie zostaje zauważona.** Uruchom go jako drugi proces obok aplikacji webowej; `manage.py run_worker --once` wykonuje każde zadanie raz i kończy, co przydaje się do testu.

## Instalacja i pierwsze uruchomienie

README repozytorium zawiera polecenia dla każdego sposobu uruchamiania, więc nie powtarzamy ich tutaj:

- na własnym komputerze, do wypróbowania: [README, Development](../../../README.md#development)
- z Docker Compose: [README, Docker Compose](../../../README.md#docker-compose)
- na serwerze z systemd, Apache i PostgreSQL: [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Niezależnie od wyboru kolejność pierwszego uruchomienia jest taka sama:

1. Utwórz tabele bazy danych (`migrate` oraz `createcachetable` dla tabel limitów żądań).
2. Utwórz sejf i odblokuj go (następna sekcja).
3. Utwórz pierwszego użytkownika poleceniem `manage.py createsuperuser`.
4. Daj mu drugi składnik: `manage.py create_staff_totp <username>` wypisuje adres `otpauth://` dla aplikacji uwierzytelniającej. Wymaga odblokowanego sejfu. Passkey możesz dodać później na stronie Security.
5. Uruchom aplikację webową i workera, otwórz witrynę i zaloguj się.
6. W **Settings** utwórz organizację oraz konto bankowe lub konto kanału płatności (rozdział 2) i wyślij sobie pierwszą fakturę (rozdział 4).

## Sejf

Wszystko, co wpisał człowiek (nazwiska, adresy, identyfikatory podatkowe, pozycje faktur, notatki, dane dostępowe do bramek płatności), jest przechowywane w postaci zaszyfrowanej. Klucz jest wyprowadzany z **hasła sejfu** i trzymany wyłącznie w pliku w pamięci wskazanym przez `VAULT_KEY_PATH`; nigdy nie ma go w środowisku ani na dysku.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # raz; co najmniej 16 znaków
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # po każdym restarcie maszyny
```

Hasło podawaj na standardowym wejściu, jak wyżej, aby nigdy nie pojawiło się w wierszu poleceń ani w historii powłoki. `init_vault` tylko tworzy `vault.json` (sól i wartość kontrolną, nie klucz); `unseal_vault` go otwiera.

Po restarcie maszyny lub gdy plik w pamięci zostanie wymazany, sejf jest **zapieczętowany**. Łagodne przeładowanie usługi webowej zostawia go otwartego; twardy restart nie. Dopóki jest zapieczętowany:

- każda strona dla pracowników przekierowuje na **/unlock/**: wpisz tam hasło sejfu lub uruchom `unseal_vault`;
- strony płatności, linki do płatności i API odpowiadają „tymczasowo niedostępne" (503), więc żaden klient nie dostanie oferty kursowej;
- worker nadal widzi i dopasowuje płatności na blockchainie (czyta tylko kolumny niezaszyfrowane), ale e-maile i webhooki czekają, a odpytywanie bramek (BTCPay, Lightning) jest wstrzymane; operator dostaje jeden alarmowy e-mail na każdy epizod zapieczętowania.

![Strona odblokowania, wyświetlana, gdy sejf jest zapieczętowany](../img/vault-unlock.png)

`GET /healthz` zgłasza `"vault": "sealed"` lub `"unsealed"` bez żadnych danych, więc monitoring może go obserwować. Zachowaj kopię `vault.json`, hasło do kopii zapasowej i hasło sejfu w miejscu innym niż serwer: bazy danych bez wszystkich trzech nie da się odczytać. Pełna lista tego, co działa przy zapieczętowanym sejfie, jest w [README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Logowanie

Pracownicy logują się pod `/login/` w dwóch krokach: **hasło**, a potem **drugi składnik**, czyli passkey lub sześciocyfrowy kod z aplikacji uwierzytelniającej. Sesja trwa osiem godzin i kończy się z zamknięciem przeglądarki. Logowanie ma limit prób na adres, na konto i na urządzenie.

![Krok z hasłem](../img/start-login.png)

![Krok z drugim składnikiem](../img/start-verify.png)

Gdy konto ma passkey, drugi krok oferuje najpierw **Use a passkey**, a pod nim pole na kod („or enter a code"). Passkey należy tylko do tej witryny, więc fałszywa strona nie może go użyć.

![Drugi krok konta z passkey](../img/verify-passkey.png)

Po zalogowaniu trafiasz na **Needs a person** (rozdział 6). Pasek po lewej to cała nawigacja: Needs a person, Invoices, New invoice, Contacts, Payment links, Exports, Settings; tarcza na dole to **Security**, strzałka wylogowuje.

![Needs a person, pierwsza strona po zalogowaniu](../img/needs-person.png)

### Security: własne passkeys i aplikacje uwierzytelniające

**Security** (ikona tarczy) wymienia Twoje drugie składniki. Passkey dodajesz, nadając mu nazwę (np. „MacBook Touch ID" lub „YubiKey 5C") i naciskając **Add a passkey**; przeglądarka zapyta wtedy o odcisk palca, twarz lub PIN. Aplikację uwierzytelniającą dodaje się na serwerze poleceniem `create_staff_totp`, nigdy w przeglądarce. Ostatniego drugiego składnika konta nie można usunąć, a usunięcie tego, którym zalogowano bieżącą sesję, najpierw wymaga innego. Każde dodanie i usunięcie jest w dzienniku audytu.

![Security z dodanym passkey](../img/security-passkey.png)

## Pracownicy i superużytkownicy

Każdy, kto ma konto pracownika, może wystawiać faktury, rejestrować płatności i obsługiwać kolejki. Niektóre zmiany mogą przesunąć pieniądze lub przekierować klienta i wymagają więcej.

### Ponowne potwierdzenie (step-up)

Te zmiany wymagają **superużytkownika**, który podał drugi składnik **w ciągu ostatnich dziesięciu minut**:

- utworzenie, zmiana zakresów (scopes) lub unieważnienie tokenu API;
- konto kanału płatności i jego dane dostępowe (oraz webhook BTCPay, który przechowuje sekret);
- znacznik wystawcy dla amerykańskich rezydentów podatkowych organizacji i jej hosty powrotu;
- konto bankowe lub fiat link do płatności organizacji;
- dodanie adresu e-mail do kontaktu lub uczynienie go głównym (trafiają tam e-maile, linki do płatności i PDF-y);
- usunięcie drugiego składnika innego pracownika.

Strony mówią, na czym stoisz, zanim cokolwiek naciśniesz:

- pracownik, który nie jest superużytkownikiem, widzi „Only a superuser may …" i nic się nie zmienia;

  ![Pracownik, który nie jest superużytkownikiem](../img/stepup-not-superuser.png)

- superużytkownik, którego ostatni drugi składnik jest starszy niż dziesięć minut, widzi **Verify again**; prowadzi to na stronę weryfikacji i wraca na stronę, na której byłeś.

  ![Zmiana wymagająca świeżego drugiego składnika](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Każda zmiana z ponownym potwierdzeniem wysyła też e-mail do każdego aktywnego superużytkownika: kto, co i kiedy, według nazw i identyfikatorów, nigdy sekret, numer konta ani adres. Jeśli skradziona sesja coś zmieni, właściciel systemu się o tym dowie.

Strona **Team** (Settings > Team) wymienia wszystkich, którzy mogą się zalogować, ich drugie składniki i ostatnie logowanie, a superużytkownikowi pozwala usunąć zgubiony składnik kolegi. Nowych pracowników tworzysz poleceniem `createsuperuser` lub w ukrytym panelu Django admin (ścieżka z `DJANGO_ADMIN_PATH`; tylko superużytkownik z drugim składnikiem, wszyscy inni dostają 404).
