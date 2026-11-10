# 2. Einstellungen

**Settings** ist der letzte Eintrag der Leiste. Die Übersichtsseite listet alles Folgende auf, dazu das Audit-Log, den E-Mail-Postausgang (E-Mail outbox) und die Webhook-Auslieferungen (Kapitel 6 nutzt sie).

![Die Übersicht von Settings](../img/settings-index.png)

Jede Änderung wird im **Audit log** festgehalten (Settings > Audit log): wer, was und welcher Datensatz, nie die eingegebenen Werte.

![Das Audit-Log](../img/audit.png)

Öffnen Sie einen Eintrag, um die Details zu lesen. Die in Kapitel 1 unter „Die erneute Bestätigung" genannten Änderungen verlangen einen Superuser mit frischem zweitem Faktor.

## Organisationen

Eine **Organisation** ist eine juristische Person, die Rechnungen ausstellt: ihr Name, ihre Adresse, Steuernummern, ihr Logo, die Rechnungsnummerierung und die Zahlungskonten, die zu ihr gehören. Eine zweite Organisation ist sinnvoll, wenn sich zwei Firmen eine Installation teilen, zum Beispiel eine in der EU und eine in den USA.

![Die Liste der Organisationen](../img/orgs-list.png)

**Settings > Organizations > New organization**, oder öffnen Sie eine bestehende zum Bearbeiten:

| Feld | Wirkung |
|---|---|
| Slug | Der Kurzname in der API und in den Befehlen (`example-studio`). |
| Legal name, Address, Tax ids | Werden im Block „Rechnungssteller" der Rechnung gedruckt. Steuernummern sind Freitext. |
| Email from | Der Absender der E-Mails der Organisation, zum Beispiel `Example Studio <billing@example.com>`. |
| Default language | Die Sprache der Kundenseiten und E-Mails, wenn der Kontakt keine bevorzugte Sprache hat. |
| Footer note, Logo | Werden auf Rechnung und PDF gedruckt. Das Logo ist ein PNG oder JPEG bis 1,9 MB und wird als PNG ohne Metadaten gespeichert. |
| Number format | Im Python-Stil mit `{year}` und `{seq}`: `{year}-{seq:04d}` ergibt `2026-0001`. Es muss `{seq}` enthalten. |
| Next number | Wird durch das Ausstellen von Rechnungen gesetzt; es lässt sich nicht bearbeiten, sonst würden Nummern doppelt vergeben. |
| Underpayment allowance | Eine Krypto-Zahlung, die um höchstens den **kleineren** Wert aus diesem Anteil (Basispunkte; 50 sind 0,5 %) und diesem festen Betrag in US-Dollar (standardmäßig 1,00) zu wenig beträgt, begleicht das Kursangebot trotzdem. Das fängt die Auszahlungsgebühr einer Börse ab. |
| Overpayment ceiling | Eine Krypto-Zahlung, die den im Kursangebot genannten Betrag um mehr als diesen Anteil (1000 sind 10 %) übersteigt, wird nicht verbucht, sondern einem Menschen vorgelegt. |
| Accounting currency | Die Währung der Buchhaltungsspalten in den Exporten (Kapitel 8). |
| Hosts a Back button may lead to | Ein Host pro Zeile. Eine über die API erstellte Rechnung darf ein `return_url` auf genau diese Hosts tragen. |

![Eine Organisation](../img/org-edit.png)

### Der Rechnungssteller für US-Steuerpflichtige

Setzen Sie **US-resident issuer** bei höchstens einer Organisation. Das ist die Organisation, die die Rechnungen von Kunden ausstellen muss, die US-Steuerresidenten sind.

- Jeder Kontakt hat eine Antwort **US tax resident**: yes, no oder unknown. Das Rechnungsformular zwingt zu einer Wahl; ein Zahlungslink fragt den Käufer.
- Verlangt eine andere Organisation eine Rechnung für einen Kontakt, der US-Steuerresident ist, **stellt die Issuer-Organisation sie aus**, mit ihrer Nummerierung, ihren Bankkonten und ihren Kanälen. Der Issuer führt eine eigene Kopie des Kontakts; Ihr Kontakt wird nie überschrieben.
- Wird ein Kontakt erst nach dem Ausstellen durch eine andere Organisation US-Steuerresident, steht die Rechnung in der Warteschlange **US tax residents with invoices from another issuer** unter Needs a person: stornieren und neu erstellen.
- Ist keine Organisation markiert, lässt sich so eine Rechnung gar nicht erstellen.

Das Kennzeichen oder die Rücksprung-Hosts zu ändern ist eine Step-up-Änderung. Das Kennzeichen steht im Audit-Log und geht per E-Mail an die Superuser.

## Bankkonten und Fiat-Links

Unten bei einer Organisation stehen zwei Listen. Beide werden **deaktiviert, nie gelöscht**: Eine ausgestellte Rechnung zeigt weiter das Konto, das sie genannt hat.

