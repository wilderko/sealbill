# 6. Needs a person

**Needs a person** je první stránka a ta, na kterou je třeba dávat pozor. Vypisuje to, o čem by žádný program neměl rozhodovat sám. Pruh vlevo ukazuje počet otevřených položek; není-li co dělat, stránka to řekne.

![Fronty s položkami](../img/needs-person.png)

Fronty se ukazují jen tehdy, když mají položky; **Show N queues** vypíše i prázdné, abyste viděli, co stránka sleduje.

![Všechny fronty](../img/needs-person-queues.png)

| Fronta | Co to je a co dělat |
|---|---|
| **Declared payments** | Klient stiskl „Oznámit platbu" u faktury. Zkontrolujte banku nebo blockchain. Zaznamenání platby na faktuře prohlášení vyřídí; **Dismiss declaration** na faktuře zavře nepravdivé. |
| **Payments to review** | Peníze, které neodpovídaly žádné faktuře nebo odpovídaly špatně, s důvodem: token se nepřijímá, žádná otevřená faktura nesedí, sedí víc než jedna, přišla po vypršení nabídky, duplikát, daleko nad dluženou částkou, faktura už byla uzavřena, velmi malý „dust" převod (pravděpodobně spam typu address poisoning, sbalený), zahozená transakce nebo hlášení BTCPay o částečné, opožděné, neplatné nebo ručně označené faktuře. **Confirm** ji na její fakturu, **Reject** s poznámkou, nebo **Assign to an invoice** (číslo faktury, částka a poznámka). S převodem na blockchainu lze jednat, když je finální. |
| **Waiting for staff confirmation** | Platby v Monero a Zcash (přes BTCPay) a peníze zaznamenané ručně: nic je nesleduje, proto zkontrolujte peněženku nebo banku a poté **Confirm** nebo **Reject**. Potvrdit může jen člověk; žádný API token ne. |
| **Gateway payments seen but not settled** | Platby BTCPay nebo Lightning, které brána viděla před více než šesti hodinami a nevyrovnala. |
| **Refunds due** | Krypto platba nad kurzovou nabídkou. Pošlete rozdíl zpět na uvedenou adresu, poté **Refunded** s poznámkou. |
| **Confirmed payments the gateway now calls invalid** | BTCPay ji vyrovnal a poté označil za neplatnou (double spend, reorg). Kód nic nezruší: platbu zrušte na její faktuře, nebo ji ponechte. |
| **Exchange rates refused** | Fiat část kurzů je zastaralá nebo skočila. Zkontrolujte kurz ECB; **accept** skok, který je skutečný. Krypto nabídky v té měně se mezitím odmítají. |
| **Watchers that could not look** | Čtečka blockchainu nebo brány selhává deset minut nebo déle (zaměstnancům přišel i e-mail). |
| **Transfers seen but not recorded** | Převod, který watcher viděl, se zatím nepodařilo zaznamenat; zkouší se při každém průchodu. |
| **Blocks not scanned, or to check** | Úsek bloků se přeskočil. Zkontrolujte jej ručně a poté **Acknowledge** s tím, co jste zkontrolovali. |
| **Overpaid invoices** | Vraťte mimo systém nebo **Keep the overpayment** (kapitola 4). |
| **US tax residents with invoices from another issuer** | Fakturu stornujte a vytvořte znovu; vystaví ji organizace pro USA. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Kapitola 3. |
| **E-mail not delivered** | Zpráva, která selhávala asi šestnáct hodin. **Retry**, nebo **Dismiss** s důvodem (pak je zrušena nadobro). |
| **Payment links** | Zaplacená objednávka překročila limit odkazu, vystavovatel nenabízí žádnou z metod odkazu, zadaná odpověď o USA odporuje uložené, nebo žádná organizace není vystavovatelem pro USA. Stiskněte **Resolved** s tím, co jste zkontrolovali. |
| **Webhooks not delivered** | Doručení, které selhalo po všech opakováních. **Retry** nebo **Dismiss** s důvodem. |

Přes celou stránku platí dvě pravidla:

- Platbu, která **čeká na člověka**, potvrdí nebo přiřadí jen přihlášený zaměstnanec. Token, i `admin`, dostane `409 needs_person` a nic se nezmění. I zaznamenání ruční platby je práce pro člověka.
- Každá akce žádá poznámku, kde na tom záleží, a každá akce je v audit logu.

Tytéž fronty lze vyřizovat přes API s `payments:confirm`, kromě těch, které jsou jen pro člověka (kapitola 9). Dva seznamy, které použijete ke zjištění, *proč* něco uvázlo, jsou **Settings > E-mail outbox** (každý e-mail se stavem, počtem pokusů a důvodem selhání; adresáti ani obsah se nikdy nezobrazí) a **Settings > Webhook deliveries**.

![E-mail outbox](../img/outbox.png)

![Doručení webhooků](../img/deliveries.png)
