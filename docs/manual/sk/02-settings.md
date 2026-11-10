# 2. Nastavenia

**Settings** je posledná položka pruhu vľavo. Jeho úvodná stránka vypisuje všetko nižšie uvedené, plus audit log, odchádzajúcu poštu (e-mail outbox) a doručenia webhookov (kapitola 6 ich používa).

![Úvodná stránka Settings](../img/settings-index.png)

Každá zmena sa zapíše do **Audit log** (Settings > Audit log): kto, čo a ktorý záznam, nikdy nie zadané hodnoty.

![Audit log](../img/audit.png)

Otvorením záznamu si prečítate podrobnosti. Zmeny uvedené v kapitole 1 v časti „Zosilnené overenie" vyžadujú superužívateľa s čerstvým druhým faktorom.

## Organizácie

**Organizácia** je právny subjekt, ktorý vystavuje faktúry: jeho názov, adresa, daňové identifikátory, logo, číslovanie faktúr a platobné účty, ktoré mu patria. Druhá organizácia sa hodí, keď jednu inštaláciu zdieľajú dve firmy, napríklad jedna v EÚ a jedna v USA.

![Zoznam organizácií](../img/orgs-list.png)

**Settings > Organizations > New organization** alebo otvorte existujúcu a upravte ju:

| Pole | Čo robí |
|---|---|
| Slug | Krátky názov používaný v API a v príkazoch (`example-studio`). |
| Legal name, Address, Tax ids | Tlačia sa v bloku „Dodávateľ" na faktúre. Daňové identifikátory sú voľný text. |
| Email from | Odosielateľ e-mailov organizácie, napr. `Example Studio <billing@example.com>`. |
| Default language | Jazyk stránok pre klienta a e-mailov, keď kontakt nemá preferovaný jazyk. |
| Footer note, Logo | Tlačia sa na faktúre a v PDF. Logo je PNG alebo JPEG do 1,9 MB, uložené ako PNG bez metadát. |
| Number format | V štýle Pythonu s `{year}` a `{seq}`: `{year}-{seq:04d}` dá `2026-0001`. Musí obsahovať `{seq}`. |
| Next number | Nastavuje sa vystavovaním faktúr; nedá sa upraviť, inak by sa čísla vydali znova. |
| Underpayment allowance | Krypto platba, ktorá chýba najviac o **menšiu** z hodnôt: tento podiel (bázické body; 50 je 0,5 %) a táto pevná suma v amerických dolároch (štandardne 1,00), stále vyrovná kurzovú ponuku. Pohltí poplatok burzy za výber. |
| Overpayment ceiling | Krypto platba, ktorá presahuje ponúknutú sumu o viac než tento podiel (1000 je 10 %), sa neuhradí, ale odloží na posúdenie človekom. |
| Accounting currency | Mena účtovných stĺpcov v exportoch (kapitola 8). |
| Hosts a Back button may lead to | Jeden host na riadok. Faktúra vytvorená cez API môže mať `return_url` presne na tieto hosty. |

![Organizácia](../img/org-edit.png)

### Vystavovateľ pre amerických daňových rezidentov

Označte **US-resident issuer** najviac na jednej organizácii. Je to organizácia, ktorá musí vystavovať faktúry klientom, ktorí sú americkí daňoví rezidenti.

- Každý kontakt má odpoveď **US tax resident**: yes, no alebo unknown. Formulár faktúry vás núti vybrať; platobný odkaz sa pýta kupujúceho.
- Keď iná organizácia vyžiada faktúru pre kontakt, ktorý je americký daňový rezident, faktúru **vystaví organizácia-vystavovateľ** so svojím číslovaním, bankovými účtami a kanálmi. Vystavovateľ si vedie vlastnú kópiu kontaktu; váš kontakt sa nikdy neprepisuje.
- Ak sa kontakt stane americkým daňovým rezidentom po vystavení faktúry inou organizáciou, faktúra sa zobrazí vo fronte **US tax residents with invoices from another issuer** v Needs a person: stornujte ju a vytvorte znova.
- Ak nie je žiadna organizácia označená, takú faktúru nemožno vôbec vytvoriť.

Zmena príznaku alebo návratových hostov patrí medzi zmeny so zosilneným overením. Príznak je v audit logu a posiela sa e-mailom superužívateľom.

## Bankové účty a fiat odkazy

Dole pri organizácii sú dva zoznamy. Oba sa **deaktivujú, nikdy nemažú**: vystavená faktúra naďalej zobrazuje účet, ktorý uvádzala.

![Bankové účty a platobné odkazy organizácie](../img/org-accounts.png)