![Bankkonten und Zahlungslinks einer Organisation](../img/org-accounts.png)

**Add bank account** verlangt eine Bezeichnung, eine Währung, den Begünstigten, IBAN oder Kontonummer, die Bank, deren SWIFT/BIC und optional eine zwischengeschaltete Bank. Ein Bankkonto wird auf Rechnungen **in seiner eigenen Währung** angeboten. *Zahlungshinweise* gibt es einmal pro Sprache: Die Zahlungsseite zeigt den in der Sprache des Lesers und fällt auf Englisch zurück; lassen Sie eine Sprache leer, um sie wegzulassen.

![Ein neues Bankkonto](../img/bank-new.png)

**Add payment link** (ein *Fiat-Link*, nicht zu verwechseln mit den teilbaren Seiten aus Kapitel 5) ist ein Link zu einem Dienst, der die Zahlung anderswo annimmt: Revolut, N26, PayPal, Wise oder „other". Er braucht eine `https://`-Adresse und die Währungen, die er annehmen kann. Die Zahlungsseite zeigt ihn unter **Online-Zahlung**.

![Ein neuer Fiat-Link](../img/fiat-new.png)

## Zahlungskanäle (Rails)

Ein **Rail account** ist eine Art, in Krypto oder über Lightning bezahlt zu werden. Ein aktives Konto wird auf jeder neuen Rechnung seiner Organisation angeboten (es sei denn, die Rechnung nennt ihre Methoden selbst). Öffnen Sie **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** fragt zuerst nach der Organisation und der Art.

![Die Art wählen](../img/rail-new-choose.png)

| Art | Was sie braucht |
|---|---|
| **evm** | Eine Adresse; die zu beobachtenden Netzwerke (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) und die anzunehmenden Assets (ETH, USDC, USDT). |
| **tron** | Eine Adresse; USDT. Das gesamte Tron-Netz wird beobachtet. |
| **solana** | Eine Adresse; USDC und USDT. |
| **ton** | Vorerst aus: Es lässt sich nur inaktiv speichern, weil sich noch nicht belegen lässt, dass eine TON-Zahlung angekommen ist. |
| **btcpay** | Die `URL` und die `Store ID` von BTCPay, die Assets (BTC, XMR, ZEC) und eine Speed-Policy: `MediumSpeed` (1 Bestätigung, Standard), `LowMediumSpeed` (2) oder `LowSpeed` (6). `HighSpeed` wird abgelehnt. |
| **lightning** | Ein Backend: `nwc` (Nostr Wallet Connect, zum Beispiel Alby Hub), `lnd`, `cln` oder `lnbits`; für die letzten drei eine `URL`. Asset BTC. |

![Ein neues EVM-Konto](../img/rail-new-evm.png)

![Ein neues Lightning-Konto](../img/rail-new-lightning.png)

Gut zu wissen:

- **Verwenden Sie Adressen, die nur dieses System nutzt.** Eine Chain-Zahlung wird ihrer Rechnung über den **Betrag** zugeordnet: Jedes Kursangebot verlangt den Grundbetrag plus einige Einheiten „Staub", die unter den Angeboten, die diese Adresse halten, eindeutig sind. Geld, das aus anderem Grund dorthin geht, landet in Needs a person.
- Die URL eines Kanals muss eine `https://`-Adresse auf einem Host sein, der nur auf öffentliche Adressen auflöst; Loopback oder private Adressen werden abgelehnt (einzige Ausnahme ist ein LND oder CLN auf demselben Rechner, auf `LOCAL_LIGHTNING_PORT`).
- Solange ein Kunde ein Angebot noch bezahlen kann, lassen sich Organisation, Art, Adresse, Netzwerke und Assets des Kontos nicht ändern; legen Sie stattdessen ein neues Konto an. **Deactivate** bietet es nicht mehr an, bereits gezeigte Angebote werden aber weiter beobachtet, bis sie freigegeben sind.
- Die Spalte *Watcher* sagt, ob der Worker diese Chain oder dieses Gateway lesen kann.

![Ein BTCPay-Konto](../img/rail-edit-btcpay.png)

### Zugangsdaten

Die Geheimnisse von BTCPay und Lightning sind mit dem Tresorschlüssel verschlüsselt und werden **nie wieder angezeigt**. Öffnen Sie das Konto und drücken Sie **Set credentials** (der Tresor muss entsperrt sein), oder verwenden Sie in einer Shell `manage.py set_rail_credentials <ID des Rail-Kontos>`. Ein leeres Feld behält den gespeicherten Wert.

Das System **gibt nie Geld aus**, deshalb speichert es nur Zugangsdaten, von denen sich beweisen lässt, dass sie das nicht können:

