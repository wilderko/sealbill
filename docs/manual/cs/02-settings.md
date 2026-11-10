# 2. Nastavení

**Settings** je poslední položka pruhu vlevo. Jeho úvodní stránka vypisuje vše níže uvedené, plus audit log, odchozí poštu (e-mail outbox) a doručení webhooků (kapitola 6 je používá).

![Úvodní stránka Settings](../img/settings-index.png)

Každá změna se zapíše do **Audit log** (Settings > Audit log): kdo, co a který záznam, nikdy ne zadané hodnoty.

![Audit log](../img/audit.png)

Otevřením záznamu si přečtete podrobnosti. Změny uvedené v kapitole 1 v části „Zesílené ověření" vyžadují superuživatele s čerstvým druhým faktorem.

## Organizace

**Organizace** je právní subjekt, který vystavuje faktury: jeho název, adresa, daňové identifikátory, logo, číslování faktur a platební účty, které mu patří. Druhá organizace se hodí, když jednu instalaci sdílejí dvě firmy, například jedna v EU a jedna v USA.

![Seznam organizací](../img/orgs-list.png)

**Settings > Organizations > New organization**, nebo otevřete existující a upravte ji:

| Pole | Co dělá |
|---|---|
| Slug | Krátký název používaný v API a v příkazech (`example-studio`). |
| Legal name, Address, Tax ids | Tisknou se v bloku „Dodavatel" na faktuře. Daňové identifikátory jsou volný text. |
| Email from | Odesílatel e-mailů organizace, např. `Example Studio <billing@example.com>`. |
| Default language | Jazyk stránek pro klienta a e-mailů, když kontakt nemá preferovaný jazyk. |
| Footer note, Logo | Tisknou se na faktuře a v PDF. Logo je PNG nebo JPEG do 1,9 MB, uložené jako PNG bez metadat. |
| Number format | Ve stylu Pythonu s `{year}` a `{seq}`: `{year}-{seq:04d}` dá `2026-0001`. Musí obsahovat `{seq}`. |
| Next number | Nastavuje se vystavováním faktur; nelze jej upravit, jinak by se čísla vydala znovu. |
| Underpayment allowance | Krypto platba, které chybí nejvýše **menší** z hodnot: tento podíl (bazické body; 50 je 0,5 %) a tato pevná částka v amerických dolarech (výchozí 1,00), stále vyrovná kurzovou nabídku. Pohltí poplatek burzy za výběr. |
| Overpayment ceiling | Krypto platba, která převyšuje nabízenou částku o víc než tento podíl (1000 je 10 %), se neuhradí, ale odloží k posouzení člověkem. |
| Accounting currency | Měna účetních sloupců v exportech (kapitola 8). |
| Hosts a Back button may lead to | Jeden host na řádek. Faktura vytvořená přes API může mít `return_url` přesně na tyto hosty. |

![Organizace](../img/org-edit.png)

### Vystavovatel pro americké daňové rezidenty

Označte **US-resident issuer** nejvýše u jedné organizace. Je to organizace, která musí vystavovat faktury klientům, kteří jsou američtí daňoví rezidenti.

- Každý kontakt má odpověď **US tax resident**: yes, no nebo unknown. Formulář faktury vás nutí vybrat; platební odkaz se ptá kupujícího.
- Když jiná organizace vyžádá fakturu pro kontakt, který je americký daňový rezident, fakturu **vystaví organizace-vystavovatel** se svým číslováním, bankovními účty a kanály. Vystavovatel si vede vlastní kopii kontaktu; váš kontakt se nikdy nepřepisuje.
- Pokud se kontakt stane americkým daňovým rezidentem po vystavení faktury jinou organizací, faktura se zobrazí ve frontě **US tax residents with invoices from another issuer** v Needs a person: stornujte ji a vytvořte znovu.
- Není-li žádná organizace označena, takovou fakturu nelze vůbec vytvořit.

Změna příznaku nebo návratových hostů patří mezi změny se zesíleným ověřením. Příznak je v audit logu a posílá se e-mailem superuživatelům.

## Bankovní účty a fiat odkazy

Dole u organizace jsou dva seznamy. Oba se **deaktivují, nikdy nemažou**: vystavená faktura nadále zobrazuje účet, který uváděla.

![Bankovní účty a platební odkazy organizace](../img/org-accounts.png)