**Add bank account** vyžaduje označenie, menu, príjemcu, IBAN alebo číslo účtu, banku, jej SWIFT/BIC a voliteľne sprostredkujúcu banku. Bankový účet sa ponúka na faktúrach **vo svojej mene**. Ak zadáte IBAN, platobná stránka aj PDF faktúra ukážu bankové QR kódy, pri EUR pre IBAN krajiny SEPA a pri CZK pre český účet (kapitola 4). *Pokyny k platbe* existujú po jednom pre každý jazyk: platobná stránka zobrazí pokyny v jazyku čitateľa a ako záložné anglické; jazyk nechajte prázdny, ak ho chcete vynechať.

![Nový bankový účet](../img/bank-new.png)

**Add payment link** (*fiat odkaz*, nezamieňať s zdieľateľnými stránkami z kapitoly 5) je odkaz na službu, ktorá prijme platbu inde: Revolut, N26, PayPal, Wise alebo „other". Vyžaduje adresu `https://` a meny, ktoré vie prijať. Platobná stránka ho zobrazí v záložke **Online platba**.

![Nový fiat odkaz](../img/fiat-new.png)

## Platobné kanály (rails)

**Rail account** je jeden spôsob, ako dostať zaplatené v kryptomene alebo cez Lightning. Aktívny účet je ponúkaný na každej novej faktúre svojej organizácie (ak faktúra nepomenuje svoje metódy sama). Otvorte **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** sa najprv pýta na organizáciu a druh.

![Výber druhu](../img/rail-new-choose.png)

| Druh | Čo vyžaduje |
|---|---|
| **evm** | Jednu adresu; siete na sledovanie (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) a prijímané aktíva (ETH, USDC, USDT). |
| **tron** | Jednu adresu; USDT. Sleduje sa celá sieť Tron. |
| **solana** | Jednu adresu; USDC a USDT. |
| **ton** | Zatiaľ vypnuté: dá sa uložiť iba ako neaktívne, lebo zatiaľ nemožno dokázať, že platba cez TON dorazila. |
| **btcpay** | `URL` a `Store ID` BTCPay, aktíva (BTC, XMR, ZEC) a politiku rýchlosti: `MediumSpeed` (1 potvrdenie, štandard), `LowMediumSpeed` (2) alebo `LowSpeed` (6). `HighSpeed` sa odmieta. |
| **lightning** | Backend: `nwc` (Nostr Wallet Connect, napr. Alby Hub), `lnd`, `cln` alebo `lnbits`; pri posledných troch aj `URL`. Aktívum BTC. |

![Nový EVM účet](../img/rail-new-evm.png)

![Nový Lightning účet](../img/rail-new-lightning.png)

Čo treba vedieť:

- **Používajte adresy, ktoré používa iba tento systém.** Platba na blockchaine sa páruje s faktúrou podľa **sumy**: každá kurzová ponuka žiada základnú sumu plus pár jednotiek „prachu", unikátnych medzi ponukami držiacimi túto adresu. Peniaze poslané tam z iného dôvodu skončia v Needs a person.
- URL kanála musí byť adresa `https://` na hoste, ktorý sa prekladá výlučne na verejné adresy; loopback alebo súkromná adresa sa odmieta (jedinou výnimkou je LND alebo CLN na rovnakom stroji, na porte `LOCAL_LIGHTNING_PORT`).
- Kým klient ešte môže zaplatiť ponuku, nemožno zmeniť organizáciu, druh, adresu, siete ani aktíva účtu; vytvorte radšej nový účet. **Deactivate** prestane účet ponúkať, no už zobrazené ponuky sa sledujú, kým sa neuvoľnia.
- Stĺpec *Watcher* hovorí, či worker vie čítať daný blockchain alebo bránu.

![Účet BTCPay](../img/rail-edit-btcpay.png)

### Prístupové údaje

Tajomstvá BTCPay a Lightning sú zašifrované kľúčom trezoru a **už sa nikdy nezobrazia**. Otvorte účet a stlačte **Set credentials** (trezor musí byť odomknutý), alebo použite v shelli `manage.py set_rail_credentials <id rail účtu>`. Prázdne pole ponechá uloženú hodnotu.

Systém **nikdy neutráca**, preto ukladá iba údaj, o ktorom sa dá dokázať, že to nevie:

