# 7. Migrace ze Zaprite a Request Finance

Pokud jste dříve používali Zaprite nebo Request Finance, můžete přenést klienty a historii faktur, aby byl Sealbill jediným místem, kde se dá najít všechno. Postup krok za krokem včetně toho, jak dostat data z každé služby, je v [docs/migration.md](../../migration.md). Tato kapitola říká, co import dělá a co ne.

## Zásada: historie, ne živé faktury

**Každá migrovaná faktura je zaznamenána jako již zaplacená.** V Sealbillu nelze zaplatit nic, co bylo jinde otevřené, takže klient nikdy nezaplatí dvakrát a nikdo není kontaktován. Každá faktura si ponechává původní číslo, datum vystavení, měnu, položky, název a poznámku v patičce.

- Každá dostane přesně jednu platbu: způsob *Recorded by hand*, potvrzenou, na celou částku, s datem zaplacení ze zdroje (nebo datem vystavení). Platba nese poznámku s názvem zdroje a původním číslem.
- Stav zdroje (`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment`, …) se uchová pro zaměstnance a klientovi se nikdy nezobrazí. CSV faktur má sloupec **migrated_from** (např. `zaprite:COMPLETE`), takže účetnictví umí rozlišit platbu, kterou klient skutečně provedl, od faktury, kterou jste prohlásili za zaplacenou.
- Migrovaná faktura je **jen ke čtení**: nelze z ní nic odeslat, zaplatit, zrušit, stornovat, vydat nové odkazy, vytvořit nabídku ani smazat, ani zaměstnancem, ani přes API. Její stránka říká, odkud pochází, a odkazuje na originál, dokud ten účet existuje.
- **Nic se nespustí.** Import neodešle e-mail, nevyšle webhook, nevytvoří nabídku ani odkaz a neposune následující číslo organizace.

## Co se stane s kontakty

Kontakty se **slučují podle e-mailové adresy v rámci organizace** (bez ohledu na velikost písmen): jedna osoba, která se objeví v Zaprite i v Request Finance, se stane jedním kontaktem, doplněným z toho zdroje, který má údaje. Kontakt bez e-mailu se zachová a přiřadí se jen k fakturám, které na něj ukazují; nikdy se nesluče podle jména. Záznamy ze Zaprite a Request Finance jdou do organizace, kterou uvedete; vystavovatel pro USA odmítá záznamy ze Zaprite, pokud nepřidáte `--allow-us-issuer`.

## Čísla

Zachová se vlastní číslo zdroje. Když číslo v organizaci už existuje (Zaprite i Request Finance číslovaly jednoduše), tomu, který přijde druhý, se připojí značka zdroje: `591-RF` (nebo `-Z`). Zkušební běh vypíše každou kolizi dřív, než se cokoli zapíše. Číslo, které vypadá jako vlastní číslování organizace a není pod jejím následujícím číslem, se odmítne a rozhodnete ručně.

## Spuštění importu

Importér čte na standardním vstupu **JSON po řádcích** (newline-delimited): jeden záznam `contact` nebo `invoice` na řádek.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- Částky jsou v minor units; `total_minor` se musí rovnat tomu, co vypočítají položky (daň a slevy na položku se přenášejí), jinak se záznam odmítne.
- `address` kontaktu má přesně klíče `line1`, `line2`, `city`, `zip`, `state` a `country` (dvoupísmenný kód). Jakýkoli jiný klíč se odmítne jménem, takže se pole nikdy neztratí bez povšimnutí.
- Každá faktura se zapisuje v konečném stavu ve vlastní transakci.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

Trezor musí být odemčený. **Vždy nejprve spusťte `--dry-run`**: udělá vše, vrátí zpět a vypíše jen počty:

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

- Výstup nikdy nevypíše jméno, adresu, e-mail ani částku na klienta. Odmítnutý nebo přeskočený záznam je pojmenován `<source>:<source_id>` a číslem řádku, abyste jej našli v exportu.
- **Dá se spustit znovu.** Faktura nebo kontakt, jehož zdrojové id už bylo importováno, se přeskočí, takže přerušený běh stačí zopakovat. Dva souběžné běhy se odmítnou.
- Záznamy konceptů, zrušených a odmítnutých faktur z Request Finance se přeskočí: nikdo je nedlužil.
- Audit log má jeden záznam na běh jen s počty a jeden na fakturu se zdrojem a jeho id.
- `--btc-check id,id` vypíše pro zdrojová id, která uvedete, částku v satoshi a URL zdroje ke kontrole bitcoinové faktury proti účetnictví.

Po importu zkontrolujte jednu až dvě migrované faktury na stránce **Invoices** a stáhněte CSV faktur za období (kapitola 8); součty podle měny vypsané zkušebním během by měly odpovídat číslům zdroje.
