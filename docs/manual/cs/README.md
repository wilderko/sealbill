# Uživatelská příručka Sealbill

Sealbill je samostatně hostovaný fakturační systém pro lidi, kteří přijímají kryptoměny, bankovní převody a platební odkazy vedle sebe a chtějí mít data klientů v klidu zašifrovaná. Tato příručka je pro ty, kdo jej provozují: pro zaměstnance, kteří vystavují faktury, a pro operátora, který jej instaluje.

Jazyky: [English](../en/README.md) · [Slovenčina](../sk/README.md) · **Čeština** · [Deutsch](../de/README.md) · [Español](../es/README.md) · [Français](../fr/README.md) · [Polski](../pl/README.md) · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Kapitoly

1. [Začínáme](01-getting-started.md): instalace, trezor, přihlášení, zesílené ověření u citlivých změn
2. [Nastavení](02-settings.md): organizace, bankovní účty, platební kanály (rails), API tokeny, webhooky, tým
3. [Kontakty](03-contacts.md) včetně šifrovacích klíčů jednotlivých kontaktů
4. [Faktury](04-invoices.md): vytvoření, odeslání, stránky pro klienta, kurzové nabídky, částečné platby a přeplatky, storno, nové odkazy, připomínka
5. [Platební odkazy](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (co vyžaduje člověka)
7. [Migrace ze Zaprite a Request Finance](07-migration.md)
8. [Exporty a účetní měna](08-exports.md)
9. [API ve zkratce](09-api.md)

## Jak číst snímky obrazovky

- **Stránky pro zaměstnance** (vše za přihlášením) jsou vždy anglicky, ať klient mluví jakýmkoli jazykem. Názvy nabídek a tlačítek v této příručce jsou psány přesně tak, jak je aplikace zobrazuje.
- **Stránky pro klienta** (stránka faktury `/i/…`, platební stránka `/p/…`, stránky platebních odkazů, stránka odemknutí a e-maily) jsou přeloženy do jazyků en, sk, cs, de, es, fr, pl, ko a ru. Klient vidí jazyk kontaktu, poté jazyk organizace, poté jazyk prohlížeče; připojíte-li k odkazu klienta `?lang=de`, uvidíte jej v jiném jazyce.
- Všechna jména, e-mailové adresy, peněženky a částky na snímcích jsou vymyšlené: pocházejí z ukázkové organizace „Example Studio s.r.o." na jednorázové databázi.
- Text v `kódu` je to, co píšete nebo kopírujete. Příkazy jsou zapsány jako `manage.py …`; v Docker Compose je to `docker compose exec web python manage.py …` a pod systemd `payinv-manage …` (viz README repozitáře).
