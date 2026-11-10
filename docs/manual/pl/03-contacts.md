# 3. Kontakty

**Kontakt** to klient organizacji: nazwiska, jeden lub więcej adresów e-mail, adres pocztowy, identyfikatory podatkowe, telefon, preferowany język, notatka i odpowiedź na „US tax resident?". Każdy kontakt należy do jednej organizacji. Otwórz **Contacts** na pasku.

![Lista kontaktów](../img/contacts.png)

- Szukaj po **dokładnym adresie e-mail**; filtruj po organizacji.
- Lista pokazuje **Clients and link buyers**, **Clients of record** lub **Link buyers**. *Link buyer* to kontakt, który checkout linku do płatności utworzył z tego, co wpisał odwiedzający; do publicznego formularza każdy może wpisać cokolwiek, dlatego nie jest klientem w ewidencji (client of record), dopóki człowiek go nie awansuje (rozdział 5).
- Kontakt, który widzisz dwa razy z tą samą nazwą, raz pod każdą organizacją, to **kopia klienta u wystawcy dla USA** (rozdział 2): wystawca prowadzi własną kopię, aby dane pierwszej organizacji nigdy nie były nadpisywane.

## Tworzenie i zmiana kontaktu

Rzadko tworzysz kontakt osobno. Wpisanie adresu e-mail w **New invoice** znajduje kontakt z tym adresem albo tworzy nowy z wpisanych danych; to, co zostawisz puste, nigdy nie nadpisuje zapisanych danych. Kontakty tworzą też API i importer.

**Contact > Edit** zmienia nazwę, telefon, preferowany język, odpowiedź o USA, identyfikatory podatkowe, notatki i adres. Organizacji nie można zmienić. Obniżenie zapisanej odpowiedzi o USA (z „yes") jest odnotowywane w dzienniku audytu.

![Edycja kontaktu](../img/contact-edit.png)

![Kontakt](../img/contact.png)

Na stronie kontaktu lista **E-mail addresses** dodaje adres, ustawia jeden jako **Primary** i usuwa adresy. E-maile, linki do płatności i PDF-y trafiają na adres główny, więc dodanie lub awansowanie adresu to zmiana z [ponownym potwierdzeniem](01-getting-started.md). Nie można usunąć adresu głównego, ostatniego adresu ani adresu, który wskazuje aktywny klucz szyfrujący.

## Klucze szyfrujące kontaktu

Wszystko, co zapisano o kontakcie, jest już zaszyfrowane w spoczynku kluczem sejfu (rozdział 1). Kontakt może mieć dodatkowo własny klucz, aby **e-maile, które do niego wysyłasz**, były na niego szyfrowane: **klucz publiczny PGP** lub **certyfikat S/MIME**. Otwórz **Contact > Encryption keys**.

![Kontakt bez klucza](../img/contact-keys-empty.png)

- **Wgranie przez pracownika.** Wklej klucz lub wybierz plik. Musi wskazywać jeden z adresów kontaktu, nie może być wygasły i musi być kluczem publicznym lub certyfikatem (klucz prywatny lub plik `.p12` jest odrzucany). Staje się od razu kluczem **aktywnym** i zastępuje obecny; klient nie musi niczego potwierdzać.
- **Wgranie przez klienta.** Każda strona klienta ma **Otrzymywanie zaszyfrowanych e-maili**. Klucz, który klient tam wgra, jest **pending** (oczekujący): czeka, aż klient odpowie na e-mail zaszyfrowany na ten klucz, co dowodzi, że ma jego prywatną połowę. Jeśli kontakt miał już aktywny klucz, jego posiadacz dostaje powiadomienie z linkiem **anulowania**, aby ktoś czytający skrzynkę nie mógł niepostrzeżenie podstawić własnego klucza.

  ![Strona klienta do wgrywania klucza](../img/portal-key-upload.png)

- Używany jest tylko klucz **aktywny**, jeden na kontakt. Jego stan to `pending`, `active`, `replaced`, `revoked` lub `failed`. **Revoke** go wycofuje; e-maile idą wtedy niezaszyfrowane.
- Klucz, który **przestaje działać będąc aktywnym** (wygasł lub został unieważniony u źródła), dostaje stan `failed`, a e-maile do tego kontaktu są **odrzucane, zamiast wysyłane niezaszyfrowane**. Jest w Needs a person w sekcji *Encryption keys that stopped working*; wgraj działający klucz lub unieważnij ten, który zawiódł, a potem ponów wiadomości. Klucz, który zawodzi jeszcze w stanie pending, niczego nie wstrzymuje. Needs a person wymienia też *klucze wygasające w ciągu 30 dni*.
- Kopia kontaktu u wystawcy automatycznie podąża za kluczem oryginału.

![Kontakt z aktywnym kluczem PGP](../img/contact-keys-active.png)

To samo można zrobić przez API: `POST /api/v1/contacts/{id}/keys` z `{"key": "<armored key>"}` (scope `invoices:write`) i `POST /api/v1/keys/{id}/revoke`.
