# 5. Linki do płatności

**Link do płatności** to jedna strona, `/l/<token>/`, dla usługi za stałą cenę (albo za kwotę wybraną przez kupującego), którą płaci wiele osób: przycisk w sklepie, kod QR na plakacie, link w newsletterze. Otwórz **Payment links** na pasku.

(To nie są *fiat linki* organizacji z rozdziału 2, które tylko wskazują na Revolut, PayPal i podobne.)

![Linki do płatności](../img/links.png)

## Tworzenie linku

**New payment link** pyta o:

- **What is sold**: organizację, która sprzedaje, dokument (*Invoice* lub *Payment request*), tytuł i opis pokazywane kupującym oraz opcjonalny obraz (PNG, JPEG lub WebP do 1,5 MB, ponownie zakodowany; 960 × 540 pasuje do strony).
- **Price**: walutę, cenę i podatek %, **Maximum quantity per order** (0 oznacza bez limitu, do 200), albo zaznacz **The customer chooses the amount** z opcjonalnym minimum i maksimum.
- **What the buyer is asked**: adres e-mail i „Are you a US tax resident?" są pytane **zawsze**; imię i nazwisko, adres rozliczeniowy, adres dostawy i telefon mogą być off, optional lub required; do pięciu dodatkowych pytań.
- **Payment methods**: domyślne metody wystawcy lub **własny zestaw**, np. „USDC on Arbitrum One", „Bank transfer in EUR" czy „Bitcoin over Lightning". Własny zestaw nazywa rodzaje płatności, nie konta, więc wystawca dla USA oferuje własne konta tych samych rodzajów.
- **Availability**: active, *Valid until*, *Total uses*, **Paid orders need fulfilling** i **E-mail staff for each paid order** oraz notatka wewnętrzna widoczna tylko dla pracowników.

![Formularz nowego linku](../img/link-new.png)

Link działa, gdy tylko zostanie zapisany i będzie aktywny.

## Udostępnianie

Strona linku pokazuje URL z **Copy**, **kod QR** (PNG lub SVG), **arkusz do druku** (PDF), HTML **przycisku dla strony WWW** i **Replace the URL**, który nadaje nowy adres i natychmiast zatrzymuje stary. Każdy, kto ma URL, może kupić; dodaj `?promoCode=CODE`, aby wypełnić kupon.

![Link z kodem QR, kuponami i zamówieniami](../img/link-detail.png)

**Kupony**: kod, procentowy rabat od pozycji (przed podatkiem, nigdy darmowe zamówienie), opcjonalnie liczba użyć i data końca. **Copy link with code** daje URL, który go wypełnia.

## Co widzi kupujący

![Publiczna strona linku](../img/link-public.png)

Kupujący wpisuje adres e-mail, imię i nazwisko, jeśli jest pytany, odpowiada na pytanie o USA i przechodzi do płatności. Strona, na którą trafia, to zwykła **strona płatności** z rozdziału 4.

![Strona płatności zamówienia](../img/link-checkout-pay.png)

- Każdy checkout tworzy **zamówienie**: fakturę lub prośbę o płatność z jedną pozycją, ponumerowaną i od razu wysłaną, z zamrożonym stanem. **Przed płatnością nie jest wysyłany żaden e-mail**; strona płatności mówi, aby ją zachować. Pierwszy list to potwierdzenie lub list o płatności częściowej i niesie linki zamówienia.
- **Amerykańscy rezydenci podatkowi.** Odpowiedź wpisana przez kupującego decyduje, która organizacja wystawia zamówienie: yes do wystawcy dla USA, no do organizacji linku. To, co przechowuje kontakt, nigdy nie decyduje, więc publiczny link nie ujawnia niczego zapisanego. Wpisana odpowiedź, której przeczy zapisany znacznik, staje się pozycją w Needs a person, a przy „no" wobec zapisanego „yes" zamówienie nie jest oferowane do realizacji, dopóki człowiek nie sprawdzi tego z kupującym.
- **Publiczny formularz nigdy nie pokazuje ani nie zmienia kontaktu.** Adres pasujący do klienta w ewidencji przypisuje zamówienie temu klientowi. W przeciwnym razie z wpisanych danych powstaje *link buyer*. Link buyers są w Contacts z filtrem, nie znajduje ich żadne wyszukiwanie i nic ich nie używa ponownie; człowiek może po sprawdzeniu zrobić z nich klienta w ewidencji na ich stronie.

  ![Contacts, tylko link buyers](../img/contacts-link-buyers.png)

## Zamówienia, użycia i limity

- Checkout **blokuje** jedno użycie linku (i kuponu) na 60 minut; płatność zamienia blokadę w użycie. Płatność, która przyjdzie po wygaśnięciu blokady, zachowuje cenę; jeśli przez to link lub kupon z limitem przekroczy limit, pracownicy dostają jedną pozycję w Needs a person.
- **Nieopłacone zamówienia są anulowane po 7 dniach.** Pieniądze, które wpłyną później, trafiają do weryfikacji, jak przy każdej anulowanej fakturze.
- **Realizacja.** Przy włączonym *Paid orders need fulfilling* opłacone zamówienia są na stronie linku wymienione jako do realizacji, dopóki ktoś nie naciśnie **Mark fulfilled**.
- **Limity**: 5 prób checkoutu na 10 minut na adres i 50 ukończonych checkoutów na godzinę na link. Żadna strona nie może osadzać linku w ramce.
- Tworzenie lub edycja linku przez API wymaga `invoices:write`; URL linku jest podawany przy tworzeniu, przy rotacji i przy odczycie pojedynczego linku tokenem, który ma też `invoices:write` (sam `read` dostaje null).
