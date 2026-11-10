# 3. Kontakty

**Kontakt** je klient organizácie: mená, jedna či viac e-mailových adries, poštová adresa, daňové identifikátory, telefón, preferovaný jazyk, poznámka a odpoveď na „US tax resident?". Každý kontakt patrí jednej organizácii. Otvorte **Contacts** v pruhu vľavo.

![Zoznam kontaktov](../img/contacts.png)

- Hľadajte podľa **presnej e-mailovej adresy**; filtrujte podľa organizácie.
- Zoznam ukazuje **Clients and link buyers**, **Clients of record** alebo **Link buyers**. *Link buyer* je kontakt, ktorý vytvoril checkout platobného odkazu z toho, čo návštevník napísal; do verejného formulára môže ktokoľvek napísať čokoľvek, preto to nie je klient v evidencii (client of record), kým ho človek nepovýši (kapitola 5).
- Kontakt, ktorý vidíte dvakrát s rovnakým menom, raz pod každou organizáciou, je **kópia klienta u vystavovateľa pre USA** (kapitola 2): vystavovateľ si vedie vlastnú kópiu, aby sa dáta prvej organizácie nikdy neprepisovali.

## Vytvorenie a zmena kontaktu

Zriedka ho vytvárate samostatne. Zadaním e-mailovej adresy pri **New invoice** sa nájde kontakt s touto adresou alebo sa z napísaného vytvorí nový; to, čo necháte prázdne, nikdy neprepíše uložené údaje. Kontakty vytvára aj API a importér.

**Contact > Edit** mení meno, telefón, preferovaný jazyk, odpoveď o USA, daňové identifikátory, poznámky a adresu. Organizáciu zmeniť nemožno. Zníženie uloženej odpovede o USA (z „yes") sa zapíše do audit logu.

![Úprava kontaktu](../img/contact-edit.png)

![Kontakt](../img/contact.png)

Na stránke kontaktu zoznam **E-mail addresses** pridáva adresu, nastavuje jednu ako **Primary** a odstraňuje adresy. E-maily, platobné odkazy a PDF idú na primárnu adresu, takže pridanie alebo povýšenie adresy je zmena so [zosilneným overením](01-getting-started.md). Primárnu adresu, poslednú adresu ani adresu, ktorú uvádza aktívny šifrovací kľúč, odstrániť nemožno.

## Šifrovacie kľúče kontaktu

Všetko uložené o kontakte je už v pokoji zašifrované kľúčom trezoru (kapitola 1). Kontakt môže mať aj vlastný kľúč, aby sa **e-maily, ktoré mu posielate**, šifrovali naň: **verejný kľúč PGP** alebo **certifikát S/MIME**. Otvorte **Contact > Encryption keys**.

![Kontakt bez kľúča](../img/contact-keys-empty.png)

- **Nahranie zamestnancom.** Vložte kľúč alebo vyberte súbor. Musí uvádzať jednu z adries kontaktu, nesmie byť expirovaný a musí to byť verejný kľúč alebo certifikát (súkromný kľúč alebo súbor `.p12` sa odmietne). Okamžite sa stane **aktívnym** kľúčom a nahradí súčasný; klient nemusí nič potvrdzovať.
- **Nahranie klientom.** Každá stránka pre klienta má **Dostávať šifrované e-maily**. Kľúč, ktorý tam klient nahrá, je **pending** (čakajúci): čaká, kým klient neodpovie na e-mail zašifrovaný naň, čím dokáže, že drží súkromnú polovicu. Ak kontakt už mal aktívny kľúč, držiteľ tohto kľúča dostane oznámenie s odkazom na **zrušenie**, aby niekto, kto číta schránku, nemohol nenápadne vložiť vlastný kľúč.

  ![Stránka klienta na nahranie kľúča](../img/portal-key-upload.png)

- Používa sa iba **aktívny** kľúč, jeden na kontakt. Jeho stav je `pending`, `active`, `replaced`, `revoked` alebo `failed`. **Revoke** ho stiahne; e-maily potom idú nešifrované.
- Kľúč, ktorý **prestane fungovať počas aktivity** (vypršal alebo bol odvolaný u zdroja), sa označí `failed` a e-maily tomuto kontaktu sa **odmietnu, nie odošlú nešifrované**. Je vo fronte Needs a person v časti *Encryption keys that stopped working*; nahrajte funkčný kľúč alebo zlyhaný odvolajte a potom správy zopakujte. Kľúč, ktorý zlyhá ešte v stave pending, nič nezastaví. Needs a person vypisuje aj *kľúče expirujúce do 30 dní*.
- Kópia kontaktu u vystavovateľa automaticky sleduje kľúč originálu.

![Kontakt s aktívnym kľúčom PGP](../img/contact-keys-active.png)

To isté možno urobiť cez API: `POST /api/v1/contacts/{id}/keys` s `{"key": "<armored key>"}` (scope `invoices:write`) a `POST /api/v1/keys/{id}/revoke`.
