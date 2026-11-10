# 3. Contacts

A **contact** is a client of an organization: names, one or more e-mail addresses, a postal address, tax ids, a phone
number, a preferred language, a note, and the answer to "US tax resident?". Contacts belong to one organization each.
Open **Contacts** in the rail.

![The contact list](../img/contacts.png)

- Search by the **exact e-mail address**; filter by organization.
- The list shows **Clients and link buyers**, **Clients of record** or **Link buyers**. A *link buyer* is a contact a
  payment-link checkout made from what a visitor typed; anyone can type anything into a public form, so it is not a
  client of record until a person promotes it (chapter 5).
- A contact you see twice with the same name, one under each organization, is the **US-resident issuer's copy** of a
  client (chapter 2): the issuer keeps its own copy so that the first organization's data is never rewritten.

## Creating and changing a contact

You rarely create one by itself. Typing an e-mail address on **New invoice** finds the contact with that address or
makes a new one from what you typed; what you leave blank never overwrites what is on file. The API and the importer
make contacts too.

**Contact > Edit** changes the name, phone, preferred language, US answer, tax ids, notes and address. The organization
cannot change. Lowering a stored US answer (from yes) is recorded in the audit log.

![Editing a contact](../img/contact-edit.png)

![A contact](../img/contact.png)

On the contact page the **E-mail addresses** list adds an address, makes one **Primary** and removes one. Letters, pay
links and PDFs go to the primary address, so adding or promoting an address is a [step-up](01-getting-started.md)
change. The primary address, the last address, and an address that an active encryption key names cannot be removed.

## Encryption keys of a contact

Everything stored about a contact is already encrypted at rest with the vault key (chapter 1). A contact can also have
a key of their own, so that **the e-mail you send them** is encrypted to it: a **PGP public key** or an **S/MIME
certificate**. Open **Contact > Encryption keys**.

![A contact with no key](../img/contact-keys-empty.png)

- **Staff upload.** Paste the key or choose a file. It must name one of the contact's addresses, must not be expired
  and must be a public key or a certificate (a private key or a `.p12` file is refused). It becomes the **active** key at
  once and replaces the current one; the client is not asked to confirm.
- **Client upload.** Every client page has **Receive encrypted e-mail**. The key the client uploads there is
  **pending**: it waits until the client answers a letter that was encrypted to it, which proves they hold the private
  half. If the contact already had an active key, the holder of that key is sent a notice with a **cancel** link, so that
  someone who reads the mailbox cannot swap in their own key unseen.

  ![The client's page for uploading a key](../img/portal-key-upload.png)

- Only the **active** key is used, one per contact. Its state is `pending`, `active`, `replaced`, `revoked` or
  `failed`. **Revoke** withdraws it; mail then goes unencrypted.
- A key that **stops working while active** (it expired, or was revoked upstream) is marked `failed`, and e-mail to that
  contact is **refused rather than sent unencrypted**. It is listed in Needs a person under *Encryption keys that
  stopped working*; upload a working key or revoke the failed one, then retry the messages. A key that fails while still
  pending stops nothing. Needs a person also lists *keys expiring within 30 days*.
- The issuer's copy of a contact follows the original's key automatically.

![A contact with an active PGP key](../img/contact-keys-active.png)

The same can be done through the API: `POST /api/v1/contacts/{id}/keys` with `{"key": "<armored key>"}` (scope
`invoices:write`) and `POST /api/v1/keys/{id}/revoke`.
