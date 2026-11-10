# 3. Kontakty

**Kontakt** je klient organizace: jména, jedna či více e-mailových adres, poštovní adresa, daňové identifikátory, telefon, preferovaný jazyk, poznámka a odpověď na „US tax resident?". Každý kontakt patří jedné organizaci. Otevřete **Contacts** v pruhu vlevo.

![Seznam kontaktů](../img/contacts.png)

- Hledejte podle **přesné e-mailové adresy**; filtrujte podle organizace.
- Seznam ukazuje **Clients and link buyers**, **Clients of record** nebo **Link buyers**. *Link buyer* je kontakt, který vytvořil checkout platebního odkazu z toho, co návštěvník napsal; do veřejného formuláře může kdokoli napsat cokoli, proto to není klient v evidenci (client of record), dokud jej člověk nepovýší (kapitola 5).
- Kontakt, který vidíte dvakrát se stejným jménem, jednou pod každou organizací, je **kopie klienta u vystavovatele pro USA** (kapitola 2): vystavovatel si vede vlastní kopii, aby se data první organizace nikdy nepřepisovala.

## Vytvoření a změna kontaktu

Zřídka jej vytváříte samostatně. Zadáním e-mailové adresy u **New invoice** se najde kontakt s touto adresou nebo se z napsaného vytvoří nový; to, co necháte prázdné, nikdy nepřepíše uložené údaje. Kontakty vytváří také API a importér.

**Contact > Edit** mění jméno, telefon, preferovaný jazyk, odpověď o USA, daňové identifikátory, poznámky a adresu. Organizaci změnit nelze. Snížení uložené odpovědi o USA (z „yes") se zapíše do audit logu.

![Úprava kontaktu](../img/contact-edit.png)

![Kontakt](../img/contact.png)

Na stránce kontaktu seznam **E-mail addresses** přidává adresu, nastavuje jednu jako **Primary** a odstraňuje adresy. E-maily, platební odkazy a PDF chodí na primární adresu, takže přidání nebo povýšení adresy je změna se [zesíleným ověřením](01-getting-started.md). Primární adresu, poslední adresu ani adresu, kterou uvádí aktivní šifrovací klíč, odstranit nelze.

## Šifrovací klíče kontaktu

Všechno uložené o kontaktu je už v klidu zašifrováno klíčem trezoru (kapitola 1). Kontakt může mít i vlastní klíč, aby se **e-maily, které mu posíláte**, šifrovaly na něj: **veřejný klíč PGP** nebo **certifikát S/MIME**. Otevřete **Contact > Encryption keys**.

![Kontakt bez klíče](../img/contact-keys-empty.png)

- **Nahrání zaměstnancem.** Vložte klíč nebo vyberte soubor. Musí uvádět jednu z adres kontaktu, nesmí být expirovaný a musí to být veřejný klíč nebo certifikát (soukromý klíč nebo soubor `.p12` se odmítne). Okamžitě se stane **aktivním** klíčem a nahradí současný; klient nemusí nic potvrzovat.
- **Nahrání klientem.** Každá stránka pro klienta má **Dostávat šifrované e-maily**. Klíč, který tam klient nahraje, je **pending** (čekající): čeká, dokud klient neodpoví na e-mail zašifrovaný na něj, čímž prokáže, že drží soukromou polovinu. Měl-li kontakt už aktivní klíč, držitel tohoto klíče dostane oznámení s odkazem ke **zrušení**, aby někdo, kdo čte schránku, nemohl nenápadně podstrčit vlastní klíč.

  ![Stránka klienta pro nahrání klíče](../img/portal-key-upload.png)

- Používá se jen **aktivní** klíč, jeden na kontakt. Jeho stav je `pending`, `active`, `replaced`, `revoked` nebo `failed`. **Revoke** jej stáhne; e-maily pak chodí nešifrované.
- Klíč, který **přestane fungovat během aktivity** (vypršel nebo byl odvolán u zdroje), se označí `failed` a e-maily tomuto kontaktu se **odmítnou, nikoli odešlou nešifrované**. Je ve frontě Needs a person v části *Encryption keys that stopped working*; nahrajte funkční klíč nebo selhavší odvolejte a pak zprávy zopakujte. Klíč, který selže ještě ve stavu pending, nic nezastaví. Needs a person vypisuje také *klíče expirující do 30 dnů*.
- Kopie kontaktu u vystavovatele automaticky sleduje klíč originálu.

![Kontakt s aktivním klíčem PGP](../img/contact-keys-active.png)

Totéž lze udělat přes API: `POST /api/v1/contacts/{id}/keys` s `{"key": "<armored key>"}` (scope `invoices:write`) a `POST /api/v1/keys/{id}/revoke`.
