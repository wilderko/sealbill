# 3. Kontakte

Ein **Kontakt** ist ein Kunde einer Organisation: Namen, eine oder mehrere E-Mail-Adressen, eine Postanschrift, Steuernummern, eine Telefonnummer, eine bevorzugte Sprache, eine Notiz und die Antwort auf „US tax resident?". Jeder Kontakt gehört zu einer Organisation. Öffnen Sie **Contacts** in der Leiste.

![Die Kontaktliste](../img/contacts.png)

- Suchen Sie nach der **genauen E-Mail-Adresse**; filtern Sie nach Organisation.
- Die Liste zeigt **Clients and link buyers**, **Clients of record** oder **Link buyers**. Ein *Link buyer* ist ein Kontakt, den der Checkout eines Zahlungslinks aus dem erzeugt hat, was ein Besucher eingetippt hat; in ein öffentliches Formular kann jeder alles eintippen, deshalb ist er kein Kunde in der Kundenkartei (client of record), bis ein Mensch ihn befördert (Kapitel 5).
- Ein Kontakt, den Sie zweimal mit demselben Namen sehen, einmal unter jeder Organisation, ist die **Kopie des Kunden beim US-Issuer** (Kapitel 2): Der Issuer führt eine eigene Kopie, damit die Daten der ersten Organisation nie überschrieben werden.

## Einen Kontakt anlegen und ändern

Selten legen Sie einen Kontakt für sich allein an. Wer bei **New invoice** eine E-Mail-Adresse eintippt, findet den Kontakt mit dieser Adresse oder legt aus dem Eingegebenen einen neuen an; was Sie leer lassen, überschreibt nie etwas Gespeichertes. Auch die API und der Importer legen Kontakte an.

**Contact > Edit** ändert Name, Telefon, bevorzugte Sprache, US-Antwort, Steuernummern, Notizen und Adresse. Die Organisation lässt sich nicht ändern. Das Herabsetzen einer gespeicherten US-Antwort (von „yes") wird im Audit-Log festgehalten.

![Einen Kontakt bearbeiten](../img/contact-edit.png)

![Ein Kontakt](../img/contact.png)

Auf der Kontaktseite fügt die Liste **E-mail addresses** eine Adresse hinzu, macht eine zur **Primary** und entfernt Adressen. E-Mails, Zahlungslinks und PDFs gehen an die primäre Adresse, daher ist das Hinzufügen oder Befördern einer Adresse eine [Step-up](01-getting-started.md)-Änderung. Die primäre Adresse, die letzte Adresse und eine Adresse, die ein aktiver Verschlüsselungsschlüssel nennt, lassen sich nicht entfernen.

## Verschlüsselungsschlüssel eines Kontakts

Alles, was über einen Kontakt gespeichert ist, ist bereits im Ruhezustand mit dem Tresorschlüssel verschlüsselt (Kapitel 1). Ein Kontakt kann außerdem einen eigenen Schlüssel haben, damit **die E-Mails, die Sie ihm senden**, an diesen verschlüsselt werden: ein **öffentlicher PGP-Schlüssel** oder ein **S/MIME-Zertifikat**. Öffnen Sie **Contact > Encryption keys**.

![Ein Kontakt ohne Schlüssel](../img/contact-keys-empty.png)

- **Upload durch Mitarbeitende.** Fügen Sie den Schlüssel ein oder wählen Sie eine Datei. Er muss eine der Adressen des Kontakts nennen, darf nicht abgelaufen sein und muss ein öffentlicher Schlüssel oder ein Zertifikat sein (ein privater Schlüssel oder eine `.p12`-Datei wird abgelehnt). Er wird sofort der **aktive** Schlüssel und ersetzt den bisherigen; der Kunde muss nichts bestätigen.
- **Upload durch den Kunden.** Jede Kundenseite hat **Verschlüsselte E-Mails erhalten**. Der Schlüssel, den der Kunde dort hochlädt, ist **pending** (ausstehend): Er wartet, bis der Kunde auf eine an ihn verschlüsselte E-Mail antwortet und damit beweist, dass er die private Hälfte besitzt. Hatte der Kontakt bereits einen aktiven Schlüssel, bekommt der Inhaber dieses Schlüssels eine Benachrichtigung mit einem **Abbrechen**-Link, damit jemand, der das Postfach mitliest, nicht unbemerkt einen eigenen Schlüssel unterschieben kann.

  ![Die Kundenseite zum Hochladen eines Schlüssels](../img/portal-key-upload.png)

- Nur der **aktive** Schlüssel wird verwendet, einer pro Kontakt. Sein Zustand ist `pending`, `active`, `replaced`, `revoked` oder `failed`. **Revoke** zieht ihn zurück; E-Mails gehen dann unverschlüsselt.
- Ein Schlüssel, der **im aktiven Zustand ausfällt** (abgelaufen oder an der Quelle widerrufen), wird `failed`, und E-Mails an diesen Kontakt werden **verweigert statt unverschlüsselt gesendet**. Er steht in Needs a person unter *Encryption keys that stopped working*; laden Sie einen funktionierenden Schlüssel hoch oder widerrufen Sie den ausgefallenen, und wiederholen Sie dann die Nachrichten. Ein Schlüssel, der noch im Zustand pending ausfällt, stoppt nichts. Needs a person listet auch *Schlüssel, die in 30 Tagen ablaufen*.
- Die Kopie eines Kontakts beim Issuer folgt dem Schlüssel des Originals automatisch.

![Ein Kontakt mit aktivem PGP-Schlüssel](../img/contact-keys-active.png)

Dasselbe geht über die API: `POST /api/v1/contacts/{id}/keys` mit `{"key": "<armored key>"}` (Scope `invoices:write`) und `POST /api/v1/keys/{id}/revoke`.
