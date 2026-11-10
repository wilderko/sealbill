# 5. Platobné odkazy

**Platobný odkaz** je jedna stránka `/l/<token>/` pre službu za pevnú cenu (alebo za sumu, ktorú si kupujúci zvolí), ktorú platí veľa ľudí: tlačidlo v obchode, QR kód na plagáte, odkaz v newsletteri. Otvorte **Payment links** v pruhu vľavo.

(Nie sú to *fiat odkazy* organizácie z kapitoly 2, ktoré len ukazujú na Revolut, PayPal a podobné.)

![Platobné odkazy](../img/links.png)

## Vytvorenie odkazu

**New payment link** sa pýta na:

- **What is sold**: organizáciu, ktorá predáva, doklad (*Invoice* alebo *Payment request*), názov a popis zobrazené kupujúcim a voliteľný obrázok (PNG, JPEG alebo WebP do 1,5 MB, prekódovaný; 960 × 540 sedí stránke).
- **Price**: menu, cenu a daň %, **Maximum quantity per order** (0 znamená neobmedzene, najviac 200), alebo zaškrtnite **The customer chooses the amount** s voliteľným minimom a maximom.
- **What the buyer is asked**: e-mailová adresa a „Are you a US tax resident?" sa pýtajú **vždy**; meno, fakturačná adresa, doručovacia adresa a telefón môžu byť off, optional alebo required; až päť vlastných otázok.
- **Payment methods**: predvolené metódy vystavovateľa alebo **vlastná množina**, napr. „USDC on Arbitrum One", „Bank transfer in EUR" alebo „Bitcoin over Lightning". Vlastná množina pomenúva druhy platby, nie účty, takže vystavovateľ pre USA ponúka vlastné účty rovnakých druhov.
- **Availability**: active, *Valid until*, *Total uses*, **Paid orders need fulfilling** a **E-mail staff for each paid order** a interná poznámka, ktorú vidia iba zamestnanci.

![Formulár nového odkazu](../img/link-new.png)

Odkaz funguje hneď, ako je uložený a aktívny.

## Zdieľanie

Stránka odkazu ukazuje URL s tlačidlom **Copy**, **QR kód** (PNG alebo SVG), **tlačiteľný list** (PDF), HTML **tlačidla pre webovú stránku** a **Replace the URL**, ktoré dá novú adresu a starú okamžite zastaví. Kúpiť môže každý, kto má URL; pripojením `?promoCode=CODE` sa vyplní kupón.

![Odkaz s QR kódom, kupónmi a objednávkami](../img/link-detail.png)

**Kupóny**: kód, percentuálna zľava z riadku (pred daňou, nikdy nie objednávka zadarmo), voliteľne počet použití a dátum konca. **Copy link with code** dá URL, ktorá ho vyplní.

## Čo vidí kupujúci

![Verejná stránka odkazu](../img/link-public.png)

Kupujúci napíše e-mailovú adresu, meno ak sa pýta, odpovie na otázku o USA a pokračuje k platbe. Stránka, na ktorú sa dostane, je bežná **platobná stránka** z kapitoly 4.

![Platobná stránka objednávky](../img/link-checkout-pay.png)

- Každý checkout vytvorí **objednávku**: faktúru alebo žiadosť o platbu s jednou položkou, očíslovanú a hneď odoslanú, so zmrazeným snímkom. **Pred platbou sa neodosiela žiadny e-mail**; platobná stránka hovorí, aby ste si ju uschovali. Prvý e-mail je potvrdenie platby alebo e-mail o čiastočnej platbe a nesie odkazy objednávky.
- **Americkí daňoví rezidenti.** Odpoveď, ktorú kupujúci napísal, rozhoduje, ktorá organizácia objednávku vystaví: yes vystavovateľovi pre USA, no organizácii odkazu. To, čo kontakt ukladá, nikdy nerozhoduje, takže verejný odkaz neprezrádza nič uložené. Zadaná odpoveď, ktorej uložený príznak protirečí, sa stane položkou v Needs a person a pri „no" proti uloženému „yes" sa objednávka neponúkne na vybavenie, kým človek nepreverí kupujúceho.
- **Verejný formulár nikdy neukáže ani nezmení kontakt.** Adresa, ktorá zodpovedá klientovi v evidencii, priradí objednávku tomuto klientovi. Inak sa z napísaných údajov vytvorí *link buyer*. Link buyers sú v Contacts s filtrom, nenájde ich žiadne vyhľadávanie a nič ich znova nepoužije; človek môže z nich po preverení spraviť klienta v evidencii na jeho stránke.

  ![Contacts, iba link buyers](../img/contacts-link-buyers.png)

## Objednávky, použitia a limity

- Checkout **drží** jedno použitie odkazu (a kupónu) 60 minút; platba z neho spraví použitie. Platba, ktorá príde po vypršaní držania, si zachová cenu; ak tým odkaz alebo kupón s limitom prekročí limit, zamestnanci dostanú jednu položku v Needs a person.
- **Nezaplatené objednávky sa po 7 dňoch stornujú.** Peniaze, ktoré prídu neskôr, idú na posúdenie, ako pri každej stornovanej faktúre.
- **Vybavenie.** Keď je zapnuté *Paid orders need fulfilling*, zaplatené objednávky sú na stránke odkazu vedené ako na vybavenie, kým niekto nestlačí **Mark fulfilled**.
- **Limity**: 5 pokusov o checkout za 10 minút na adresu a 50 dokončených checkoutov za hodinu na odkaz. Odkaz nemôže vložiť do rámca žiadna stránka.
- Vytvorenie alebo úprava odkazu cez API je `invoices:write`; URL odkazu sa vydáva pri vytvorení, pri rotácii a keď sa číta jeden odkaz.
