# Sealbill

**Privacy aware self-hosted crypto-friendly invoice system.**

Sealbill issues invoices and payment requests, offers your clients the ways you accept money,
watches for the payment itself and sends the receipt. It runs on your own server. No payment
processor holds the money, and client data is encrypted in the database with a key that exists
only in RAM after a person has unlocked it.

It is a Django 5 application with server-rendered pages (HTMX, no front-end build step), a JSON API
for other systems, an optional MCP server for AI agents, and tools for leaving Zaprite and Request
Finance. Inside the code it is still called `pay` (the units are `pay-web` and `pay-worker`, the
service account `payinv`).

## What it does

- **Invoices and payment requests** from several organizations in one installation, each with its
  own numbering, bank accounts, payment methods, logo and default language. An organization can be
  marked as the issuer for US tax residents, and invoices for such clients go out from it.
- **A client page for every invoice**, reached by a link that is its own credential: the invoice, a
  PDF, a pay page with a quote and a QR code, and the status turning to "paid". Client pages, PDFs
  and letters are in nine languages: English, Slovak, Czech, German, Spanish, French, Polish,
  Korean and Russian. Staff pages are in English.
- **Payment links** (`/l/<token>/`): one page for a service at a set price, or an amount the buyer
  chooses, paid by many people. Each checkout becomes an order with its own invoice or payment
  request. Coupons, use limits and a "to fulfil" list are included.
- **Quotes**: for crypto, the client gets an exact amount at a fixed exchange rate, valid for 30
  minutes. Each amount is unique among the open quotes on that address, which is how an incoming
  transfer is matched to its invoice without asking the client for anything.
- **Needs a person**: one page lists everything no program should decide, such as a payment that
  matched badly, an overpayment to refund, a transfer still short of finality, a watcher that cannot
  see its chain, a failed e-mail or webhook, or an exchange rate that was refused.
- **Letters**: invoice, receipt, partial payment and new links, encrypted to the client's own key
  when they have one (see below), retried for about sixteen hours, then listed for a person.
- **CSV exports** of invoices and payments for the books, with an accounting currency beside each
  invoice's own, converted at the day's closing rate (Bitstamp for crypto, the ECB through
  Frankfurter for fiat).
- **An API** (`/api/v1/`, OpenAPI at `/api/v1/docs`, a plain-text guide for agents at `/llms.txt`)
  with webhooks, and an **MCP server** (`mcp_server/`) that can create and send invoices but can
  never confirm, assign, reverse or refund a payment.

## Payment methods

Each organization configures its own; an invoice offers all of them or a chosen set.

| Kind | What the client gets |
|---|---|
| Bank transfer | The organization's account for the invoice's currency: beneficiary, account number or IBAN, bank, SWIFT, an intermediary bank if any. Stored encrypted. For EUR to a SEPA IBAN the pay page and the PDF also print PAY by square, QR Platba and EPC QR codes (QR Platba alone for CZK to a Czech IBAN), carrying the amount still due and the reference. Staff record the payment when it arrives. |
| Fiat payment links | A link to Revolut, N26, PayPal, Wise or any other payment page, with instructions, per currency. |
| Stablecoins and ETH on EVM chains | Ethereum (ETH, USDT, USDC), Arbitrum One (ETH, USDC, USDT), Optimism (ETH, USDC, USDT), Base (ETH, USDC), Polygon PoS (USDC, USDT), BNB Smart Chain (USDT, USDC as Binance-Peg tokens), Avalanche C-Chain (USDC, USDT). One receiving address per rail account. |
| Tron | USDT (TRC-20). |
| Solana | USDC and USDT. |
| TON | USDT. Off by default (`TON_ENABLED=false`): the arrival of a TON payment cannot be verified yet, so leave it off. |
| BTCPay Server | Bitcoin, Monero and Zcash through your own BTCPay store. |
| Lightning | Through Nostr Wallet Connect, LND, Core Lightning or LNbits. |

Token contracts are an allow-list in `apps/rails/networks.py`, each checked against the issuer's own
page (the BNB Smart Chain tokens and USDT on Polygon are marked there as unverified, because the
issuers do not publish them). Bridged look-alikes are never accepted.