**Add bank account** vyžaduje označení, měnu, příjemce, IBAN nebo číslo účtu, banku, její SWIFT/BIC a volitelně zprostředkující banku. Bankovní účet se nabízí na fakturách **ve své měně**. *Pokyny k platbě* existují po jednom pro každý jazyk: platební stránka zobrazí pokyny v jazyce čtenáře a jako záložní anglické; jazyk nechte prázdný, chcete-li jej vynechat.

![Nový bankovní účet](../img/bank-new.png)

**Add payment link** (*fiat odkaz*, nezaměňovat se sdílitelnými stránkami z kapitoly 5) je odkaz na službu, která přijme platbu jinde: Revolut, N26, PayPal, Wise nebo „other". Vyžaduje adresu `https://` a měny, které umí přijmout. Platební stránka jej zobrazí v záložce **Online platba**.

![Nový fiat odkaz](../img/fiat-new.png)

## Platební kanály (rails)

**Rail account** je jeden způsob, jak dostat zaplaceno v kryptoměně nebo přes Lightning. Aktivní účet je nabízen na každé nové faktuře své organizace (pokud faktura nepojmenuje své metody sama). Otevřete **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** se nejprve ptá na organizaci a druh.

![Výběr druhu](../img/rail-new-choose.png)

| Druh | Co vyžaduje |
|---|---|
| **evm** | Jednu adresu; sítě ke sledování (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) a přijímaná aktiva (ETH, USDC, USDT). |
| **tron** | Jednu adresu; USDT. Sleduje se celá síť Tron. |
| **solana** | Jednu adresu; USDC a USDT. |
| **ton** | Zatím vypnuto: lze uložit jen jako neaktivní, protože zatím nelze prokázat, že platba přes TON dorazila. |
| **btcpay** | `URL` a `Store ID` BTCPay, aktiva (BTC, XMR, ZEC) a politiku rychlosti: `MediumSpeed` (1 potvrzení, výchozí), `LowMediumSpeed` (2) nebo `LowSpeed` (6). `HighSpeed` se odmítá. |
| **lightning** | Backend: `nwc` (Nostr Wallet Connect, např. Alby Hub), `lnd`, `cln` nebo `lnbits`; u posledních tří také `URL`. Aktivum BTC. |

![Nový EVM účet](../img/rail-new-evm.png)

![Nový Lightning účet](../img/rail-new-lightning.png)

Co je dobré vědět:

- **Používejte adresy, které používá jen tento systém.** Platba na blockchainu se páruje s fakturou podle **částky**: každá kurzová nabídka žádá základní částku plus pár jednotek „prachu", jedinečných mezi nabídkami držícími tuto adresu. Peníze poslané tam z jiného důvodu skončí v Needs a person.
- URL kanálu musí být adresa `https://` na hostu, který se překládá výhradně na veřejné adresy; loopback nebo soukromá adresa se odmítá (jedinou výjimkou je LND nebo CLN na stejném stroji, na portu `LOCAL_LIGHTNING_PORT`).
- Dokud klient může ještě zaplatit nabídku, nelze změnit organizaci, druh, adresu, sítě ani aktiva účtu; vytvořte raději nový účet. **Deactivate** přestane účet nabízet, ale už zobrazené nabídky se sledují, dokud se neuvolní.
- Sloupec *Watcher* říká, zda worker umí číst daný blockchain nebo bránu.

![Účet BTCPay](../img/rail-edit-btcpay.png)

### Přístupové údaje

Tajemství BTCPay a Lightning jsou zašifrována klíčem trezoru a **už se nikdy nezobrazí**. Otevřete účet a stiskněte **Set credentials** (trezor musí být odemčený), nebo použijte v shellu `manage.py set_rail_credentials <id rail účtu>`. Prázdné pole ponechá uloženou hodnotu.

Systém **nikdy neutrácí**, proto ukládá jen údaj, o němž lze dokázat, že to neumí:

