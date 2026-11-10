# 2. Settings

**Settings** is the last item of the rail. Its index lists everything below, plus the audit log, the e-mail outbox and
the webhook deliveries (chapter 6 uses those).

![The Settings index](../img/settings-index.png)

Every change here is written to the **Audit log** (Settings > Audit log): who, what and which record, and never the
values typed.

![The audit log](../img/audit.png)

Open an entry to read its details. The changes listed under "The step-up" in chapter 1 need a superuser with a fresh second factor.

## Organizations

An **organization** is a legal entity that issues invoices: its name, address, tax ids, logo, the invoice numbering and
the payment accounts that belong to it. A second organization is useful when two companies share one installation, for
example one in the EU and one in the US.

![The list of organizations](../img/orgs-list.png)

**Settings > Organizations > New organization**, or open one to edit it:

| Field | What it does |
|---|---|
| Slug | The short name used in the API and the commands (`example-studio`). |
| Legal name, Address, Tax ids | Printed in the "From" block of the invoice. Tax ids are free text. |
| Email from | The sender of the organization's letters, for example `Example Studio <billing@example.com>`. |
| Default language | The language of the client pages and letters when the contact has no preferred language. |
| Footer note, Logo | Printed on the invoice and the PDF. The logo is a PNG or JPEG up to 1.9 MB, stored as a PNG without metadata. |
| Number format | Python-style, with `{year}` and `{seq}`: `{year}-{seq:04d}` gives `2026-0001`. It must contain `{seq}`. |
| Next number | Set by issuing invoices; it cannot be edited, or numbers would be reissued. |
| Underpayment allowance | A crypto payment short by at most the **smaller** of this share (basis points; 50 is 0.5 %) and this flat amount in US dollars (1.00 by default) still settles the quote. It absorbs an exchange's withdrawal fee. |
| Overpayment ceiling | A crypto payment above the quoted amount by more than this share (1000 is 10 %) is held for a person instead of settled. |
| Accounting currency | The currency of the accounting columns in the exports (chapter 8). |
| Hosts a Back button may lead to | One host per line. An invoice created through the API may carry a `return_url` on exactly these hosts. |

![An organization](../img/org-edit.png)

### The US-resident issuer

Tick **US-resident issuer** on at most one organization. It is the organization that must issue the invoices of
clients who are US tax residents.

- Every contact has a **US tax resident** answer: yes, no or unknown. The invoice form makes you choose; a payment
  link asks the buyer.
- When an invoice is requested by another organization for a contact who is a US tax resident, the invoice is **issued by
  the issuer organization**, with its numbering, its bank accounts and its rails. The issuer keeps its own copy of the
  contact; your contact is never rewritten.
- If a contact becomes a US tax resident after an invoice was issued by another organization, the invoice is listed
  under **US tax residents with invoices from another issuer** in Needs a person: void it and create it again.
- With no organization flagged, such an invoice cannot be created at all.

Changing the flag or the return hosts is a step-up change. The flag is in the audit log and e-mailed to the
superusers.

## Bank accounts and fiat links

At the bottom of an organization there are two lists. Both are **deactivated, never deleted**: an issued invoice keeps
showing the account it named.

![Bank accounts and payment links of an organization](../img/org-accounts.png)

**Add bank account** takes a label, a currency, the beneficiary, the IBAN or account number, the bank, its SWIFT/BIC and
an optional intermediary bank. A bank account is offered on invoices in **its own currency**. With an IBAN, the pay page
and the PDF invoice also show bank QR codes: in EUR for the IBAN of a SEPA country, in CZK for a Czech account (chapter
4). *Payment instructions* exist once per language: the pay page shows the one in the reader's language and falls back
to English; leave a language empty to leave it out.

![A new bank account](../img/bank-new.png)

**Add payment link** (a *fiat link*, not to be confused with the shareable pages of chapter 5) is a link to a service
that takes the payment elsewhere: Revolut, N26, PayPal, Wise or "other". It needs an `https://` address and the
currencies it can take. The pay page shows it under **Online payment**.

![A new fiat link](../img/fiat-new.png)

## Payment rails

A **rail account** is one way of being paid in crypto or over Lightning. An active rail account is offered on every new
invoice of its organization (unless the invoice names its methods itself). Open **Settings > Rail accounts**.

![Rail accounts](../img/rails-list.png)

**New rail account** first asks for the organization and the kind.

![Choosing the kind](../img/rail-new-choose.png)

| Kind | What it needs |
|---|---|
| **evm** | One address; the networks to watch (Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain) and the assets to accept (ETH, USDC, USDT). |
| **tron** | One address; USDT. Every Tron network is watched. |
| **solana** | One address; USDC and USDT. |
| **ton** | Off for now: it can only be saved inactive, because a TON payment cannot yet be proven to have arrived. |
| **btcpay** | The BTCPay `URL` and `Store ID`, the assets (BTC, XMR, ZEC) and a speed policy: `MediumSpeed` (1 confirmation, the default), `LowMediumSpeed` (2) or `LowSpeed` (6). `HighSpeed` is refused. |
| **lightning** | A backend: `nwc` (Nostr Wallet Connect, for example Alby Hub), `lnd`, `cln` or `lnbits`; a `URL` for the last three. Asset BTC. |

