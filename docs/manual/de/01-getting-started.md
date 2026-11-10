# 1. Erste Schritte

## Was Sie betreiben

Sealbill besteht aus einer Webanwendung und einem Hintergrund-Worker.

- Die **Webanwendung** liefert die Mitarbeiterseiten (an der Wurzel der Website, zum Beispiel `https://pay.example.org/`), die Kundenseiten (`/i/…` zeigt eine Rechnung, `/p/…` ist der Ort, an dem bezahlt wird, `/l/…` ist ein Zahlungslink), die API (`/api/v1/…`) und die Entsperrseite (`/unlock/`).
- Der **Worker** (`manage.py run_worker`) erledigt alles, was ohne Klick geschehen muss: Er sendet wartende E-Mails, markiert Rechnungen als überfällig, beobachtet Blockchains und Gateways auf Zahlungen, lässt Kursangebote ablaufen, sendet die eine Zahlungserinnerung und liefert Webhooks aus. **Ohne Worker wird nichts per E-Mail verschickt und keine Krypto-Zahlung bemerkt.** Starten Sie ihn als zweiten Prozess neben der Webanwendung; `manage.py run_worker --once` führt jede Aufgabe einmal aus und beendet sich, was für einen Test praktisch ist.

## Installation und erster Start

Die README des Repositorys enthält die Befehle für jede Betriebsart, sie werden hier nicht wiederholt:

