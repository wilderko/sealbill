# Sealbill operations manual

How to develop, configure, run and deploy Sealbill. The project overview is in the top-level
README.md; inside the code the service is still called `pay`. The paths in the deployment guide
(`/srv/sealbill`, `/etc/sealbill`, the account `payinv`) are this guide's defaults: use your own.

Django service that issues invoices, takes payments and e-mails receipts. Client
data is encrypted at rest with a key that lives only in RAM (the vault).

## Development

```bash
uv sync
uv run manage.py migrate
printf '%s\n' 'a long vault password' | uv run manage.py init_vault     # password via stdin, never argv
printf '%s\n' 'a long vault password' | uv run manage.py unseal_vault   # init does not unseal
uv run manage.py createsuperuser
uv run manage.py create_staff_totp <username>      # prints the otpauth:// URL for the authenticator app
uv run manage.py runserver
uv run manage.py run_worker            # second terminal: outbox, overdue, alerts, cache purge
```

Then sign in at http://localhost:8000/login/ (password, then the authenticator code or a passkey) and
create organizations, bank accounts, payment links, rail accounts, API tokens and webhooks on the staff pages under Settings (http://localhost:8000/settings/). The Django admin is a hidden fallback at http://localhost:8000/django-admin/ (set `DJANGO_ADMIN_PATH`; production refuses a short, guessable or placeholder value; superuser plus second factor only, everyone else gets a 404). API tokens can also be issued from a shell:

```bash
uv run manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write
uv run manage.py revoke_api_token <name> --org <slug>
```

Development settings use SQLite and in-memory caches. `run_worker --once` runs every job once
and exits.

## Tests

```bash
uv sync --group dev                   # tblib among them: a parallel run reports a failure instead of crashing
uv run manage.py test --parallel 8    # or without --parallel
```

The suite runs on SQLite in memory. Tests that assert PostgreSQL behaviour (row locking) are skipped
there; to exercise them, set `DATABASE_URL` and the same settings run on that database (the runner
creates a throwaway `test_<name>` database and drops it afterwards):

```bash
DATABASE_URL='postgres:///payinv?host=/var/run/postgresql' uv run manage.py test
```

Never run the suite on a host that holds real credentials.

## MCP server

`mcp_server/` lets an AI agent work with the invoicing API over the Model Context Protocol. It is not a
Django app: it talks to the public API with a bearer token and uses only `requests` and `mcp`.

`mcp` is an optional dependency group, so the service itself installs and runs without it. Install it
where the MCP server runs with `uv sync --group mcp` (development gets it through the dev group, so
its tests run; a production `uv sync --no-dev` leaves it out).

Tools (exactly these eleven): `list_invoices`, `get_invoice`, `create_invoice` (needs an
`idempotency_key`), `send_invoice`, `list_payments`, `get_payment_methods`, `list_contacts`,
`list_quotes`, `list_payment_links`, `create_payment_link` and `deactivate_payment_link`. It offers nothing that confirms, rejects, reverses, assigns or refunds a payment, voids
an invoice or manages webhooks.

Issue the token with only `read` and `invoices:write`:

```bash
uv run manage.py create_api_token --org <slug> --name agent --scopes read,invoices:write
```

At start the server calls `GET /api/v1/me` and exits with code 2 if the token holds `payments:confirm`
or `admin`, if `PAY_API_URL` or `PAY_API_TOKEN` is missing, if `PAY_API_URL` is not https (plain http only on
`127.0.0.1`, `::1` or `localhost`, so the token never travels in the clear), or if the API cannot be reached.

```bash
PAY_API_URL=https://<your-invoicing-host> PAY_API_TOKEN=<token> uv run --group mcp python -m mcp_server
```

The `mcp` group is optional and never installed by `uv sync --no-dev`, so a production server does not
carry it. The official SDK (`mcp`, published from `modelcontextprotocol/python-sdk` with a PyPI attestation)
depends on `httpx2` and `httpcore2`, Pydantic's fork of httpx (`github.com/pydantic/httpx2`). Those two carry
no PyPI attestation; on 2026-11-11 their 2.16.0 wheels were compared file by file with the repository's
`v2.16.0` tag (identical, no `.pth` or install hook), and `uv.lock` pins their hashes.

Client configuration (`mcpServers` JSON; use your own paths and values):

```json
{
  "mcpServers": {
    "pay-invoicing": {
      "command": "uv",
      "args": ["run", "--group", "mcp", "--directory", "/path/to/sealbill", "python", "-m", "mcp_server"],
      "env": {
        "PAY_API_URL": "https://<your-invoicing-host>",
        "PAY_API_TOKEN": "<read + invoices:write token>"
      }
    }
  }
}
```

`send_invoice` leaves the client's view and pay links out of its answer when the client is e-mailed
(they are capability links, and the agent's transcript is not the place for them); with
`email_client` false they are returned, for the agent to pass on.

Test it with `uv run python -m unittest mcp_server.tests` (without the `mcp` group the server's own
tests are skipped).

## Integrating another system through the API

The full reference is `/llms.txt`; the points a caller most often needs:

- An invoice shows `amount_paid_minor` and `balance_due_minor` (meaningless when `status` is `void`);
  `GET /api/v1/payments?invoice=<id>` lists its payments.
- `POST /invoices/{id}/send` takes `client_letters: false` when the caller writes to the client itself:
  no letter about that invoice (invoice, receipts, new links) is sent from here. It is per invoice, not a
  mute on the contact, and encryption-key e-mails are unaffected. `email_client: false` skips only the
  invoice letter. Without `client_letters: false`, one reminder letter follows an unpaid invoice after
  `INVOICE_REMINDER_DAYS` (default 30; 0 turns it off). It replaces both links, so links you gave the client
  yourself stop working, and it emits `invoice.links_reissued`.
- `return_url` on create adds a Back link to the client's page; its host must be in the organization
  setting `return_url_hosts`. The page never redirects there by itself.
- `method_kinds` (`bank`, `fiat_link`, `rail`) is resolved against the issuing organization instead of
  listing `offered_methods`; errors `methods_conflict` and `no_methods`.
- `GET /contacts?name=` finds contacts by whole words (2-100 characters, accents folded, at most 20,
  30 per minute per token, own organization only).

## Docker Compose

```bash
cp .env.example .env     # then set SECRET_KEY, SITE_URL, ALLOWED_HOSTS, DJANGO_ADMIN_PATH (a long random
                         # path ending in /, the hidden admin; the web service will not start without it), STAFF_NOTIFY_EMAILS and one
                         # password used in BOTH DATABASE_URL and POSTGRES_PASSWORD
docker compose up -d     # the web service runs migrate and createcachetable before gunicorn
curl localhost:8000/healthz                  # {"status":"degraded","vault":"sealed",...} until unsealed
docker compose exec web python manage.py migrate            # already done on start; safe to repeat
docker compose exec web python manage.py createcachetable   # likewise
printf '%s\n' 'the vault password' | docker compose exec -T web python manage.py init_vault
printf '%s\n' 'the vault password' | docker compose exec -T web python manage.py unseal_vault
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py create_staff_totp <username>
docker compose exec web python manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write
docker compose logs -f worker
```

`init_vault` only creates `vault.json`; it does not unseal. Unseal with `unseal_vault` as above
or by opening `/unlock/` in the browser and entering the password. The key volume is
RAM-backed, so every restart of the stack leaves the vault sealed again: open `/unlock/`.

Both services run as the image's user `pay` (uid 10001). The key volume and a second RAM-backed
volume for the throw-away GPG keyrings (`GPG_TMP_DIR=/run/gpg`) are tmpfs mounts owned by that uid,
and `/app/vault` and `/app/media` exist in the image with that owner, so new named volumes take it.

The web port is published on `127.0.0.1:8000` only. Put a reverse proxy (TLS) in front of it
that sets `X-Forwarded-For` (appending the client address) and `X-Forwarded-Proto: https`. The
app trusts the LAST `X-Forwarded-For` entry as the client address (rate limits are keyed on
it), so the proxy must append to the header, never pass a client-supplied one through as the
last entry.

On a host that other accounts share, every one of them can reach `127.0.0.1:8000` too, and forge
that header: set `PROXY_SECRET` in `.env` (generate it with `secrets.token_urlsafe(32)`) and have
the proxy send it on every request as `X-Pay-Proxy-Secret` (Apache: `RequestHeader set
X-Pay-Proxy-Secret "<value>"`). A request without it is refused with 403, `/healthz` included
(`curl -H 'X-Pay-Proxy-Secret: <value>' localhost:8000/healthz`). Production refuses to start while
it is empty. Where no other account can reach the port, say so with `ALLOW_EMPTY_PROXY_SECRET=true`:
the app then starts, `manage.py check --deploy` warns and `manage.py healthcheck` prints
`proxy_secret_empty 1`. The `change-me` placeholder is refused at start for `PROXY_SECRET` and
`SECRET_KEY`.

Static files are collected when the image is built and served by WhiteNoise.

## Configuration

Everything comes from environment variables read in `config/settings/base.py`
(`.env` in development and Compose, a systemd `EnvironmentFile` in production).
The `.env` file is git-ignored; nothing secret belongs in the repository.

## Bank transfers and their QR codes

A bank account (Settings > Organizations > Add bank account) is offered on invoices in its own currency.
The pay page and the PDF invoice show its details and the payment reference (ISO 11649, `RF…`). A person
records the payment when it arrives, because Sealbill does not read the bank.

When the account number is a valid IBAN, the page and the PDF also print QR codes that a banking app reads,
so the client scans instead of typing (`apps/invoices/bank_qr.py`):

| Invoice | Account | Codes |
|---|---|---|
| EUR | IBAN of a SEPA country | PAY by square and payme (Slovakia), QR Platba (Czech Republic), EPC QR / GiroCode (SEPA) |
| CZK | Czech IBAN | QR Platba |
| anything else | | none |

Each code carries the IBAN, the BIC when it is valid (payme has no field for one), the beneficiary, **the
amount still due**, the currency and the payment reference. PAY by square, payme and QR Platba also carry
a variable symbol: the digits of the invoice number, when there are at most ten (`2026-0007` gives
`20260007`). An IBAN outside SEPA gets no code at all, because a Slovak or Czech app would refuse it or
turn it into a SWIFT payment with fees.
PAY by square is written as version 1.1.0 and its encoder is pinned to the bysquare reference library's
golden string (`apps/invoices/tests_bank_qr.py`). There is no setting to switch the codes off.

**payme** is the Slovak Banking Association's newer standard (Payment Link Standard 2.0, payme.sk). The
code is a web address, `https://payme.sk/2/p/PME?IBAN=…&AM=…&CC=EUR&PI=…&MSG=…&CN=…`, which a Slovak
banking app opens as a filled-in transfer. So on a phone, where the client cannot scan their own screen,
the pay page shows **Open in your banking app (payme)** above the codes. A computer with a mouse does not
show the button, and on the PDF the payme code is a link. The link is built the way payme.sk's own
generator builds an invoice: type `/p/` (a SEPA transfer the client can still edit), the variable symbol
as `PI=/VS…/SS/KS` (the reference itself when there is no symbol), the reference as `MSG`, the beneficiary
as `CN`, in the standard's recommended characters (diacritics dropped). Without a beneficiary name, or for
more than 999 999.99 EUR, there is no payme code, because the standard does not allow one.
Sealbill never contacts payme.sk. A phone whose banking app does not take payme links opens the address
in the browser instead, and then payme.sk, run by the Slovak Banking Association, sees the transfer's
details and shows them with a PAY by square code.

## Crypto rails

An invoice can offer crypto next to bank transfers and card links. A client picks a network and
a token on the pay page and gets a **quote**: the exact amount to send at a fixed exchange rate,
valid about thirty minutes. The agent API (`/llms.txt`, `/api/v1/docs`) exposes the same quotes.

### Configuring rails

Add a rail account per organization on the staff pages (Settings > Rail accounts; the hidden Django admin is the fallback). Each is one
way of being paid; an active account is offered on every new invoice of its organization unless
the invoice names its methods itself (`offered_methods`, `rail:<id>`).

| Kind | What it needs |
|---|---|
| `evm` | One address, the networks (Ethereum, Arbitrum, ...) and tokens to accept |
| `tron`, `solana` | One address; every network of the family is watched; the tokens to accept |
| `ton` | Off for now (see below): it can only be saved inactive |
| `btcpay` | `config`: `url`, `store_id`; assets from BTC, XMR, ZEC; secrets `api_key` and `webhook_secret` |
| `lightning` | `config.backend` (`nwc`, `lnd`, `cln`, `lnbits`), `url` where the backend needs one; the backend's secret |

A rail URL is an https address on a host that resolves only to public addresses: it is checked when it
is saved and again by every call, whose connection opens a socket only to an address it has just checked
(the webhook resolver's guard, audit SEC-4). A loopback, private, link-local or reserved address is
refused, so a credential header is never sent to whatever else listens on this machine. The one
exception is an LND or CLN on this machine, reached on `LOCAL_LIGHTNING_PORT` only (plain http allowed
there); unset, there is none. If a BTCPay host resolves to a private address from this machine (an
`/etc/hosts` entry, an internal LXC address), calls to it are refused and its watcher reports
`private_address`: give it a public name.

An NWC connection (Alby Hub) is reached through the Nostr relays its connection string names, and
nostr-sdk opens those websockets itself, outside this guard. So every `relay=` must be `wss://` on a host
that resolves to public addresses when the credential is saved (Alby's `wss://relay.getalby.com/v1` and
`wss://relay2.getalby.com/v1` are); a relay on this machine or a private network is refused. nostr-sdk
resolves the name again when it connects, so this stops the plain case, not a rebinding name.

### Credentials

BTCPay and Lightning secrets are encrypted with the vault key and are never shown again. Set them on
the Credentials page of the rail account (Settings > Rail accounts), or, with the vault unsealed, from a
shell. The hidden Django admin neither shows nor edits them. Changing the host of a rail account's URL
clears its stored credentials and deactivates it, so a key is never sent to a different service:

```bash
uv run manage.py set_rail_credentials <rail account id>       # prompts for each, without echo
printf '%s\n' "$API_KEY" "$WEBHOOK_SECRET" | uv run manage.py set_rail_credentials <id>  # or one per line
```

It asks for `api_key` and `webhook_secret` on a BTCPay account, and for the backend's secret on a
Lightning account (`nwc_url`, `macaroon`, `rune` or `invoice_key`). An empty answer keeps the value
already stored. A secret is never taken from the command line, printed or logged, and each change
is audited (which fields, never their values).

The system never spends, so it stores only a credential that provably cannot (the command says
what is too broad and stores nothing):

| Backend | What to paste |
|---|---|
| NWC | a connection with `make_invoice` and `lookup_invoice` and no `pay*` method (below) |
| LND | the hex of a macaroon baked with `lncli bakemacaroon invoices:read invoices:write info:read`. Its operations are read from the macaroon itself; LND's own `invoice.macaroon` is refused, as it also grants `address:write` and `onchain:read` |
| Core Lightning | a rune limited to the methods `invoice` and `listinvoices` (`waitinvoice` and `getinfo` are also allowed): `lightning-cli createrune restrictions='[["method=invoice","method=listinvoices"]]'` |
| LNbits | the wallet's **Invoice/read key**, never the Admin key, with `--lnbits-key-is-invoice-key`: the two cannot be told apart without asking LNbits, so the command takes your word for it and the audit entry records that you gave it |
| BTCPay | a key whose permissions are only those listed below; BTCPay is asked (`GET /api/v1/api-keys/current`) when the key is set, and again whenever the webhook secret alone is replaced |

**Use new addresses that only this system uses.** A chain payment is matched to its invoice by
its amount: each quote asks for the base amount plus a few units of dust, unique among the
quotes that hold that address. Money sent to the same address for any other reason arrives as
an unmatched transfer and lands in Needs a person. One wallet may serve several organizations;
amounts are kept unique per address across them, and a person can assign an unmatched transfer
to any organization that has an active account on that address.

### Lightning with a receive-only NWC connection

Create the Nostr Wallet Connect connection in your wallet with **only** the permissions to make
invoices and look them up (`make_invoice`, `lookup_invoice`). The service never sends a payment,
and a connection that cannot spend cannot lose money if the vault is ever read. Paste the
`nostr+walletconnect://` string to `set_rail_credentials` as `nwc_url`. It asks the wallet what the
connection allows and stores nothing if any advertised method's name contains `pay` (`pay_invoice`,
`pay_keysend`, `pay_offer` and any later `pay_*`), if `make_invoice` or `lookup_invoice` is missing,
or if the wallet cannot be asked.

### BTCPay (Bitcoin, Monero, Zcash)

1. Create an API key for the store with only `btcpay.store.canviewinvoices` and
   `btcpay.store.cancreateinvoice` (`btcpay.store.webhooks.canmodifywebhooks` and
   `btcpay.store.canviewstoresettings` are also allowed, nothing else). Enter it as `api_key`
   with `set_rail_credentials`, which asks BTCPay what the key may do and refuses anything broader.
2. In the store, add a webhook to `https://<your host>/hooks/btcpay/<rail account id>/` with a
   secret, and enter the same secret as `webhook_secret`. The webhook only makes settlement
   quicker: the worker also polls BTCPay.
3. `config.speed_policy` is `MediumSpeed` (the default: 1 confirmation), `LowMediumSpeed` (2) or
   `LowSpeed` (6). `HighSpeed` settles with no confirmation at all and is refused. A confirmed
   payment is read again for 72 hours: if BTCPay then calls its invoice `Invalid` (a double spend,
   a reorg), staff are told and it is listed in Needs a person. Nothing is undone by code: a person
   reverses the payment from its invoice, or keeps it.

### TON is off for now

`TON_ENABLED` (default `false`) keeps TON out until the credit leg can be verified (project 2).
toncenter's jetton transfer, its hash and its `transaction_aborted` flag describe the sender's
debit, not the credit to our own jetton wallet, so a TON payment cannot yet be shown to have
arrived: a fake jetton wallet naming the USDT master could even succeed its debit while ours
bounces. While it is off, a TON account cannot be saved active, the watcher does not scan TON, no
quote is made on it, a declared TON hash is not looked up, and the pay page does not offer it.

### Network access and environment variables

Chains are read through public endpoints by default. Set your own where the free tiers are too
slow (see `.env.example`; keys are optional and must never be committed). Production refuses to start
with any of these on plain http, except on `127.0.0.1`, `::1` or `localhost`, where a node of your own
may answer:

| Variable | Meaning |
|---|---|
| `EVM_RPC_URLS` | JSON: network name to a list of RPC URLs, replacing the defaults (three operators a network). A transfer is final only once endpoints of two different operators (counted by domain) each hold it, as recorded, in a block they call final: one endpoint alone could invent a transfer. Configure at least two operators a network; with one, that one decides alone, as before |
| `TRONGRID_API_URL`, `TRONGRID_API_KEY` | Tron watcher |
| `SOLANA_RPC_URLS` | Comma separated, tried in order |
| `TONCENTER_API_URL`, `TONCENTER_API_KEY` | TON watcher |
| `FX_FRANKFURTER_URL` | Frankfurter (ECB): the EUR to other-fiat leg, multiplied into both prices. It has no second source; Bitstamp and Coinbase are the two cross-checked sources, and a divergence over 100 bps refuses the quote. A Frankfurter answer dated more than 5 days ago is refused (Thursday's rate on Easter Tuesday morning is 5 days old and still used), and so is one more than 10 % from the last rate accepted for that currency, or another rate for a day already accepted (the ECB never changes a published rate, so a source inside the bound cannot walk the rate away in a few calls; an answer older than the accepted day is not used, the accepted rate is). Crypto quotes in that currency are then refused: staff are told, and Needs a person lists it under "Exchange rates refused" with what to do. A stale rate is waited out; a jump that is real (check the ECB's rate) is accepted there, with a note, and audited |
| `TON_ENABLED` | `true` turns TON on; leave it `false` until the credit leg is verified (above) |
| `RAILS_HTTP_ENABLED` | `false` stops every outbound rails call: chains, exchange rates, BTCPay, Lightning and NWC (tests set it). It is not a switch for everything: webhook deliveries have `WEBHOOK_HTTP_ENABLED`, and e-mail goes out through `EMAIL_BACKEND`. To cut the app off the network in an incident, stop `pay-worker` (it makes the scheduled calls) and seal the vault (`seal_vault`) |
| `LOCAL_LIGHTNING_PORT` | The one port an LND or CLN on this machine is reached on (loopback, plain http allowed there). Default unset: no rail URL may lead to this machine at all. BTCPay, LNbits and any other rail URL must resolve to public addresses only (audit SEC-4) |
| `MIGRATION_NOTE_LANGUAGE` | Language of the note stored on the payment of an invoice imported from Zaprite or Request Finance: `en` (default) or `sk`. Notes already stored are not changed |
| `INVOICE_REMINDER_DAYS` | Days after sending, default 30; `0` turns the reminder off. The worker's `invoice_reminders` job runs once at worker start and then every 24 hours (with the vault sealed at start, the run does nothing and the next attempt is up to 24 hours after the unlock). It queues one letter, ever, per unpaid invoice that this system mails itself, replacing the invoice's links, and logs one line `invoice reminders: N queued` at INFO when N is not zero. The first run after a deploy reminds every qualifying invoice whatever its age. It skips an invoice while a quote is open or a payment is detected or in review, for a US tax resident on a non-US issuer (staff's "wrong issuer" queue) and while the contact's encryption key has failed; a later run asks again |
| `WEBHOOK_HTTP_ENABLED`, `WEBHOOK_DNS_ENABLED` | `false` stops every outbound webhook delivery, or every lookup of a webhook host (tests set both). They touch webhooks only: a rail URL's host is looked up through the rails' own gate (`RAILS_DNS_ENABLED`, closed by the test settings alone and never read from the environment), so pausing webhook lookups never stops a BTCPay or Lightning call |
| `WEBHOOK_ALLOW_HTTP`, `WEBHOOK_ALLOW_PRIVATE` | `true` lets a webhook go to plain http, or to a loopback or private address: a development receiver only (`config.settings.dev` sets both). Without `WEBHOOK_ALLOW_PRIVATE` a destination must also use port 443 (80 for plain http) |
| `WEBHOOK_DENY_ADDRESSES` | Comma separated IPs a webhook is never sent to, refused like a private address: this host's own public addresses, so a webhook cannot reach Apache resources restricted with `Require local` (`/server-status`, `/server-info`: a connection from the host to its own address counts as local, audit SEC-5). The production plan is `203.0.113.10`, with `WEBHOOK_ALLOW_URL_PREFIXES` naming shop.example.org' receiver, since every one of the host's hostnames resolves to that address; list the host's public IPv6 addresses too, if it has any. Default empty. Single addresses only: an entry that is not one (a range, a name, a typo) is a system check error (`webhooks.E001`), so `check --deploy` and pay-web's start refuse it |
| `WEBHOOK_ALLOW_URL_PREFIXES` | Comma separated https URL prefixes, each ending in `/`, delivered to although their host resolves to a denied address: a receiver that lives on this host's own address. Production: `https://shop.example.com/payments/webhook/invoicing/`. Matched on the normalised endpoint URL (host in any case, port 443 written or not) when the endpoint is created and before every delivery, and by the connection itself; a URL with a `.` or `..` segment, plain or percent-encoded, never matches, and neither do the host's other paths, a subdomain or the address. It lifts the deny list only, never the private-address rule. An entry that is not such a prefix is a system check error (`webhooks.E002`). `WEBHOOK_ALLOW_HOSTS`, which allowed every path of a host, is gone: setting it stops the start (`webhooks.E003`). Default empty |

### What runs while the vault is sealed

**Wrong passwords at `/unlock/`** are limited to 5 per 5 minutes per address and to 20 per 15 minutes from
every address together; an unseal starts both over. Whoever fills the second can keep the page from unlocking,
never the server: `manage.py unseal_vault` has no limit, and the refusal says so. The tenth wrong password of an
episode alerts staff.

**Sealing on purpose (an incident):** `manage.py seal_vault` (`payinv-manage seal_vault` on a systemd host)
removes the key file, and every process holding the key, the web workers and the worker included, drops it
on its next use. A restart of `pay-web` does not seal: the runtime directory with the key file is kept.
Unseal again with `unseal_vault` or at `/unlock/`.

While sealed, the pay page (`/p/`), payment links (`/l/`) and the API (`/api/`) answer 503, so **no client can get a quote,
chain rails included**, and `/` redirects to `/unlock/`. The worker's chain watchers, payment
matching, finality refresh, quote expiry and release, and the retry of queued transfers read and
write plaintext columns only, so **they keep running after a restart**: a transfer made while the
vault is sealed is still seen and matched (but see what the watchers do not see, below).
Staff alerts (a failing watcher, the sealed vault) are sent directly, even while sealed.
Webhook events (invoice and payment changes, staff alerts) are recorded while sealed too, in
plaintext and with ids and statuses only; they are delivered once the vault is unlocked, because
endpoint URLs and secrets are encrypted.
**What waits for an unlock**: the pay page and the API, BTCPay and Lightning polling (their
credentials are encrypted), and client e-mail (queued, then sent).

### What the watchers do not see

A chain watcher sees allow-listed token transfers to our address, and native ETH sent by a
transaction whose own recipient is our address. **Native ETH sent by a contract** (an internal transfer, as some
exchanges and smart-contract wallets send it) is not seen at all, neither by the scan nor through
the hash a client declares: that money arrives unrecorded. Ask clients to pay in a token, or to
send ETH from an ordinary wallet; staff record a payment that came in that way by hand, on the
invoice page. A
declared hash that is not a transaction hash at all (a typo) is never sent to a node.

### Needs a person

`/` lists what no program should decide: payments that matched no invoice or matched
badly (with the reason), BTCPay and
Lightning payments the gateway saw more than six hours ago and has not settled, refunds due
for overpayments, transfers still short of finality, queued transfers that could not be
recorded, and watchers that cannot see their chain. A person assigns a payment to an invoice or
acknowledges a refund there; the API does the same with `payments:confirm`
(`POST /api/v1/payments/{id}/assign`, `.../acknowledge-refund`). A payment that is not yet on an invoice
needs an `admin` token, and is shown only to the organizations that may be credited with it. A payment
waiting for a person (`needs_staff_confirmation`) is confirmed, assigned or rejected only by an active staff user
(since 2026-11-09 no gateway sets it: Monero and Zcash that BTCPay calls Settled are confirmed like
bitcoin, and only payments recorded before then still carry it):
`confirm_payment`, `assign_payment` and `reject_payment` refuse anyone else, so every API token gets 409 `needs_person`. A manual
payment ("staff saw the money arrive", on the invoice page) is recorded only by a person too: no chain or gateway
stands behind it, so `record_manual_payment` refuses every token the same way.

## Payment links

A payment link (staff: **Payment links**, `/links/`) is one page, `/l/<token>/`, for a service at a
set price (or an amount the buyer chooses, within optional bounds), paid by many people. Each checkout
makes an **order**: an invoice or a payment request with one line (title x quantity, the coupon's
percentage off before tax, the link's tax), sent at once with a number and a frozen snapshot, and the
buyer goes straight on to its ordinary pay page. From there it is the usual flow: quotes, matching,
receipt, webhooks, CSV.

- **No e-mail before payment.** The form mails nobody; the pay page says to keep it. The first letter
  is the receipt (or the partial-payment letter), and it carries the order's links: a link order keeps
  its tokens encrypted beside their hashes until its receipt is out. Letters go to the address typed at
  checkout, in the checkout's language.
- **A public form never shows or changes a contact.** An address that matches a client of record books the
  order to it; the order's buyer block is what was typed. Otherwise the checkout makes a contact of its own
  from the typed data, a link buyer, which is not a client of record (`Contact.of_record`): anyone can type
  anyone's name and e-mail into a public form, and pay any price under it. No e-mail or name lookup finds it
  (the API, the staff invoice form, the US-resident issuer's copies), nothing reuses it, another checkout
  included, and the API cannot read it by id. Paying does not change that. The staff contact list shows link
  buyers, marked and with a filter, and a person makes one a client of record from its page once they have
  checked it (refused while a client of record holds its address; audited). The API's contacts carry
  `has_paid_invoice`, which counts only invoices staff or the API issued, never link orders.
- **US tax residents.** The question is always asked, and the order goes where the answer sends it: a
  typed "yes" to the US-resident organization, a typed "no" to the link's own. What a contact stores
  never routes it, so a public link reveals nothing stored. A typed answer the stored flag contradicts
  is an item on Needs a person; for a "no" against a stored "yes" the item says to check with the buyer
  before fulfilling (and, if they are a US tax resident, to refund and invoice them again from the
  US-resident organization), and the order is not offered to fulfil until a person resolves it.
- **Uses.** A link's total uses and a coupon's remaining uses count orders, held 60 minutes at checkout
  and turned into a use when paid. A payment after its hold lapsed keeps its price; if that goes over a
  cap, staff get one item.
- **A custom method set** is stored as kinds of payment (USDC on Arbitrum, a bank transfer in EUR, ...),
  so the US-resident organization offers its own accounts of the same kinds. A set that leaves nothing to
  offer refuses the checkout and raises one item.
- **Unpaid orders are voided after 7 days** by the worker (audited as the system), unless a payment may
  be under way. Money that arrives later goes to review, as for any void invoice.
- **Limits**: 5 checkout attempts per 10 minutes per address, 50 completed checkouts per hour per link (a refused form counts toward the address only). The image is re-encoded
  (PNG, JPEG or WebP, at most 1.5 MB, metadata stripped), kept encrypted, and shown only inside the page.
  No page may frame a link (`frame-ancestors 'none'`).

Paid orders of a link with fulfilment on are listed "to fulfil" until someone marks them fulfilled.

## Production notes

- **Vault key**: `VAULT_KEY_PATH` must point at tmpfs (for example `/run/...`). The key is never
  in the environment or on persistent disk. After a reboot or a hard restart the vault is
  sealed: open `/unlock/` and enter the password. A sealed vault queues e-mail, and the worker
  sends staff one alert per sealed episode (`STAFF_NOTIFY_EMAILS`).
- **Caches**: rate-limit counters and the sealed-episode flag are kept in two database tables
  (`pay_cache`, `pay_ratelimit`), never on the key's tmpfs. Run `manage.py createcachetable`
  after `migrate` on every deploy (it is idempotent; the Compose web service does both on
  start). The worker deletes expired entries every hour.
- **E-mail**: a message that cannot be sent is retried for about sixteen hours (2, 5, 15, 30
  minutes, then 1, 2, 4 and 8 hours). After that it is `failed`: staff get one alert e-mail,
  and it is listed under Needs a person (`/`) with a retry button, and a dismiss button
  (a reason is required and audited) for a message that must never go out: a dismissed message
  is cancelled for good and no retry sends it. The health check counts failed messages only.
- **Webhooks**: an `admin` token registers endpoints (`POST /api/v1/webhooks`; see `/llms.txt`
  for the events and how to verify `X-Pay-Signature`). A delivery is retried on the same
  schedule as e-mail, then `failed`: staff get one alert e-mail per batch, and it is listed under
  Needs a person with a retry button and a dismiss button (with a reason, audited). Only https to public addresses; redirects are never followed.
- **Reverse proxy**: as above, it must set `X-Forwarded-For` and `X-Forwarded-Proto`; the app
  reads the LAST `X-Forwarded-For` entry. With `PROXY_SECRET` set, every request must also carry it
  in `X-Pay-Proxy-Secret` (403 otherwise, `/healthz` included; the cron check runs `manage.py
  healthcheck`). Set it wherever another account could reach the app without the proxy.
- **Restarts**: a graceful reload (`systemctl reload`, SIGHUP to gunicorn) keeps the key in the
  file and the vault unsealed. A hard restart of the machine or a tmpfs wipe seals it.
- **Static files**: served by WhiteNoise from `STATIC_ROOT` (compressed, hashed names). Run
  `manage.py collectstatic --noinput` on every deploy (`pay-web.service` and the Dockerfile do).
- **Logging**: production settings log to stdout (`apps` at INFO, request and security errors);
  capability tokens are cut out of logged paths.
- **Units**: `deploy/systemd/` (see the next section).
- **Health**: `GET /healthz` returns the status (`ok`, `degraded`, or `error` with 503 when the
  database cannot be read), the vault state, the worker heartbeat's age and three counts: watchers
  failing for at least ten minutes (as long as their alert waits), failed e-mails and failed webhook
  deliveries. No names, no client data. `manage.py healthcheck` checks the same, plus the rates
  (cron mails a non-zero exit). Point the monitor at either. A BTCPay or Lightning rail with no
  quote to poll is still asked every five minutes whether it answers (one `get_info` over NWC,
  otherwise a read of its newest invoice), so a node that comes back clears its watcher, and one
  that stops answering (Alby Hub locked after an update or a reboot) shows up even with no
  payment open (`apps/rails/gateways/idle_probe.py`).
- **Backups**: `deploy/backup/backup-db.sh` writes one AES-256 encrypted archive,
  `pay-DATE.tar.enc`, holding the `pg_dump` **and the vault config** (`vault.json`: the salt
  and the sentinel, never the key) into `$BACKUP_DIR` (mode 0600, 14 days kept), with
  `pay-DATE.tar.enc.hmac` beside it: the HMAC-SHA256 of the archive under a key derived from the
  same passphrase (`archive-mac.py`). AES-CBC alone cannot tell a changed archive, so the restore
  checks it before it decrypts anything. Run it from
  cron with `BACKUP_DIR`, `BACKUP_PASSPHRASE_FILE`, `VAULT_CONFIG_PATH` and `DATABASE_URL` set
  (the same values as the app's; without `DATABASE_URL` the script falls back to the `PG*`
  variables and refuses to run if `PGDATABASE` is unset). Restore:

  ```bash
  deploy/backup/restore-db.sh pay-DATE.tar.enc            # database + vault.json
  deploy/backup/restore-db.sh --force pay-DATE.tar.enc    # also replace a different vault.json
  deploy/backup/restore-db.sh --unverified pay-DATE.tar.enc  # an archive made before 2026-11-11 (no .hmac)
  ```

  An archive that does not match its `.hmac` (changed, or another passphrase) is refused before
  anything is decrypted or restored; one without a `.hmac` is refused unless `--unverified`.

  `restore-db.sh` needs `BACKUP_PASSPHRASE_FILE`, `VAULT_CONFIG_PATH`, `VAULT_KEY_PATH` and
  `DATABASE_URL` (or `PG*`). It refuses, before touching the database, to overwrite an existing
  `vault.json` that differs from the archive's unless `--force` is given. Both scripts split the URL
  into `PG*` variables and, when it carries a password, a private 0600 passfile, so the password never
  appears in `ps`. The Unix-socket form (`postgres:///payinv?host=/var/run/postgresql`, peer authentication)
  needs no password at all. Do not use
  `pg_restore --dbname="$DATABASE_URL"` by hand: that puts the password in argv. Both scripts work
  from `/`, so they can be started from a directory `payinv` cannot read.

  **Stop the app before a restore.** `restore-db.sh` refuses while `pay-web.socket`, `pay-web` or
  `pay-worker` is active (`systemctl is-active`) and never stops or starts them itself: a process
  still holding the old vault key would fail every restored letter for good, and encrypt what it
  writes with a key the restored `vault.json` cannot derive. After installing `vault.json` it removes
  the old key file (`$VAULT_KEY_PATH`), and it ends with what comes next:
  start the units, then unseal with the backup's password.

  ```bash
  systemctl stop pay-web.socket pay-web pay-worker                 # as root
  sudo -u payinv bash -c 'set -a; . /etc/sealbill/env; set +a;
    BACKUP_PASSPHRASE_FILE=/etc/sealbill/backup-passphrase \
    /srv/sealbill/app/deploy/backup/restore-db.sh /srv/sealbill/backups/pay-DATE.tar.enc'
  systemctl start pay-web.socket pay-web pay-worker
  read -rsp "The backup's vault password: " p; echo
  printf '%s\n' "$p" | payinv-manage unseal_vault; unset p
  ```

  (If `pay-web` stops at a migration asking for the vault, start it again once it is unsealed.) Where
  these units do not exist (Docker Compose), stop the app yourself, `docker compose stop web worker`,
  and run the script with `PAY_RESTORE_SKIP_UNIT_CHECK=1`: without it the script refuses there, both
  without `systemctl` and where none of the three units is installed (systemd answers "inactive" for a
  unit that does not exist, so that alone proves nothing). It also refuses a `VAULT_KEY_PATH` that names
  `vault.json` itself. A process that runs anyway does not write under the wrong key: every use of the
  key checks the inode and modification time of `vault.json`, and when another installation's
  `vault.json` is in place and the key no longer opens it, that process seals itself.

  **The database without `vault.json` and the vault password is unreadable**: every encrypted
  field needs the key, and the key is derived from the password and the salt in `vault.json`.
  Keep an off-server copy of `vault.json` (it changes only when the vault is initialised), the
  backup passphrase and the vault password, each somewhere other than the server.

## Deploying with systemd and Apache

Layout: gunicorn on the Unix socket `/run/pay-invoicing-web/gunicorn.sock` behind the host's Apache
(TLS terminated there), PostgreSQL on the host, no Docker. The Docker files stay for portability.
Production settings refuse to start without `SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASE_URL`, `SITE_URL`
and `VAULT_KEY_PATH`. Replace `${PAY_HOST}` below with the real host name.

**Why a socket and not a loopback port.** `127.0.0.1` is shared by every account on the host. Any of
them could connect to a gunicorn port directly, bypassing Apache, and send its own `X-Forwarded-For`
and `X-Forwarded-Proto`. The app keys its rate limits (staff login, vault unlock, the pay page) on the
LAST `X-Forwarded-For` entry, so a forged header would defeat them. systemd creates the socket
(`deploy/systemd/pay-web.socket`): owner `payinv`, group `www-data` (Apache's), mode 0660. Connecting
to a Unix socket needs write permission on it, so no other account can. systemd hands the listening
socket to gunicorn when `pay-web.service` starts (`LISTEN_FDS`); gunicorn uses it as it is and never
binds, chowns or removes it. A socket gunicorn bound itself would take its process's group, `payinv`,
and Apache would be refused. Group `www-data` is not always Apache alone: where PHP (`php-fpm`) runs
as `www-data` too, it can reach the socket, so Apache also sends a secret the app checks (step 4).

The secret does not close everything. A socket reachable by `www-data` is reachable by every PHP site
on the host for as long as Apache and PHP-FPM share that uid. A PHP script can connect to it directly,
skip Apache, and hold every gunicorn worker with half-sent requests: the secret is checked only once the
headers have arrived. The portal, the staff pages, `/unlock/`, the API and the webhooks then answer late
or not at all. The fix is on the host: run Apache under a user of its own (`APACHE_RUN_USER` and
`APACHE_RUN_GROUP` in `/etc/apache2/envvars`), or the PHP-FPM pools under per-site users (`user =` and
`group =` in each pool), and then set `SocketGroup=` in `pay-web.socket` to Apache's group alone.
Another gunicorn worker class (gthread, gevent) would only raise the number of connections it takes to
stall it, without closing the hole, so the units keep the default.

0. **System packages** (as root): the ones the Docker image installs. WeasyPrint renders the PDFs
   with Pango and HarfBuzz; without `fonts-noto-cjk` Korean prints as empty boxes. `gnupg` and
   `openssl` encrypt e-mail (PGP, S/MIME) and the backups.

   ```bash
   apt-get update
   apt-get install --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
     fonts-noto-core fonts-noto-cjk gnupg openssl
   ```

   Apache, PostgreSQL and certbot come from the distribution too (`apache2 postgresql certbot`).
1. **User and directories** (as root):

   ```bash
   useradd --system --create-home --home-dir /srv/sealbill --shell /usr/sbin/nologin payinv
   install -d -o payinv -g payinv -m 0750 /srv/sealbill/app
   install -d -o payinv -g payinv -m 0700 /srv/sealbill/media /srv/sealbill/vault /srv/sealbill/backups
   install -d -o root -g payinv -m 0750 /etc/sealbill
   install -d -m 0755 /var/www/letsencrypt                      # ACME webroot (step 8)
   ```

2. **Database** (as the PostgreSQL superuser). Peer authentication over the Unix socket: the system
   account `payinv` is the role `payinv`, and no password exists anywhere.

   ```sql
   CREATE ROLE payinv LOGIN;
   CREATE DATABASE payinv OWNER payinv;
   ```

   `DATABASE_URL=postgres:///payinv?host=/var/run/postgresql`. Over TCP instead, give the role a
   password and use `postgres://payinv:<password>@127.0.0.1:5432/payinv`; the backup scripts read
   both forms (a password goes to a private passfile, never to a command line). On Debian and Ubuntu
   the server runs as `postgresql@18-main.service`; the units order themselves after
   `postgresql.service`, the umbrella unit that starts after every instance.

3. **Code**: copy the repository to `/srv/sealbill/app` and, as `payinv` (home
   `/srv/sealbill`, set by `useradd` above), run
   `UV_PYTHON_DOWNLOADS=never uv venv --python /usr/bin/python3 && uv sync --frozen --no-dev --compile-bytecode`
   there (any system Python 3.12 or newer; Ubuntu 26.04 ships 3.14, and the suite passes on it). The
   units run with a read-only file system, so the bytecode is compiled now: the service could never
   write `__pycache__`. They also run with `ProtectHome=yes`, so the venv's interpreter must not live
   under `/home`: use the system Python as shown, or `UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync ...`
   (readable by `payinv`).
4. **Environment**: `cp deploy/env.example /etc/sealbill/env`, fill it in, then
   `chown root:payinv /etc/sealbill/env && chmod 0640 /etc/sealbill/env`. Generate
   `SECRET_KEY` with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.

   **Proxy secret.** Where PHP (`php-fpm`) runs as `www-data`, as WordPress does on a shared host, it
   can connect to the socket and send its own `X-Forwarded-For`. So Apache sends a secret with every
   request, and the app refuses (403) any request without it. Generate the value once
   (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`) and write it into two files
   `www-data` cannot read:

   ```bash
   umask 077
   editor /etc/apache2/pay-invoicing-proxy-secret.conf   # one line: RequestHeader set X-Pay-Proxy-Secret "<value>"
   editor /etc/sealbill/env                         # PROXY_SECRET=<value>
   chown root:root /etc/apache2/pay-invoicing-proxy-secret.conf && chmod 0600 /etc/apache2/pay-invoicing-proxy-secret.conf
   chown root:payinv /etc/sealbill/env && chmod 0640 /etc/sealbill/env
   ```

   Apache reads its configuration as root, before it starts its `www-data` children, so the file can
   be 0600 root. `RequestHeader set` replaces any `X-Pay-Proxy-Secret` a client sends. Keep `mod_info`
   disabled: its `/server-info` page prints the configuration, this line included. To change the
   value later, edit both files, then `systemctl restart pay-web && systemctl reload apache2`.
5. **Backup passphrase**:

   ```bash
   (umask 077; openssl rand -base64 48 > /etc/sealbill/backup-passphrase)
   chown root:payinv /etc/sealbill/backup-passphrase && chmod 0640 /etc/sealbill/backup-passphrase
   ```

   The nightly backup runs as `payinv` (peer authentication), so `payinv` may read the file and only
   root may change it. **Keep a copy off the host**, with the vault password and `vault.json`: without
   the passphrase no backup can be opened.
6. **Units**: copy `deploy/systemd/pay-web.socket`, `pay-web.service` and `pay-worker.service` to
   `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now pay-web.socket pay-web pay-worker`.
   The socket unit creates `/run/pay-invoicing-web/gunicorn.sock` (the directory root-owned, 0755).
   The web unit runs `migrate`, `createcachetable` and `collectstatic`, then starts gunicorn on that
   socket. The socket stays while `pay-web` restarts, so a request that arrives meanwhile waits (up to
   Apache's 60-second `ProxyTimeout`) instead of failing. A request also starts a stopped `pay-web`:
   to stop it for good, `systemctl stop pay-web.socket pay-web`.
   Both units share the runtime directory `/run/pay-invoicing` (tmpfs, 0700, preserved across
   restarts of either unit). It holds the vault key (`vault.key`), the throw-away GPG keyrings
   (`gpg/`, which the application creates on first use with mode 0700) and fontconfig's cache
   (`XDG_CACHE_HOME`, the home directory being read-only).
7. **Vault, first time only** (as `payinv`, with the units' environment). `deploy/payinv-manage`,
   installed as root to `/usr/local/sbin/payinv-manage`, runs `manage.py` exactly that way and passes
   stdin through:

   ```bash
   read -rsp 'Vault password: ' p; echo
   printf '%s\n' "$p" | payinv-manage init_vault && printf '%s\n' "$p" | payinv-manage unseal_vault
   unset p
   payinv-manage createsuperuser
   payinv-manage create_staff_totp <username>
   ```

   `printf` is a shell builtin, so the password never reaches a command line. `init_vault` refuses a
   password under 16 characters: every encrypted column hangs on it and `vault.json` is in every
   backup, so use a generated passphrase (`python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`).
   `init_vault` only writes `vault.json`; `unseal_vault` (or `https://${PAY_HOST}/unlock/` once Apache
   is up) opens it.
   `systemctl reload pay-web` (SIGHUP) keeps the vault open; a reboot or a tmpfs wipe seals it,
   and `/unlock/` opens it again.
8. **Apache**, the `:80` vhost first (certbot needs it), then the `:443` one:
   1. Copy `deploy/apache/pay-invoicing.conf.example` to `/etc/apache2/sites-available/pay-invoicing.conf`,
      replace `${PAY_HOST}`, enable the modules (`a2enmod proxy proxy_http headers rewrite ssl`), then
      `a2ensite pay-invoicing && apachectl configtest && systemctl reload apache2`. It serves
      `/.well-known/acme-challenge/` from `/var/www/letsencrypt` and redirects everything else to https.
   2. Obtain the certificate through that webroot:
      `certbot certonly --webroot -w /var/www/letsencrypt -d ${PAY_HOST} --deploy-hook "systemctl reload apache2"`.
      certbot stores the webroot and the hook for renewals: they go through the same `:80` path, and the
      reload puts the renewed certificate in use (the webroot method does not reload Apache by itself).
   3. Copy `deploy/apache/pay-invoicing-ssl.conf.example` to
      `/etc/apache2/sites-available/pay-invoicing-ssl.conf`, replace `${PAY_HOST}`, then
      `a2ensite pay-invoicing-ssl && apachectl configtest && systemctl reload apache2`. It proxies to
      the socket (never `/.well-known/acme-challenge/`), sets `X-Forwarded-Proto`, sends the proxy
      secret (step 4; `configtest` fails while that file is missing) and limits request bodies (2 MiB;
      128 KiB for key uploads). It includes `/etc/letsencrypt/options-ssl-apache.conf` if it exists;
      only certbot's Apache plugin writes that file, `certonly --webroot` does not. Without it, add to
      the vhost `SSLProtocol -all +TLSv1.2 +TLSv1.3`, `SSLHonorCipherOrder off` and
      `SSLCipherSuite ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305`.

   Logging: on `:443` every access log line carries the method, the path **without the query string**
   and the protocol (`"%m %U %H"`), and never the `Referer`: query strings carry client data
   (`/api/v1/contacts?email=...`, the staff form's address). Requests under the capability paths
   (`/i/`, `/p/`, `/k/`, `/l/`), where the path itself is the token, are logged with no URL at all. They are
   recognised by `<LocationMatch>`, on the same decoded, slash-merged path that `%U` would log, so
   `//i/...` and `/%69/...` count too. The `:80` vhost logs no URL at all (it serves only the ACME files
   and a redirect), in a log of its own, so the distribution's `other-vhosts-access-log` never writes
   the request line (disabling that conf with `a2disconf other-vhosts-access-log` is an optional
   host-level extra). `LogLevel warn proxy:crit proxy_http:crit` keeps mod_proxy errors, which print
   the request path, out of the error log. Gunicorn's own access log is off and Django logs to journald
   (`journalctl -u pay-web`), so a failing gunicorn stays visible there.

   **mod_status.** `/server-status` keeps each worker slot's last request line after the request has
   ended, and with extended status prints it for idle slots too, with the client's address. On a shared
   host every local account (`curl http://127.0.0.1/server-status`) would read this site's `/i/`, `/p/`,
   `/k/` and `/l/` tokens and the query strings of API calls. Extended status is on by default whenever
   `mod_status` is loaded: set `ExtendedStatus Off` in the server-wide configuration (a file in
   `conf-available/` enabled with `a2enconf`), or disable the module with `a2dismod status`, then
   `apachectl configtest && systemctl reload apache2`. A `<Location /server-status>` rule alone is not
   enough: a tenant whose `.htaccess` may use `SetHandler server-status` (`AllowOverride FileInfo`)
   serves the page from their own site. Keep `mod_info` disabled too (step 4); `apachectl -M` lists
   what is loaded.
9. **Cron**: `deploy/cron/pay-invoicing.cron.example` runs the nightly backup and
   `manage.py healthcheck` every 10 minutes (mail on a non-zero exit).
10. **Check**: `curl -s https://${PAY_HOST}/healthz` answers `{"status":"ok","vault":"unsealed",...}`.

Deploying an update: replace the code, `uv sync --frozen --no-dev --compile-bytecode` as `payinv`,
then `systemctl restart pay-web` (the migrations run first; a restart of the web unit does not seal the
vault because the runtime directory is preserved) and `systemctl restart pay-worker`.
Static files are served by WhiteNoise, so Apache needs no `Alias` for them. The units run with a
read-only file system, so `STATIC_ROOT` is `/run/pay-invoicing/static` (rebuilt by `collectstatic` at
every start of `pay-web`) and `MEDIA_ROOT` is `/srv/sealbill/media`.

Staff TOTP secrets are stored encrypted with the vault key (`StaffTOTPDevice`), so `create_staff_totp`
needs the vault unsealed. Migration `staff.0002` moved the devices django-otp kept in plaintext,
secrets unchanged, so authenticator apps keep working. A migration that has secrets to encrypt and finds
the vault sealed stops with "unseal the vault first, then migrate" and changes nothing; `pay-web` then
does not start (its `migrate` runs first), so `/unlock/` is not there to use: unseal with
`payinv-manage unseal_vault` and start it again.

Passkeys (WebAuthn) are the other second factor. Staff add them on `/security/` after a full
sign-in (password and a second factor from the last ten minutes), name them and remove them there;
both are audited, and the last second factor of an account cannot be removed. The relying party is
the host of `SITE_URL` and the only accepted origin is `SITE_URL`'s own, so `SITE_URL` must be the
exact address staff open (`https://invoices.example.org`, no path): a passkey registered
under one host does not work under another. User verification (PIN or biometrics) is required. A
lost passkey is replaced by signing in with the authenticator app, or on the server with
`payinv-manage create_staff_totp <username> --replace`, which replaces the TOTP devices and leaves the
passkeys alone. A superuser can also remove another person's authenticator or passkey on Settings > Team.

**Powers that need a superuser and a fresh second factor** (audit SEC-3). Creating, rescoping or
revoking an API token; creating, changing or (de)activating a rail account, setting its credentials or
registering its BTCPay webhook; an organization's US-resident issuer flag and its return hosts; creating,
changing or (de)activating a bank account or a payment link; creating or deleting a webhook endpoint;
adding a contact's e-mail address or making one primary; and removing another person's second factor. Each needs a superuser who gave a second factor
within the last ten minutes, the same rule as the Security page. Anyone else gets a 403 and nothing
changes; a superuser whose second factor is older is sent to verify again and comes back to the page. The
rest of the staff pages (invoices, payments, contacts' other fields, an organization's other fields,
a webhook's ping and retry, links) stay open to every staff account.

The hidden Django admin keeps the same rule. It is open to superusers only, and any change there (any request
but GET, HEAD or OPTIONS; signing out excepted) needs a second factor from the last ten minutes, or the
request goes to the verify page and nothing is saved. A change in the admin to an API token's scopes or
revocation, a rail account, an organization's US-resident flag, settings or bank and payment-link inlines, a
bank account, a payment link, a contact's e-mail addresses, or another person's authenticator or passkey
(deleted, or an authenticator unconfirmed) is told to every superuser in the same letter, linking the staff
audit log rather than the admin.

Every such change e-mails every active superuser (their account's e-mail address; `STAFF_NOTIFY_EMAILS`
when none has one) through the outbox: who, what, when, and a link to the page. It names things by
label and id only, never a secret, an IBAN, a URL a credential belongs to or a client's e-mail address.
Give every superuser an e-mail address (`payinv-manage shell`, or the Django admin), or these alerts go
only to `STAFF_NOTIFY_EMAILS`.