| Backend | Was einzufügen ist |
|---|---|
| BTCPay | Ein `api_key` mit nur `btcpay.store.canviewinvoices` und `btcpay.store.cancreateinvoice` (BTCPay wird gefragt, was der Schlüssel darf) und das `webhook_secret`. Die Schaltfläche **Register the BTCPay webhook** legt den Webhook in BTCPay an und speichert sein Geheimnis für Sie. |
| Lightning, NWC | Die `nwc_url` einer Verbindung mit `make_invoice` und `lookup_invoice` und **ohne** `pay_*`-Methode. |
| Lightning, LND | Ein `macaroon`, gebacken mit `invoices:read invoices:write info:read` (LNDs eigenes `invoice.macaroon` wird abgelehnt). |
| Lightning, Core Lightning | Ein `rune`, beschränkt auf `invoice` und `listinvoices`. |
| Lightning, LNbits | Der **Invoice/read key** der Wallet, nie der Admin key; setzen Sie das Häkchen, dass Sie das geprüft haben. |

![Zugangsdaten eines BTCPay-Kontos](../img/rail-credentials-btcpay.png)

![Zugangsdaten eines Lightning-Kontos](../img/rail-credentials-lightning.png)

Wird der Host der URL eines Kanals geändert, werden seine gespeicherten Zugangsdaten gelöscht und er wird deaktiviert, damit ein Schlüssel nie an einen anderen Dienst geht. Die README ([Crypto rails](../../../README.md#crypto-rails)) enthält die Einzelheiten je Backend, die Umgebungsvariablen für eigene RPC-Endpunkte und das, was die Watcher nicht sehen.

## API-Tokens und Scopes

**Settings > API tokens**. Ein Token gehört **einer Organisation** und sieht nur deren Daten. Er wird **einmal** angezeigt, bei der Erstellung; geht er verloren, widerrufen Sie ihn und erstellen einen neuen. Widerrufen ist endgültig, die Zeile bleibt.

![Tokens](../img/tokens.png)

| Scope | Erlaubt |
|---|---|
| `read` | Jedes GET. |
| `invoices:write` | Kontakte und deren Verschlüsselungsschlüssel anlegen und ändern; Rechnungen erstellen, senden, stornieren; Links neu ausstellen; Zahlungslinks verwalten. |
| `payments:confirm` | Zahlungen bestätigen, ablehnen, rückgängig machen und zuordnen, Rückerstattungen bestätigen, Erklärungen verwerfen. **Das ändert den Geldzustand.** Eine manuelle Zahlung kann er nicht erfassen: Das kann nur ein Mensch. |
| `admin` | Alles oben Genannte und die Webhook-Endpunkte. |

Geben Sie einem Token die kleinste nötige Menge; eine Integration, die nur Rechnungen erstellt und sendet, braucht `read` und `invoices:write`. Erstellen, Scopes ändern und Widerrufen sind Step-up-Änderungen.

![Ein Token, einmal angezeigt](../img/token-created.png)

Sie können auch in einer Shell einen ausstellen: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooks

**Settings > Webhooks** registriert einen Endpunkt, dem mitgeteilt wird, was mit den Rechnungen und Zahlungen einer Organisation geschieht.

![Webhooks](../img/webhooks.png)

- Die Adresse muss `https://` sein, auf Port 443, auf einem Host, der nur auf öffentliche Adressen auflöst. (Eine Entwicklungsumgebung kann das mit `WEBHOOK_ALLOW_HTTP` und `WEBHOOK_ALLOW_PRIVATE` lockern.)
- Haken Sie die gewünschten Ereignisse an; kein Haken bedeutet alle: `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert` und `ping`.
- Das **Geheimnis** wird einmal angezeigt. Jede Auslieferung ist ein signierter `POST` mit `X-Pay-Signature: sha256=<hex>`, einem HMAC-SHA256 des rohen Bodys mit diesem Geheimnis; prüfen Sie ihn, bevor Sie dem Aufruf vertrauen. Der Body enthält nur IDs und Status, nie Namen, Adressen oder Beträge: Den Rest lesen Sie über die API.
- Eine Auslieferung wird nach 2, 5, 15, 30, 60, 120, 240 und 480 Minuten wiederholt und dann als fehlgeschlagen markiert; die Mitarbeitenden werden einmal informiert, und sie steht unter **Webhooks not delivered** in Needs a person.
- Die Seite eines Endpunkts hat **Send a ping**, die letzten Auslieferungen und **Delete**. Settings > Webhook deliveries listet jede Auslieferung mit **Retry now**.

![Ein Endpunkt und seine Auslieferungen](../img/webhook-detail.png)

Die vollständige Ereignisliste und der Prüfcode stehen in `/llms.txt` Ihrer Installation (Kapitel 9).

## Das Team

**Settings > Team** listet alle auf, die sich anmelden können, mit den Namen ihrer zweiten Faktoren und der letzten Anmeldung. Ein Superuser kann den verlorenen Faktor einer Kollegin oder eines Kollegen entfernen (eine Step-up-Änderung, per E-Mail an jeden Superuser gemeldet); die Person ist dann ausgesperrt, bis jemand auf dem Server eine neue Authenticator-App einrichtet. Ihren eigenen letzten Faktor können Sie nicht entfernen.

![Das Team](../img/team.png)