Sealbill never holds a key that can spend. Chain rails are watch-only: Sealbill reads public RPC or
indexer endpoints (your own if you set them) for transfers to your address. For BTCPay and Lightning
it stores only a credential that provably cannot spend, and refuses one that can: an NWC connection
with any `pay*` method, an LND macaroon with more than invoice permissions, a Core Lightning rune
beyond `invoice` and `listinvoices`, a BTCPay key with more than the invoice permissions. LNbits is
the exception: an invoice key and an admin key look the same, so you confirm that you pasted the
invoice key and the audit trail records that you did.

Invoices can be in EUR, USD, CZK, GBP, CHF, PLN, HUF, GEL, KGS, KRW, JPY, RUB, UAH, SEK, NOK, DKK,
CAD, AUD, PYG, BRL, MXN, AED or BTC. A few more crypto currencies exist only for invoices carried
over from Request Finance (`MIGRATED_ONLY` in `apps/invoices/currencies.py`).

## Security

What is below is what the code does; the operations manual explains how to run it that way.

- **A sealed vault.** Every client field (names, addresses, e-mail addresses, notes, bank details,
  stored keys, gateway credentials, audit details) is encrypted with Fernet. The key is derived from
  a vault password of at least 16 characters with PBKDF2-HMAC-SHA256 (600,000 iterations) and is
  kept in RAM and in a file on tmpfs, never in the environment or on disk. After a reboot the vault
  is sealed until a person unlocks it at `/unlock/`. While it is sealed, client pages, payment links
  and the API answer 503, and the chain watchers keep recording transfers using plaintext columns
  only. E-mail addresses are looked up through a keyed blind index (HMAC-SHA256), never stored in
  plaintext.
- **Staff sign-in with a second factor.** A password and then either a TOTP code (its secret is
  encrypted with the vault key) or a passkey (WebAuthn, user verification required, bound to the
  host of `SITE_URL`). Sign-in, second-factor and unlock attempts are rate limited per address,
  account and device, each counted in one database statement so that parallel attempts cannot share
  a count. The last second factor of an account cannot be removed.
- **Per-contact encryption keys.** A client can give an OpenPGP public key or an S/MIME certificate,
  uploaded through a link of their own and confirmed by answering a letter encrypted to it. From
  then on every letter to that client is encrypted to it (PGP/MIME or S/MIME), attachments included.
- **Capability links.** The links to an invoice, its pay page, a key upload and a payment link are
  random 256-bit tokens, looked up by their SHA-256 hashes. Most are stored as the hash alone. Two are
  also kept encrypted under the vault key: a payment link's token, so staff can show its URL again, and
  a payment-link order's invoice links, until its receipt is out. They are cut out of logged paths
  (tracebacks included), and the Apache example logs no URL at all under those paths.
- **A strict Content Security Policy** with a fresh nonce per response: scripts and styles from the
  site only, no inline event handlers or inline styles, `frame-ancestors 'none'`.
- **Signed webhooks, both ways.** Outgoing events are signed with HMAC-SHA256 over the exact body
  (`X-Pay-Signature`), go only to https on port 443 at public addresses, never follow a redirect and
  are retried with backoff. Incoming BTCPay webhooks must carry a valid `BTCPay-Sig`, and the worker
  polls BTCPay as well, so a webhook only makes settlement quicker.
- **API tokens with scopes and a throttle.** Tokens are stored as SHA-256 hashes, belong to one
  organization and carry scopes (`read`, `invoices:write`, `payments:confirm`, `admin`). Each token
  may make 120 requests a minute. Some acts are refused to every token and left to a person
  (HTTP 409 `needs_person`): confirming or assigning a payment that waits for a person, and
  recording a manual payment.
- **An audit trail.** Who did what to which record and when is stored in plaintext; the detail is
  encrypted. Actions the system takes on its own while the vault is sealed record plain facts only:
  ids, amounts, states and hashes.
- **Exchange rates from two sources.** Crypto prices come from Bitstamp and Coinbase, and a
  divergence of more than 1 % refuses the quote. The EUR to other-fiat leg comes from the ECB
  through Frankfurter, so it is held to rules of its own: a rate more than 5 days old, or more than
  10 % away from the last accepted one, is refused until a person accepts it.