![A new EVM rail account](../img/rail-new-evm.png)

![A new Lightning rail account](../img/rail-new-lightning.png)

Things to know:

- **Use addresses that only this system uses.** A chain payment is matched to its invoice by its **amount**: each quote
  asks for the base amount plus a few units of dust, unique among the quotes that hold that address. Money sent there
  for any other reason lands in Needs a person.
- A rail URL must be an `https://` address on a host that resolves only to public addresses; a loopback or private
  address is refused (an LND or CLN on the same machine is the one exception, on `LOCAL_LIGHTNING_PORT`).
- While a client may still pay a quote, the account's organization, kind, address, networks and assets cannot change; create a
  new account instead. **Deactivate** stops offering it, while quotes already shown keep being watched until released.
- The *Watcher* column says whether the worker can read that chain or gateway.

![A BTCPay rail account](../img/rail-edit-btcpay.png)

### Credentials

BTCPay and Lightning secrets are encrypted with the vault key and **never shown again**. Open the account and press
**Set credentials** (the vault must be unsealed), or use `manage.py set_rail_credentials <rail account id>` in a shell.
Leave a field empty to keep what is stored.

The system **never spends**, so it stores only a credential that provably cannot:

| Backend | What to paste |
|---|---|
| BTCPay | `api_key` with only `btcpay.store.canviewinvoices` and `btcpay.store.cancreateinvoice` (BTCPay is asked what the key may do), and the `webhook_secret`. The button **Register the BTCPay webhook** creates the webhook in BTCPay and stores its secret for you. |
| Lightning, NWC | `nwc_url` of a connection with `make_invoice` and `lookup_invoice` and **no** `pay_*` method. |
| Lightning, LND | `macaroon` baked with `invoices:read invoices:write info:read` (LND's own `invoice.macaroon` is refused). |
| Lightning, Core Lightning | `rune` limited to `invoice` and `listinvoices`. |
| Lightning, LNbits | the wallet's **Invoice/read key**, never the Admin key; tick the box that says you checked. |

![Credentials of a BTCPay account](../img/rail-credentials-btcpay.png)

![Credentials of a Lightning account](../img/rail-credentials-lightning.png)

Changing the host of a rail's URL clears its stored credentials and deactivates it, so a key is never sent to a
different service. The README ([Crypto rails](../../../README.md#crypto-rails)) has the details per backend, the
environment variables for your own RPC endpoints and what the watchers cannot see.

## API tokens and scopes

**Settings > API tokens**. A token belongs to **one organization** and sees only that organization's data. It is shown
**once**, when it is created; if it is lost, revoke it and make a new one. Revoking is permanent and the row stays.

![Tokens](../img/tokens.png)

| Scope | Allows |
|---|---|
| `read` | Every GET. |
| `invoices:write` | Create and update contacts and their encryption keys; create, send, void invoices; reissue links; manage payment links. |
| `payments:confirm` | Confirm, reject, reverse and assign payments, acknowledge refunds, dismiss declarations. **This moves money state.** It cannot record a manual payment: only a person can. |
| `admin` | Everything above, and the webhook endpoints. |

Give a token the smallest set it needs; an integration that only creates and sends invoices needs `read` and
`invoices:write`. Creating, rescoping and revoking are step-up changes.

![A token, shown once](../img/token-created.png)

You can also issue one from a shell: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## Webhooks

**Settings > Webhooks** registers an endpoint that is told what happens to an organization's invoices and payments.

![Webhooks](../img/webhooks.png)

- The address must be `https://`, on port 443, on a host that resolves only to public addresses. (A development setup can
  lift this with `WEBHOOK_ALLOW_HTTP` and `WEBHOOK_ALLOW_PRIVATE`.)
- Tick the events you want; ticking none means all of them: `invoice.created`, `.sent`, `.voided`, `.declared`,
  `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`,
  `.rejected`, `.reversed`, `staff.alert` and `ping`.
- The **secret** is shown once. Each delivery is a signed `POST` with `X-Pay-Signature: sha256=<hex>`, an HMAC-SHA256 of
  the raw body with that secret; check it before you trust the call. The body carries ids and statuses only, never
  names, addresses or amounts: read the invoice through the API for the rest.
- A delivery is retried after 2, 5, 15, 30, 60, 120, 240 and 480 minutes, then marked failed; the staff are told once
  and it is listed under **Webhooks not delivered** in Needs a person.
- An endpoint's page has **Send a ping**, the latest deliveries and **Delete**. Settings > Webhook deliveries lists
  every delivery with **Retry now**.

![An endpoint and its deliveries](../img/webhook-detail.png)

The full list of events and the verification code is in `/llms.txt` on your installation (chapter 9).

## The team

**Settings > Team** lists everyone who can sign in, with their second factors by name and the last sign-in. A superuser
can remove a colleague's lost factor (a step-up change, e-mailed to every superuser); the colleague is then locked out
until someone enrols a new authenticator on the server. Your own last factor cannot be removed.

![The team](../img/team.png)