| Backend | Čo vložiť |
|---|---|
| BTCPay | `api_key` iba s `btcpay.store.canviewinvoices` a `btcpay.store.cancreateinvoice` (BTCPay sa pýta, čo kľúč smie) a `webhook_secret`. Tlačidlo **Register the BTCPay webhook** vytvorí webhook v BTCPay a uloží jeho tajomstvo za vás. |
| Lightning, NWC | `nwc_url` pripojenia s `make_invoice` a `lookup_invoice` a **bez** metódy `pay_*`. |
| Lightning, LND | `macaroon` vytvorený s `invoices:read invoices:write info:read` (vlastný `invoice.macaroon` LND sa odmieta). |
| Lightning, Core Lightning | `rune` obmedzený na `invoice` a `listinvoices`. |
| Lightning, LNbits | **Invoice/read key** peňaženky, nikdy Admin key; zaškrtnite políčko, že ste to overili. |

![Prístupové údaje BTCPay](../img/rail-credentials-btcpay.png)

![Prístupové údaje Lightning](../img/rail-credentials-lightning.png)

Zmena hostu v URL kanála vymaže jeho uložené údaje a deaktivuje ho, takže sa kľúč nikdy neodošle inej službe. README ([Crypto rails](../../../README.md#crypto-rails)) obsahuje podrobnosti pre každý backend, premenné prostredia pre vlastné RPC endpointy a to, čo watchery nevidia.

## API tokeny a rozsahy (scopes)

**Settings > API tokens**. Token patrí **jednej organizácii** a vidí iba jej dáta. Zobrazí sa **raz**, pri vytvorení; ak sa stratí, zrušte ho a vytvorte nový. Zrušenie je trvalé a riadok zostáva.

![Tokeny](../img/tokens.png)

| Scope | Povoľuje |
|---|---|
| `read` | Každé GET. |
| `invoices:write` | Vytvárať a upravovať kontakty a ich šifrovacie kľúče; vytvárať, odosielať a stornovať faktúry; vydávať nové odkazy; spravovať platobné odkazy. |
| `payments:confirm` | Potvrdzovať, odmietať, vracať a priraďovať platby, potvrdzovať vrátenie preplatku, zamietať vyhlásenia o platbe. **Mení stav peňazí.** Nemôže zaznamenať ručnú platbu: to môže iba človek. |
| `admin` | Všetko vyššie a koncové body webhookov. |

Dajte tokenu najmenšiu potrebnú množinu; integrácia, ktorá iba vytvára a odosiela faktúry, potrebuje `read` a `invoices:write`. Vytvorenie, zmena rozsahov a zrušenie patria medzi zmeny so zosilneným overením.

![Token, zobrazený raz](../img/token-created.png)

Token môžete vydať aj z shellu: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooky

**Settings > Webhooks** zaregistruje koncový bod, ktorému sa oznamuje, čo sa deje s faktúrami a platbami organizácie.

![Webhooky](../img/webhooks.png)

- Adresa musí byť `https://`, na porte 443, na hoste, ktorý sa prekladá výlučne na verejné adresy. (Vývojové prostredie to môže uvoľniť cez `WEBHOOK_ALLOW_HTTP` a `WEBHOOK_ALLOW_PRIVATE`.)
- Zaškrtnite udalosti, ktoré chcete; nezaškrtnutie ničoho znamená všetky: `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert` a `ping`.
- **Tajomstvo** sa zobrazí raz. Každé doručenie je podpísané `POST` s `X-Pay-Signature: sha256=<hex>`, čo je HMAC-SHA256 surového tela s týmto tajomstvom; skontrolujte ho skôr, než volaniu uveríte. Telo nesie iba id a stavy, nikdy mená, adresy ani sumy: zvyšok si prečítajte cez API.
- Doručenie sa opakuje po 2, 5, 15, 30, 60, 120, 240 a 480 minútach, potom sa označí ako neúspešné; zamestnanci sa o tom dozvedia raz a zobrazí sa vo fronte **Webhooks not delivered** v Needs a person.
- Stránka koncového bodu má **Send a ping**, posledné doručenia a **Delete**. Settings > Webhook deliveries vypisuje každé doručenie s tlačidlom **Retry now**.

![Koncový bod a jeho doručenia](../img/webhook-detail.png)

Úplný zoznam udalostí a kód na overenie podpisu je v `/llms.txt` vašej inštalácie (kapitola 9).

## Tím

**Settings > Team** vypisuje všetkých, ktorí sa môžu prihlásiť, s menami ich druhých faktorov a posledným prihlásením. Superužívateľ môže odstrániť stratený faktor kolegu (zmena so zosilneným overením, e-mailom oznámená každému superužívateľovi); kolega je potom zamknutý, kým niekto na serveri nezaregistruje nový autentifikátor. Váš vlastný posledný faktor odstrániť nemožno.

![Tím](../img/team.png)