- auf Ihrem eigenen Rechner, zum Ausprobieren: [README, Development](../../../README.md#development)
- mit Docker Compose: [README, Docker Compose](../../../README.md#docker-compose)
- auf einem Server mit systemd, Apache und PostgreSQL: [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Welchen Weg Sie auch wählen, die Reihenfolge des ersten Starts ist dieselbe:

1. Datenbanktabellen anlegen (`migrate`, und `createcachetable` für die Tabellen der Ratenbegrenzung).
2. Den Tresor anlegen und entsperren (nächster Abschnitt).
3. Den ersten Benutzer mit `manage.py createsuperuser` anlegen.
4. Diesem Benutzer einen zweiten Faktor geben: `manage.py create_staff_totp <username>` gibt eine `otpauth://`-Adresse für eine Authenticator-App aus. Der Tresor muss dafür entsperrt sein. Einen Passkey können Sie später auf der Seite Security hinzufügen.
5. Webanwendung und Worker starten, die Seite öffnen und anmelden.
6. Unter **Settings** eine Organisation und ein Bankkonto oder Zahlungskanal-Konto anlegen (Kapitel 2) und sich selbst eine erste Rechnung schicken (Kapitel 4).

## Der Tresor

Alles, was ein Mensch eingegeben hat (Namen, Adressen, Steuernummern, Rechnungspositionen, Notizen, Zugangsdaten Ihrer Zahlungs-Gateways), wird verschlüsselt gespeichert. Der Schlüssel wird aus einem **Tresor-Passwort** abgeleitet und nur in der Datei im Arbeitsspeicher gehalten, die `VAULT_KEY_PATH` nennt; er steht nie in der Umgebung oder auf der Festplatte.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # einmalig; mindestens 16 Zeichen
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # nach jedem Neustart des Rechners
```

Geben Sie das Passwort wie oben auf der Standardeingabe ein, damit es nie in einer Befehlszeile oder in der Shell-Historie erscheint. `init_vault` legt nur `vault.json` an (Salz und einen Prüfwert, nicht den Schlüssel); `unseal_vault` öffnet ihn.

Nach einem Neustart des Rechners oder wenn die Datei im Arbeitsspeicher gelöscht wird, ist der Tresor **versiegelt**. Ein sanftes Neuladen des Webdienstes lässt ihn offen; ein harter Neustart nicht. Solange er versiegelt ist:

- leitet jede Mitarbeiterseite auf **/unlock/** um: Geben Sie dort das Tresor-Passwort ein oder führen Sie `unseal_vault` aus;
- antworten die Zahlungsseiten, Zahlungslinks und die API mit „vorübergehend nicht verfügbar" (503), sodass kein Kunde ein Kursangebot bekommt;
- sieht und gleicht der Worker Blockchain-Zahlungen weiterhin ab (er liest nur unverschlüsselte Spalten), E-Mails und Webhooks warten aber, und das Abfragen der Gateways (BTCPay, Lightning) pausiert; die Betreiberin oder der Betreiber bekommt pro versiegelter Phase eine Alarm-E-Mail.

![Die Entsperrseite, die bei versiegeltem Tresor erscheint](../img/vault-unlock.png)

`GET /healthz` meldet `"vault": "sealed"` oder `"unsealed"` ganz ohne Daten, ein Monitoring kann es also beobachten. Bewahren Sie eine Kopie von `vault.json`, die Backup-Passphrase und das Tresor-Passwort an einem anderen Ort als dem Server auf: Eine Datenbank ohne alle drei lässt sich nicht lesen. Die vollständige Liste dessen, was bei versiegeltem Tresor weiterläuft, steht in der [README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Anmeldung

Mitarbeitende melden sich unter `/login/` in zwei Schritten an: dem **Passwort** und dann einem **zweiten Faktor**, nämlich einem Passkey oder einem sechsstelligen Code aus einer Authenticator-App. Eine Sitzung dauert acht Stunden und endet, wenn der Browser geschlossen wird. Die Anmeldung ist pro Adresse, pro Konto und pro Gerät ratenbegrenzt.

![Der Passwort-Schritt](../img/start-login.png)

![Der Schritt mit dem zweiten Faktor](../img/start-verify.png)

Hat das Konto einen Passkey, bietet der zweite Schritt zuerst **Use a passkey** und darunter das Codefeld („or enter a code"). Ein Passkey gehört nur zu dieser Website, eine Fälschung kann ihn also nicht verwenden.

![Der zweite Schritt eines Kontos mit Passkey](../img/verify-passkey.png)

Nach der Anmeldung landen Sie bei **Needs a person** (Kapitel 6). Die Leiste links ist die gesamte Navigation: Needs a person, Invoices, New invoice, Contacts, Payment links, Exports, Settings; das Schild unten ist **Security**, der Pfeil meldet Sie ab.

![Needs a person, die erste Seite nach der Anmeldung](../img/needs-person.png)

### Security: Ihre eigenen Passkeys und Authenticator-Apps

**Security** (das Schild-Symbol) listet Ihre zweiten Faktoren auf. Einen Passkey fügen Sie hinzu, indem Sie ihm einen Namen geben (zum Beispiel „MacBook Touch ID" oder „YubiKey 5C") und **Add a passkey** drücken; der Browser fragt dann nach Fingerabdruck, Gesicht oder PIN. Eine Authenticator-App wird auf dem Server mit `create_staff_totp` hinzugefügt, nie im Browser. Der letzte zweite Faktor eines Kontos lässt sich nicht entfernen, und wer den Faktor entfernt, mit dem die Sitzung angemeldet wurde, muss zuerst einen anderen bestätigen. Jedes Hinzufügen und Entfernen steht im Audit-Log.

![Security mit hinzugefügtem Passkey](../img/security-passkey.png)

## Mitarbeitende und Superuser

Jedes Konto mit Mitarbeiterstatus kann Rechnungen ausstellen, Zahlungen erfassen und die Warteschlangen abarbeiten. Manche Änderungen können Geld bewegen oder einen Kunden umleiten, sie verlangen mehr.

### Die erneute Bestätigung (Step-up)

Diese Änderungen verlangen einen **Superuser**, der **innerhalb der letzten zehn Minuten** einen zweiten Faktor angegeben hat:

- einen API-Token erstellen, seine Scopes ändern oder ihn widerrufen;
- ein Zahlungskanal-Konto und seine Zugangsdaten (und den BTCPay-Webhook, der ein Geheimnis speichert);
- das Kennzeichen „US-resident issuer" einer Organisation und ihre Rücksprung-Hosts;
- ein Bankkonto oder ein Fiat-Zahlungslink einer Organisation;
- einen Webhook-Endpunkt erstellen oder löschen (wohin die Ereignisse einer Organisation gehen);
- eine E-Mail-Adresse zu einem Kontakt hinzufügen oder zur primären machen (E-Mails, Zahlungslinks und PDFs gehen dorthin);
- den zweiten Faktor einer anderen Person entfernen.

Die Seiten sagen, wo Sie stehen, bevor Sie etwas drücken:

- Wer kein Superuser ist, sieht „Only a superuser may …", und nichts ändert sich;

  ![Ein Mitarbeiter, der kein Superuser ist](../img/stepup-not-superuser.png)

- ein Superuser, dessen letzter zweiter Faktor älter als zehn Minuten ist, sieht **Verify again**; das führt zur Bestätigungsseite und bringt Sie zur Seite zurück, auf der Sie waren.

  ![Eine Änderung, die einen frischen zweiten Faktor braucht](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Jede Step-up-Änderung sendet außerdem eine E-Mail an jeden aktiven Superuser: wer, was und wann, mit Namen und IDs, nie mit einem Geheimnis, einer Kontonummer oder einer Adresse. Ändert eine gestohlene Sitzung etwas, erfährt es die Inhaberin oder der Inhaber des Systems.

Die Seite **Team** (Settings > Team) listet alle auf, die sich anmelden können, mit ihren zweiten Faktoren und der letzten Anmeldung, und lässt einen Superuser den verlorenen Faktor einer Kollegin oder eines Kollegen entfernen. Neue Mitarbeitende legen Sie mit `createsuperuser` oder im versteckten Django-Admin an (der Pfad steht in `DJANGO_ADMIN_PATH`; nur Superuser mit zweitem Faktor, alle anderen bekommen ein 404).