- **A hidden admin.** The Django admin lives at a path you choose (`DJANGO_ADMIN_PATH`, which
  production requires and refuses when short, guessable or the placeholder). It is open only to a
  superuser who has passed a second factor; everyone else gets 404.
- **Behind a proxy.** The app trusts the last `X-Forwarded-For` entry and requires a shared secret
  header from the reverse proxy (`PROXY_SECRET`): production refuses to start without one unless
  `ALLOW_EMPTY_PROXY_SECRET=true` says no other account can reach the app. The systemd units run with a read-only file system, `NoNewPrivileges` and
  `ProtectHome`, and serve gunicorn on a Unix socket only Apache's group can reach.
- **Encrypted backups.** `deploy/backup/backup-db.sh` writes one AES-256 encrypted archive holding
  the database dump and `vault.json` (salt and sentinel, never the key), with an HMAC-SHA256 beside it
  that `restore-db.sh` checks before it decrypts anything.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- PostgreSQL in production. Development and the test suite use SQLite.
- The system libraries WeasyPrint needs for PDFs (Pango, HarfBuzz), fonts (Noto, including CJK for
  Korean), and `gnupg` and `openssl` for encrypted e-mail and backups. On Debian or Ubuntu:
  `apt-get install --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-noto-core fonts-noto-cjk gnupg openssl`.
- A tmpfs for the vault key in production (`/run/...`), an SMTP server for letters, and a reverse
  proxy with TLS.
- Node.js 20 or newer only to run the tests of the Request Finance export script; the tests skip
  without it.

## Quick start (development)

```bash
uv sync
uv run manage.py migrate
read -rsp 'Vault password (16+ characters): ' p; echo
printf '%s\n' "$p" | uv run manage.py init_vault      # the password goes on stdin, never in argv
printf '%s\n' "$p" | uv run manage.py unseal_vault    # init_vault does not unseal
unset p
uv run manage.py createsuperuser
uv run manage.py create_staff_totp <username>         # prints the otpauth:// URL for your authenticator
uv run manage.py runserver
uv run manage.py run_worker                           # in a second terminal: e-mail, webhooks, watchers
```

Sign in at http://localhost:8000/login/ and create organizations, bank accounts, payment links, rail
accounts, API tokens and webhooks under Settings. Development settings need no `.env` file: they use
SQLite, in-memory caches, the console e-mail backend, a vault key in `/tmp`, and the admin at
`/django-admin/`.

Run the test suite (SQLite in memory, no network, no `.env` needed):

```bash
uv run manage.py test --parallel 8
```

Never run it on a host that holds real credentials. To run the PostgreSQL-only tests too, set
`DATABASE_URL` to a database the test runner may create a throwaway `test_<name>` copy of.

For Docker Compose, production settings, the crypto rails, payment links, backups and the
step-by-step deployment with systemd and Apache, read the operations manual,
[docs/operations.md](docs/operations.md). Where the code, the deploy files or a message say
"README", they mean that manual.

## User manual

Step by step, with screenshots, in nine languages:
[English](docs/manual/en/README.md) ·
[Slovenčina](docs/manual/sk/README.md) ·
[Čeština](docs/manual/cs/README.md) ·
[Deutsch](docs/manual/de/README.md) ·
[Español](docs/manual/es/README.md) ·
[Français](docs/manual/fr/README.md) ·
[Polski](docs/manual/pl/README.md) ·
[한국어](docs/manual/ko/README.md) ·
[Русский](docs/manual/ru/README.md)

## Migrating from Zaprite or Request Finance

`tools/migrate/` holds two exporters and `manage.py import_invoices` reads what they write:

- `tools/migrate/zaprite_export.py` reads every contact and order through Zaprite's API.
- `tools/migrate/request_finance_export.js` runs in your browser on app.request.finance while you
  are logged in, and downloads every sent invoice.

Every carried-over invoice is imported as paid, numbered, with its original buyer and lines, and
locked: nothing can be sent, paid, voided or changed on it. The import is idempotent. Start with
[docs/migration.md](docs/migration.md).

## Licence

Sealbill is free software under the GNU Affero General Public License, version 3 only. See
[LICENSE](LICENSE). If you run a modified Sealbill as a network service, the AGPL requires you to
offer its users the source of your version.

SPDX-License-Identifier: AGPL-3.0-only

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
