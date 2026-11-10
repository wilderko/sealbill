# 1. Začínáme

## Co provozujete

Sealbill je jedna webová aplikace a jeden worker na pozadí.

- **Webová aplikace** obsluhuje stránky pro zaměstnance (v kořeni webu, např. `https://pay.example.org/`), stránky pro klienta (`/i/…` zobrazí fakturu, `/p/…` je místo, kde se platí, `/l/…` je platební odkaz), API (`/api/v1/…`) a stránku odemknutí (`/unlock/`).
- **Worker** (`manage.py run_worker`) dělá vše, co se musí dít bez kliknutí: odesílá e-maily z fronty, označuje faktury po splatnosti, sleduje blockchainy a brány kvůli platbám, ukončuje kurzové nabídky, posílá jednu připomínku platby a doručuje webhooky. **Bez workeru se nic neodesílá a nezaznamená se žádná krypto platba.** Spouštějte jej jako druhý proces vedle webové aplikace; `manage.py run_worker --once` provede každou úlohu jednou a skončí, což se hodí k testu.

## Instalace a první spuštění

README repozitáře obsahuje příkazy pro každý způsob provozu, proto je tu neopakujeme:

- na vlastním počítači, k vyzkoušení: [README, Development](../../../README.md#development)
- s Docker Compose: [README, Docker Compose](../../../README.md#docker-compose)
- na serveru se systemd, Apache a PostgreSQL: [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Ať zvolíte cokoli, pořadí prvního spuštění je stejné:

1. Vytvořte databázové tabulky (`migrate` a `createcachetable` pro tabulky s limity požadavků).
2. Vytvořte trezor a odemkněte jej (další část).
3. Vytvořte prvního uživatele příkazem `manage.py createsuperuser`.
4. Dejte mu druhý faktor: `manage.py create_staff_totp <username>` vypíše adresu `otpauth://` pro aplikaci autentifikátoru. Trezor musí být odemčený. Passkey můžete přidat později na stránce Security.
5. Spusťte webovou aplikaci a worker, otevřete stránku a přihlaste se.
6. V **Settings** vytvořte organizaci a bankovní účet nebo platební kanál (kapitola 2) a pošlete si první fakturu (kapitola 4).

## Trezor

Všechno, co člověk napsal (jména, adresy, daňové identifikátory, položky faktur, poznámky, přístupové údaje vašich platebních bran), je uloženo zašifrované. Klíč se odvozuje z **hesla trezoru** a je uložen jen v souboru v paměti určeném cestou `VAULT_KEY_PATH`; nikdy ne v prostředí ani na disku.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # jednou; alespoň 16 znaků
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # po každém restartu stroje
```

Heslo zadávejte na standardní vstup, jako výše, aby se nikdy neobjevilo v příkazové řádce ani v historii shellu. `init_vault` jen vytvoří `vault.json` (sůl a kontrolní hodnotu, ne klíč); `unseal_vault` jej otevře.

Po restartu stroje nebo po smazání souboru v paměti je trezor **zapečetěný**. Postupné znovunačtení webové služby jej nechá otevřený; tvrdý restart ne. Dokud je zapečetěný:

- každá stránka pro zaměstnance přesměruje na **/unlock/**: zadejte tam heslo trezoru nebo spusťte `unseal_vault`;
- platební stránky, platební odkazy a API odpovídají „dočasně nedostupné" (503), takže žádný klient nezíská kurzovou nabídku;
- worker stále vidí a páruje platby na blockchainu (čte jen nešifrované sloupce), ale e-maily a webhooky čekají a sledování bran (BTCPay, Lightning) se pozastaví; operátor dostane jeden upozorňující e-mail za každou zapečetěnou epizodu.

![Stránka odemknutí, zobrazená při zapečetěném trezoru](../img/vault-unlock.png)

`GET /healthz` hlásí `"vault": "sealed"` nebo `"unsealed"` bez jakýchkoli dat, takže jej může sledovat monitoring. Uchovejte kopii `vault.json`, zálohovací frázi a heslo trezoru někde mimo server: databázi bez všech tří nelze přečíst. Úplný seznam toho, co běží při zapečetěném trezoru, je v [README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Přihlášení

Zaměstnanci se přihlašují na `/login/` ve dvou krocích: **heslo** a poté **druhý faktor**, kterým je passkey nebo šestimístný kód z aplikace autentifikátoru. Relace trvá osm hodin a končí zavřením prohlížeče. Přihlášení má omezený počet pokusů na adresu, na účet a na zařízení.

![Krok s heslem](../img/start-login.png)

![Krok s druhým faktorem](../img/start-verify.png)

Má-li účet passkey, druhý krok nabídne nejprve **Use a passkey** a pod ním pole pro kód („or enter a code"). Passkey patří jen této stránce, takže jej nemůže použít falešná stránka.

![Druhý krok účtu s passkey](../img/verify-passkey.png)

Po přihlášení se dostanete na **Needs a person** (kapitola 6). Pruh vlevo je celá navigace: Needs a person, Invoices, New invoice, Contacts, Payment links, Exports, Settings; štít dole je **Security**, šipka vás odhlásí.

![Needs a person, první stránka po přihlášení](../img/needs-person.png)

### Security: vlastní passkeys a autentifikátory

**Security** (ikona štítu) vypisuje vaše druhé faktory. Passkey přidáte tak, že mu dáte jméno (např. „MacBook Touch ID" nebo „YubiKey 5C") a stisknete **Add a passkey**; prohlížeč se pak zeptá na otisk, obličej nebo PIN. Aplikace autentifikátoru se přidává na serveru příkazem `create_staff_totp`, nikdy v prohlížeči. Poslední druhý faktor účtu nelze odstranit a odstranění faktoru, kterým je přihlášena aktuální relace, nejprve vyžaduje jiný. Každé přidání i odstranění je v audit logu.

![Security s přidaným passkey](../img/security-passkey.png)

## Zaměstnanci a superuživatelé

Každý s účtem zaměstnance může vystavovat faktury, zaznamenávat platby a vyřizovat fronty. Některé změny mohou přesunout peníze nebo přesměrovat klienta, a ty vyžadují víc.

### Zesílené ověření (step-up)

Tyto změny vyžadují **superuživatele**, který zadal druhý faktor **v posledních deseti minutách**:

- vytvoření, změna rozsahů (scopes) nebo zrušení API tokenu;
- účet platebního kanálu a jeho přístupové údaje (a webhook BTCPay, který ukládá tajemství);
- příznak vystavovatele pro americké daňové rezidenty a návratové hosty organizace;
- bankovní účet nebo fiat platební odkaz organizace;
- přidání e-mailové adresy ke kontaktu nebo její nastavení jako primární (chodí na ni e-maily, platební odkazy a PDF);
- odstranění druhého faktoru jiného zaměstnance.

Stránky řeknou, jak na tom jste, ještě než cokoli stisknete:

- zaměstnanec, který není superuživatel, uvidí „Only a superuser may …" a nic se nezmění;

  ![Zaměstnanec, který není superuživatel](../img/stepup-not-superuser.png)

- superuživatel, jehož poslední druhý faktor je starší než deset minut, uvidí **Verify again**; tlačítko vede na ověřovací stránku a vrátí vás na původní stránku.

  ![Změna, která vyžaduje čerstvý druhý faktor](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Každá změna se zesíleným ověřením pošle také e-mail každému aktivnímu superuživateli: kdo, co a kdy, podle jmen a id, nikdy ne tajemství, číslo účtu ani adresa. Pokud ukradená relace něco změní, vlastník systému se to dozví.

Stránka **Team** (Settings > Team) vypisuje všechny, kdo se mohou přihlásit, jejich druhé faktory a poslední přihlášení, a superuživateli umožňuje odstranit ztracený faktor kolegy. Nové zaměstnance vytvoříte příkazem `createsuperuser` nebo ve skrytém Django adminu (cesta v `DJANGO_ADMIN_PATH`; jen superuživatel s druhým faktorem, ostatní dostanou 404).
