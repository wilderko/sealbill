# Používateľská príručka Sealbill

Sealbill je samostatne hostovaný fakturačný systém pre ľudí, ktorí prijímajú kryptomeny, bankové prevody a platobné odkazy vedľa seba a chcú mať dáta klientov v pokoji zašifrované. Táto príručka je pre tých, ktorí ho prevádzkujú: pre zamestnancov, ktorí vystavujú faktúry, a pre operátora, ktorý ho inštaluje.

Jazyky: [English](../en/README.md) · **Slovenčina** · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) · [Español](../es/README.md) · [Français](../fr/README.md) · [Polski](../pl/README.md) · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Kapitoly

1. [Začíname](01-getting-started.md): inštalácia, trezor, prihlásenie, zosilnené overenie pri citlivých zmenách
2. [Nastavenia](02-settings.md): organizácie, bankové účty, platobné kanály (rails), API tokeny, webhooky, tím
3. [Kontakty](03-contacts.md) vrátane šifrovacích kľúčov jednotlivých kontaktov
4. [Faktúry](04-invoices.md): vytvorenie, odoslanie, stránky pre klienta, kurzové ponuky, čiastočné platby a preplatky, storno, nové odkazy, pripomienka
5. [Platobné odkazy](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (čo vyžaduje človeka)
7. [Migrácia zo Zaprite a Request Finance](07-migration.md)
8. [Exporty a účtovná mena](08-exports.md)
9. [API stručne](09-api.md)

## Ako čítať snímky obrazovky

- **Stránky pre zamestnancov** (všetko za prihlásením) sú vždy po anglicky, nech klient hovorí akýmkoľvek jazykom. Názvy menu a tlačidiel v tejto príručke sú napísané presne tak, ako ich aplikácia zobrazuje.
- **Stránky pre klienta** (stránka faktúry `/i/…`, platobná stránka `/p/…`, stránky platobných odkazov, stránka odomknutia a e-maily) sú preložené do jazykov en, sk, cs, de, es, fr, pl, ko a ru. Klient vidí jazyk kontaktu, potom jazyk organizácie, potom jazyk prehliadača; ak k odkazu klienta pridáte `?lang=de`, uvidíte ho v inom jazyku.
- Všetky mená, e-mailové adresy, peňaženky a sumy na snímkach sú vymyslené: pochádzajú z ukážkovej organizácie „Example Studio s.r.o." na jednorazovej databáze.
- Text v `kóde` je to, čo píšete alebo kopírujete. Príkazy sú zapísané ako `manage.py …`; v Docker Compose je to `docker compose exec web python manage.py …` a pod systemd `payinv-manage …` (pozri README repozitára).
