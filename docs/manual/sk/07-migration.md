# 7. Migrácia zo Zaprite a Request Finance

Ak ste predtým používali Zaprite alebo Request Finance, môžete preniesť klientov a históriu faktúr, aby bol Sealbill jediným miestom, kde sa dá nájsť všetko. Postup krok za krokom vrátane toho, ako dostať dáta z každej služby, je v [docs/migration.md](../../migration.md). Táto kapitola hovorí, čo import robí a čo nie.

## Zásada: história, nie živé faktúry

**Každá migrovaná faktúra je zaznamenaná ako už zaplatená.** V Sealbille nemožno zaplatiť nič, čo bolo inde otvorené, takže klient nikdy nezaplatí dvakrát a nikto nie je kontaktovaný. Každá faktúra si ponecháva pôvodné číslo, dátum vystavenia, menu, položky, názov a poznámku v pätičke.

- Každá dostane presne jednu platbu: spôsob *Recorded by hand*, potvrdenú, na celú sumu, s dátumom zaplatenia zo zdroja (alebo dátumom vystavenia). Platba nesie poznámku s názvom zdroja a pôvodným číslom.
- Stav zdroja (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) sa uchová pre zamestnancov a klientovi sa nikdy nezobrazí. CSV faktúr má stĺpec **migrated_from** (napr. `zaprite:COMPLETE`), takže účtovníctvo vie rozlíšiť platbu, ktorú klient skutočne uskutočnil, od faktúry, ktorú ste vyhlásili za zaplatenú.
- Migrovaná faktúra je **iba na čítanie**: nemožno z nej nič odoslať, zaplatiť, zrušiť, stornovať, vydať nové odkazy, vytvoriť ponuku ani zmazať, ani zamestnancom, ani cez API. Jej stránka hovorí, odkiaľ pochádza, a odkazuje na originál, kým ten účet existuje.
- **Nič sa nespustí.** Import neodošle e-mail, nevyšle webhook, nevytvorí ponuku ani odkaz a neposunie nasledujúce číslo organizácie.

## Čo sa stane s kontaktmi

Kontakty sa **zlučujú podľa e-mailovej adresy v rámci organizácie** (bez ohľadu na veľkosť písmen): jedna osoba, ktorá sa objaví v Zaprite aj v Request Finance, sa stane jedným kontaktom, doplneným z toho zdroja, ktorý má údaje. Kontakt bez e-mailu sa zachová a priradí sa iba k faktúram, ktoré na neho ukazujú; nikdy sa nezlučuje podľa mena. Záznamy zo Zaprite a Request Finance idú do organizácie, ktorú uvediete; vystavovateľ pre USA odmieta záznamy zo Zaprite, ak nepridáte `--allow-us-issuer`.

## Čísla

Zachová sa vlastné číslo zdroja. Keď číslo v organizácii už existuje (Zaprite aj Request Finance číslovali jednoducho), tomu, ktorý príde druhý, sa pripojí značka zdroja: `591-RF` (alebo `-Z`). Skúšobný beh vypíše každú kolíziu skôr, než sa čokoľvek zapíše. Číslo, ktoré vyzerá ako vlastné číslovanie organizácie a nie je pod jej nasledujúcim číslom, sa odmietne a rozhodnete ručne.

## Spustenie importu

Importér číta na štandardnom vstupe **JSON po riadkoch** (newline-delimited): jeden záznam `contact` alebo `invoice` na riadok.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Sumy sú v minor units; `total_minor` sa musí rovnať tomu, čo vypočítajú položky (daň a zľavy na položku sa prenášajú), inak sa záznam odmietne.
- `address` kontaktu má presne kľúče `line1`, `line2`, `city`, `zip`, `state` a `country` (dvojpísmenový kód). Akýkoľvek iný kľúč sa odmietne menom, takže sa pole nikdy nestratí bez povšimnutia.
- Každá faktúra sa zapisuje v konečnom stave vo vlastnej transakcii.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

Trezor musí byť odomknutý. **Vždy najprv spustite `--dry-run`**: urobí všetko, vráti späť a vypíše iba počty:

```text
DRY RUN: nothing was written (source zaprite, org example-studio)
contacts: new 1, merged 0, without e-mail 0, skipped (already imported) 0
invoices invoice EUR: 1
invoices payment_request USD: 1
total EUR: 133700 minor units
total USD: 5000 minor units
skipped, already imported: 0
source status COMPLETE: 1
source status PAID: 1
number collisions: 0
refused lines: 1
  line 4 zaprite:bad: the lines do not add up to total_minor
```

- Výstup nikdy nevypíše meno, adresu, e-mail ani sumu na klienta. Odmietnutý alebo preskočený záznam je pomenovaný `<source>:<source_id>` a číslom riadku, aby ste ho našli v exporte.
- **Dá sa spustiť znova.** Faktúra alebo kontakt, ktorého zdrojové id už bolo importované, sa preskočí, takže prerušený beh stačí zopakovať. Dva súbežné behy sa odmietnu.
- Záznamy konceptov, zrušených a odmietnutých faktúr z Request Finance sa preskočia: nikto ich nedlhoval.
- Audit log má jeden záznam na beh iba s počtami a jeden na faktúru so zdrojom a jeho id.
- `--btc-check id,id` vypíše pre zdrojové id, ktoré uvediete, sumu v satoshi a URL zdroja na kontrolu bitcoinovej faktúry voči účtovníctvu.

Po importe skontrolujte jednu-dve migrované faktúry na stránke **Invoices** a stiahnite CSV faktúr za obdobie (kapitola 8); súčty podľa meny vypísané skúšobným behom by mali zodpovedať číslam zdroja.
