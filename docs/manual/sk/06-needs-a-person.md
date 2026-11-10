# 6. Needs a person

**Needs a person** je prvá stránka a tá, na ktorú treba dávať pozor. Vypisuje to, o čom by žiaden program nemal rozhodovať sám. Pruh vľavo ukazuje počet otvorených položiek; keď nie je čo robiť, stránka to povie.

![Fronty s položkami](../img/needs-person.png)

Fronty sa ukazujú iba vtedy, keď majú položky; **Show N queues** vypíše aj prázdne, aby ste videli, čo stránka sleduje.

![Všetky fronty](../img/needs-person-queues.png)

| Fronta | Čo to je a čo robiť |
|---|---|
| **Declared payments** | Klient stlačil „Oznámiť platbu" pri faktúre. Skontrolujte banku alebo blockchain. Zaznamenanie platby na faktúre vyhlásenie vybaví; **Dismiss declaration** na faktúre zatvorí nepravdivé. |
| **Payments to review** | Peniaze, ktoré nezodpovedali žiadnej faktúre alebo zodpovedali zle, s dôvodom: token sa neprijíma, žiadna otvorená faktúra nesedí, sedí viac než jedna, prišla po vypršaní ponuky, duplikát, ďaleko nad dlžnou sumou, faktúra už bola uzavretá, veľmi malý „dust" prevod (pravdepodobne spam typu address poisoning, zbalený), zahodená transakcia alebo hlásenie BTCPay o čiastočnej, oneskorenej, neplatnej alebo ručne označenej faktúre. **Confirm** ju na jej faktúru, **Reject** s poznámkou, alebo **Assign to an invoice** (číslo faktúry, suma a poznámka). S prevodom na blockchaine možno konať, keď je finálny. |
| **Waiting for staff confirmation** | Platby v Monero a Zcash (cez BTCPay) a peniaze zaznamenané ručne: nič ich nesleduje, preto skontrolujte peňaženku alebo banku a potom **Confirm** alebo **Reject**. Potvrdiť môže iba človek; žiaden API token nie. |
| **Gateway payments seen but not settled** | Platby BTCPay alebo Lightning, ktoré brána videla pred viac než šiestimi hodinami a nevyrovnala. |
| **Refunds due** | Krypto platba nad kurzovou ponukou. Pošlite rozdiel späť na uvedenú adresu, potom **Refunded** s poznámkou. |
| **Confirmed payments the gateway now calls invalid** | BTCPay ju vyrovnal a potom označil za neplatnú (double spend, reorg). Kód nič nezruší: platbu zrušte na jej faktúre, alebo ju ponechajte. |
| **Exchange rates refused** | Fiat časť kurzov je zastaraná alebo skočila. Skontrolujte kurz ECB; **accept** skok, ktorý je skutočný. Krypto ponuky v tej mene sa medzitým odmietajú. |
| **Watchers that could not look** | Čítač blockchainu alebo brány zlyháva desať minút alebo dlhšie (zamestnancom prišiel aj e-mail). |
| **Transfers seen but not recorded** | Prevod, ktorý watcher videl, sa zatiaľ nepodarilo zaznamenať; skúša sa pri každom prechode. |
| **Blocks not scanned, or to check** | Úsek blokov sa preskočil. Skontrolujte ho ručne a potom **Acknowledge** s tým, čo ste skontrolovali. |
| **Overpaid invoices** | Vráťte mimo systému alebo **Keep the overpayment** (kapitola 4). |
| **US tax residents with invoices from another issuer** | Faktúru stornujte a vytvorte znova; vystaví ju organizácia pre USA. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Kapitola 3. |
| **E-mail not delivered** | Správa, ktorá zlyhávala asi šestnásť hodín. **Retry**, alebo **Dismiss** s dôvodom (potom je zrušená nadobro). |
| **Payment links** | Zaplatená objednávka prekročila limit odkazu, vystavovateľ neponúka žiadnu z metód odkazu, zadaná odpoveď o USA protirečí uloženej, alebo žiadna organizácia nie je vystavovateľom pre USA. Stlačte **Resolved** s tým, čo ste skontrolovali. |
| **Webhooks not delivered** | Doručenie, ktoré zlyhalo po všetkých opakovaniach. **Retry** alebo **Dismiss** s dôvodom. |

Cez celú stránku platia dve pravidlá:

- Platbu, ktorá **čaká na človeka**, potvrdí alebo priradí iba prihlásený zamestnanec. Token, aj `admin`, dostane `409 needs_person` a nič sa nezmení. Aj zaznamenanie ručnej platby je práca pre človeka.
- Každá akcia žiada poznámku, kde na tom záleží, a každá akcia je v audit logu.

Tie isté fronty možno vybavovať cez API s `payments:confirm`, okrem tých, ktoré sú len pre človeka (kapitola 9). Dva zoznamy, ktoré použijete na zistenie, *prečo* niečo uviazlo, sú **Settings > E-mail outbox** (každý e-mail so stavom, počtom pokusov a dôvodom zlyhania; adresáti ani obsah sa nikdy nezobrazia) a **Settings > Webhook deliveries**.

![E-mail outbox](../img/outbox.png)

![Doručenia webhookov](../img/deliveries.png)
