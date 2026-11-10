# 5. Zahlungslinks

Ein **Zahlungslink** ist eine Seite, `/l/<token>/`, für eine Leistung zu einem festen Preis (oder zu einem Betrag, den der Käufer wählt), die viele Menschen bezahlen: ein Shop-Button, ein QR-Code auf einem Plakat, ein Link im Newsletter. Öffnen Sie **Payment links** in der Leiste.

(Das sind nicht die *Fiat-Links* einer Organisation aus Kapitel 2, die nur auf Revolut, PayPal und Ähnliches verweisen.)

![Zahlungslinks](../img/links.png)

## Einen Link erstellen

**New payment link** fragt nach:

- **What is sold**: der verkaufenden Organisation, dem Dokument (*Invoice* oder *Payment request*), einem Titel und einer Beschreibung, die Käufern gezeigt werden, und optional einem Bild (PNG, JPEG oder WebP bis 1,5 MB, neu kodiert; 960 × 540 passt zur Seite).
- **Price**: Währung, Preis und Steuer %, **Maximum quantity per order** (0 heißt unbegrenzt, bis 200), oder haken Sie **The customer chooses the amount** an, mit optionalem Minimum und Maximum.
- **What the buyer is asked**: E-Mail-Adresse und „Are you a US tax resident?" werden **immer** gefragt; Name, Rechnungsadresse, Lieferadresse und Telefon sind off, optional oder required; bis zu fünf Zusatzfragen.
- **Payment methods**: die Standardmethoden des Issuers oder ein **eigener Satz**, etwa „USDC on Arbitrum One", „Bank transfer in EUR" oder „Bitcoin over Lightning". Ein eigener Satz nennt Zahlungsarten, nicht Konten; der US-Issuer bietet daher eigene Konten derselben Arten an.
- **Availability**: active, *Valid until*, *Total uses*, **Paid orders need fulfilling** und **E-mail staff for each paid order** sowie eine interne Notiz, die nur Mitarbeitende sehen.

![Das Formular für einen neuen Link](../img/link-new.png)

Der Link funktioniert, sobald er gespeichert und aktiv ist.

## Teilen

Die Seite des Links zeigt die URL mit **Copy**, einen **QR-Code** (PNG oder SVG), ein **druckbares Blatt** (PDF), das HTML eines **Buttons für eine Webseite** und **Replace the URL**, das eine neue Adresse vergibt und die alte sofort stoppt. Jeder mit der URL kann kaufen; hängen Sie `?promoCode=CODE` an, um einen Gutschein auszufüllen.

![Ein Link mit QR-Code, Gutscheinen und Bestellungen](../img/link-detail.png)

**Gutscheine**: ein Code, ein Prozentrabatt auf die Position (vor Steuer, nie eine kostenlose Bestellung), optional eine Zahl von Verwendungen und ein Enddatum. **Copy link with code** liefert eine URL, die ihn ausfüllt.

## Was der Käufer sieht

![Die öffentliche Seite eines Links](../img/link-public.png)

Der Käufer tippt eine E-Mail-Adresse ein, einen Namen, wenn gefragt, beantwortet die US-Frage und geht weiter zur Zahlung. Die Seite, auf der er landet, ist die gewöhnliche **Zahlungsseite** aus Kapitel 4.

![Die Zahlungsseite einer Bestellung](../img/link-checkout-pay.png)

- Jeder Checkout erzeugt eine **Bestellung**: eine Rechnung oder eine Zahlungsaufforderung mit einer Position, nummeriert und sofort gesendet, mit eingefrorenem Stand. **Vor der Zahlung wird keine E-Mail gesendet**; die Zahlungsseite sagt, man solle sie aufbewahren. Der erste Brief ist die Quittung oder der Brief über die Teilzahlung, und er enthält die Links der Bestellung.
- **US-Steuerresidenten.** Die Antwort, die der Käufer eintippt, entscheidet, welche Organisation die Bestellung ausstellt: ein yes den US-Issuer, ein no die Organisation des Links. Was ein Kontakt gespeichert hat, entscheidet nie, ein öffentlicher Link verrät also nichts Gespeichertes. Eine eingetippte Antwort, der das gespeicherte Kennzeichen widerspricht, wird ein Eintrag in Needs a person, und bei einem „no" gegen ein gespeichertes „yes" wird die Bestellung nicht zur Erledigung angeboten, bis ein Mensch beim Käufer nachgefragt hat.
- **Ein öffentliches Formular zeigt oder ändert nie einen Kontakt.** Eine Adresse, die zu einem Kunden in der Kundenkartei passt, bucht die Bestellung auf diesen Kunden. Sonst entsteht aus den eingetippten Daten ein *Link buyer*. Link buyers stehen in Contacts mit einem Filter, werden von keiner Suche gefunden und von nichts wiederverwendet; ein Mensch kann einen nach Prüfung auf seiner Seite zum Kunden in der Kundenkartei machen.

  ![Contacts, nur Link buyers](../img/contacts-link-buyers.png)

## Bestellungen, Verwendungen und Limits

- Ein Checkout **hält** eine der Verwendungen des Links (und des Gutscheins) 60 Minuten lang; die Zahlung macht daraus eine Verwendung. Eine Zahlung, die nach Ablauf der Reservierung eintrifft, behält ihren Preis; überschreitet ein Link oder Gutschein mit Limit dadurch sein Limit, bekommen die Mitarbeitenden einen Eintrag in Needs a person.
- **Unbezahlte Bestellungen werden nach 7 Tagen storniert.** Geld, das später eintrifft, geht zur Prüfung, wie bei jeder stornierten Rechnung.
- **Erledigung.** Ist *Paid orders need fulfilling* an, stehen bezahlte Bestellungen auf der Seite des Links als zu erledigen, bis jemand **Mark fulfilled** drückt.
- **Limits**: 5 Checkout-Versuche pro 10 Minuten und Adresse und 50 abgeschlossene Checkouts pro Stunde und Link. Keine Seite darf einen Link einbetten.
- Einen Link über die API zu erstellen oder zu ändern erfordert `invoices:write`; die URL des Links wird beim Erstellen, beim Rotieren und beim Lesen eines einzelnen Links ausgegeben.
