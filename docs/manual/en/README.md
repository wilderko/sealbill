# Sealbill user manual

Sealbill is a self-hosted invoicing system for people who take crypto, bank transfers and card links side by side,
and who want the client data encrypted at rest. This manual is for the people who run it: the staff who issue
invoices and the operator who installs it.

Languages: **English** · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) ·
[Español](../es/README.md) · [Français](../fr/README.md) · [Polski](../pl/README.md) · [한국어](../ko/README.md) ·
[Русский](../ru/README.md)

## Chapters

1. [Getting started](01-getting-started.md): install, the vault, signing in, the step-up for sensitive changes
2. [Settings](02-settings.md): organizations, bank accounts, payment rails, API tokens, webhooks, the team
3. [Contacts](03-contacts.md), including per-contact encryption keys
4. [Invoices](04-invoices.md): create, send, the client pages, quotes, partial and over-payments, void, reissue, reminder
5. [Payment links](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md)
7. [Migrating from Zaprite and Request Finance](07-migration.md)
8. [Exports and the accounting currency](08-exports.md)
9. [The API in short](09-api.md)

## How to read the screenshots

- The **staff pages** (everything behind the sign-in) are in English whatever language the client speaks. Menu names in
  this manual are written exactly as the app shows them.
- The **client pages** (the invoice page `/i/…`, the pay page `/p/…`, payment-link pages, the unlock page and the
  letters) are translated into en, sk, cs, de, es, fr, pl, ko and ru. The client sees the language of the contact, then of
  the organization, then of their browser; add `?lang=de` to a client link to see it in another language.
- All names, e-mail addresses, wallets and amounts in the screenshots are made up: they come from a demo
  organization, "Example Studio s.r.o.", on a throw-away database.
- A line in `code` is something you type or a value you copy. Commands are shown as `manage.py …`; in the Docker
  Compose setup that is `docker compose exec web python manage.py …`, and under systemd it is `payinv-manage …`
  (see the README of the repository).
