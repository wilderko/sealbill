# 6. Needs a person

**Needs a person** to pierwsza strona i ta, na którą trzeba zwracać uwagę. Wymienia to, czego żaden program nie powinien rozstrzygać sam. Pasek pokazuje, ile pozycji jest otwartych; gdy nie ma nic do zrobienia, strona to mówi.

![Kolejki z zawartością](../img/needs-person.png)

Kolejki są pokazywane tylko wtedy, gdy mają pozycje; **Show N queues** wymienia także puste, abyś widział, co strona obserwuje.

![Wszystkie kolejki](../img/needs-person-queues.png)

| Kolejka | Co to jest i co robić |
|---|---|
| **Declared payments** | Klient nacisnął „Zgłaszam płatność" przy fakturze. Sprawdź bank lub łańcuch. Zarejestrowanie płatności na fakturze załatwia zgłoszenie; **Dismiss declaration** na fakturze zamyka fałszywe. |
| **Payments to review** | Pieniądze, które nie pasowały do żadnej faktury lub pasowały źle, z powodem: token nie jest akceptowany, żadna otwarta faktura nie pasuje, mogłaby pasować więcej niż jedna, przyszły po wygaśnięciu oferty, duplikat, znacznie ponad należność, faktura była już zamknięta, bardzo mały przelew „pyłu" (prawdopodobnie spam typu address poisoning, zwinięty), porzucona transakcja albo raport BTCPay o fakturze częściowej, spóźnionej, nieważnej lub oznaczonej ręcznie. **Confirm** przypisuje ją do jej faktury, **Reject** z notatką, albo **Assign to an invoice** (numer faktury, kwota i notatka). Na przelewie w łańcuchu można działać, gdy jest finalny. |
| **Waiting for staff confirmation** | Płatności Monero i Zcash (przez BTCPay) oraz pieniądze zarejestrowane ręcznie: nic ich nie obserwuje, więc sprawdź portfel lub bank, a potem **Confirm** lub **Reject**. Potwierdzić może tylko człowiek; żaden token API. |
| **Gateway payments seen but not settled** | Płatności BTCPay lub Lightning, które bramka widziała ponad sześć godzin temu i nie rozliczyła. |
| **Refunds due** | Płatność krypto powyżej oferty kursowej. Odeślij różnicę na podany adres, potem **Refunded** z notatką. |
| **Confirmed payments the gateway now calls invalid** | BTCPay ją rozliczył, a potem uznał za nieważną (double spend, reorg). Kod niczego nie cofa: cofnij płatność z jej faktury lub ją zachowaj. |
| **Exchange rates refused** | Fiat część kursów jest nieaktualna lub skoczyła. Sprawdź kurs EBC; **accept** skok, który jest prawdziwy. Oferty krypto w tej walucie są w międzyczasie odrzucane. |
| **Watchers that could not look** | Czytnik łańcucha lub bramki zawodzi od dziesięciu minut lub dłużej (pracownicy dostali też e-mail). |
| **Transfers seen but not recorded** | Przelewu, który watcher widział, nie udało się jeszcze zarejestrować; jest ponawiany przy każdym przebiegu. |
| **Blocks not scanned, or to check** | Pominięto zakres bloków. Sprawdź go ręcznie, potem **Acknowledge** z tym, co sprawdziłeś. |
| **Overpaid invoices** | Zwróć poza systemem lub **Keep the overpayment** (rozdział 4). |
| **US tax residents with invoices from another issuer** | Anuluj fakturę i utwórz ją ponownie; wystawi ją wtedy organizacja dla USA. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Rozdział 3. |
| **E-mail not delivered** | Wiadomość, która zawodziła przez około szesnaście godzin. **Retry** albo **Dismiss** z powodem (jest wtedy anulowana na dobre). |
| **Payment links** | Opłacone zamówienie przekroczyło limit linku, wystawca nie oferuje żadnej z metod linku, wpisana odpowiedź o USA przeczy zapisanej albo żadna organizacja nie jest wystawcą dla USA. Naciśnij **Resolved** z tym, co sprawdziłeś. |
| **Webhooks not delivered** | Dostarczenie, które zawiodło po wszystkich ponowieniach. **Retry** lub **Dismiss** z powodem. |

Dwie zasady obowiązują na całej stronie:

- Płatność, która **czeka na człowieka**, jest potwierdzana, przypisywana lub odrzucana tylko przez zalogowanego pracownika. Token, nawet `admin`, dostaje `409 needs_person` i nic się nie zmienia. Rejestracja płatności ręcznej to również zadanie człowieka.
- Każda akcja wymaga notatki tam, gdzie ma to znaczenie, i każda akcja jest w dzienniku audytu.

Te same kolejki można obsługiwać przez API z `payments:confirm`, z wyjątkiem tych zarezerwowanych dla człowieka (rozdział 9). Dwie listy, których użyjesz, aby ustalić, *dlaczego* coś utknęło, to **Settings > E-mail outbox** (każdy list ze stanem, próbami i powodem niepowodzenia; odbiorcy i treści nigdy nie są pokazywani) oraz **Settings > Webhook deliveries**.

![Skrzynka nadawcza e-maili](../img/outbox.png)

![Dostarczenia webhooków](../img/deliveries.png)