| Backend | Co vložit |
|---|---|
| BTCPay | `api_key` pouze s `btcpay.store.canviewinvoices` a `btcpay.store.cancreateinvoice` (BTCPay se ptá, co klíč smí) a `webhook_secret`. Tlačítko **Register the BTCPay webhook** vytvoří webhook v BTCPay a uloží jeho tajemství za vás. |
| Lightning, NWC | `nwc_url` připojení s `make_invoice` a `lookup_invoice` a **bez** metody `pay_*`. |
| Lightning, LND | `macaroon` vytvořený s `invoices:read invoices:write info:read` (vlastní `invoice.macaroon` LND se odmítá). |
| Lightning, Core Lightning | `rune` omezený na `invoice` a `listinvoices`. |
| Lightning, LNbits | **Invoice/read key** peněženky, nikdy Admin key; zaškrtněte políčko, že jste to ověřili. |

![Přístupové údaje BTCPay](../img/rail-credentials-btcpay.png)

![Přístupové údaje Lightning](../img/rail-credentials-lightning.png)

Změna hostu v URL kanálu smaže jeho uložené údaje a deaktivuje jej, takže se klíč nikdy neodešle jiné službě. README ([Crypto rails](../../../README.md#crypto-rails)) obsahuje podrobnosti pro každý backend, proměnné prostředí pro vlastní RPC endpointy a to, co watchery nevidí.

## API tokeny a rozsahy (scopes)

**Settings > API tokens**. Token patří **jedné organizaci** a vidí jen její data. Zobrazí se **jednou**, při vytvoření; pokud se ztratí, zrušte jej a vytvořte nový. Zrušení je trvalé a řádek zůstává.

![Tokeny](../img/tokens.png)

| Scope | Povoluje |
|---|---|
| `read` | Každé GET. |
| `invoices:write` | Vytvářet a upravovat kontakty a jejich šifrovací klíče; vytvářet, odesílat a stornovat faktury; vydávat nové odkazy; spravovat platební odkazy. |
| `payments:confirm` | Potvrzovat, odmítat, vracet a přiřazovat platby, potvrzovat vrácení přeplatku, zamítat prohlášení o platbě. **Mění stav peněz.** Nemůže zaznamenat ruční platbu: to může jen člověk. |
| `admin` | Vše výše a koncové body webhooků. |

Dejte tokenu nejmenší potřebnou množinu; integrace, která jen vytváří a odesílá faktury, potřebuje `read` a `invoices:write`. Vytvoření, změna rozsahů a zrušení patří mezi změny se zesíleným ověřením.

![Token, zobrazený jednou](../img/token-created.png)

Token můžete vydat i z shellu: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooky

**Settings > Webhooks** zaregistruje koncový bod, kterému se oznamuje, co se děje s fakturami a platbami organizace.

![Webhooky](../img/webhooks.png)

- Adresa musí být `https://`, na portu 443, na hostu, který se překládá výhradně na veřejné adresy. (Vývojové prostředí to může uvolnit přes `WEBHOOK_ALLOW_HTTP` a `WEBHOOK_ALLOW_PRIVATE`.)
- Zaškrtněte události, které chcete; nezaškrtnutí ničeho znamená všechny: `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert` a `ping`.
- **Tajemství** se zobrazí jednou. Každé doručení je podepsaný `POST` s `X-Pay-Signature: sha256=<hex>`, což je HMAC-SHA256 surového těla s tímto tajemstvím; zkontrolujte jej dřív, než volání uvěříte. Tělo nese jen id a stavy, nikdy jména, adresy ani částky: zbytek si přečtěte přes API.
- Doručení se opakuje po 2, 5, 15, 30, 60, 120, 240 a 480 minutách, pak se označí jako neúspěšné; zaměstnanci se o tom dozvědí jednou a zobrazí se ve frontě **Webhooks not delivered** v Needs a person.
- Stránka koncového bodu má **Send a ping**, poslední doručení a **Delete**. Settings > Webhook deliveries vypisuje každé doručení s tlačítkem **Retry now**.

![Koncový bod a jeho doručení](../img/webhook-detail.png)

Úplný seznam událostí a kód k ověření podpisu je v `/llms.txt` vaší instalace (kapitola 9).

## Tým

**Settings > Team** vypisuje všechny, kdo se mohou přihlásit, se jmény jejich druhých faktorů a posledním přihlášením. Superuživatel může odstranit ztracený faktor kolegy (změna se zesíleným ověřením, e-mailem oznámená každému superuživateli); kolega je pak zamčený, dokud někdo na serveru nezaregistruje nový autentifikátor. Váš vlastní poslední faktor odstranit nelze.

![Tým](../img/team.png)
