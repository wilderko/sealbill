# 1. Getting started

## What you are running

Sealbill is one web application plus one background worker.

- The **web application** serves the staff pages (at the root of the site, for example `https://pay.example.org/`), the
  client pages (`/i/…` shows an invoice, `/p/…` is where it is paid, `/l/…` is a payment link), the API (`/api/v1/…`)
  and the unlock page (`/unlock/`).
- The **worker** (`manage.py run_worker`) does everything that has to happen without a click: it sends queued e-mail,
  marks invoices overdue, watches the chains and the gateways for payments, expires quotes, sends the one payment
  reminder and delivers webhooks. **Without the worker nothing is e-mailed and no crypto payment is noticed.** Run it
  as a second process next to the web application; `manage.py run_worker --once` runs every job once and exits, which
  is handy for a test.

## Install and first start

The README of the repository has the commands for each way of running it, so they are not repeated here:

- on your own machine, to try it: [README, Development](../../../README.md#development)
- with Docker Compose: [README, Docker Compose](../../../README.md#docker-compose)
- on a server with systemd, Apache and PostgreSQL: [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Whichever you choose, the order of the first start is the same:

1. Create the database tables (`migrate`, and `createcachetable` for the rate-limit tables).
2. Create the vault and unseal it (next section).
3. Create the first user with `manage.py createsuperuser`.
4. Give that user a second factor: `manage.py create_staff_totp <username>` prints an `otpauth://` address for an
   authenticator app. It needs the vault unsealed. You can add a passkey later on the Security page.
5. Start the web application and the worker, open the site and sign in.
6. Under **Settings**, create an organization, a bank account or a rail account (chapter 2), and send a first invoice
   to yourself (chapter 4).

## The vault

Everything a person typed (names, addresses, tax ids, invoice lines, notes, credentials of your payment gateways) is
stored encrypted. The key is derived from a **vault password** and kept only in the memory-backed file named by
`VAULT_KEY_PATH`; it is never in the environment or on the disk.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # once; at least 16 characters
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # after every restart of the machine
```

Type the password on standard input, as above, so that it never appears in a command line or in the shell history.
`init_vault` only creates `vault.json` (the salt and a check value, not the key); `unseal_vault` opens it.

After a reboot, or whenever the memory-backed file is wiped, the vault is **sealed**. A graceful reload of the web
service keeps it open; a hard restart does not. While it is sealed:

- every staff page redirects to **/unlock/**: enter the vault password there, or run `unseal_vault`;
- the pay pages, payment links and the API answer "temporarily unavailable" (503), so no client can get a quote;
- the worker still sees and matches chain payments (it only reads plain columns), but e-mail and webhooks wait and
  the gateway polling (BTCPay, Lightning) pauses; the operator gets one alert e-mail per sealed episode.

![The unlock page, shown when the vault is sealed](../img/vault-unlock.png)

`GET /healthz` reports `"vault": "sealed"` or `"unsealed"` without any data, so a monitor can watch it. Keep a copy of
`vault.json`, the backup passphrase and the vault password somewhere other than the server: a database without all of
them cannot be read. The complete list of what runs while the vault is sealed is in the
[README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Signing in

Staff sign in at `/login/` in two steps: the **password**, then a **second factor**, which is a passkey or a six-digit
code from an authenticator app. A session lasts eight hours and ends when the browser closes. The sign-in is
rate-limited per address, per account and per device.

![The password step](../img/start-login.png)

![The second factor step](../img/start-verify.png)

When the account has a passkey the second step offers **Use a passkey** first and the code field below it ("or enter a
code"). A passkey belongs to this site only, so a look-alike page cannot use it.

![The second step of an account that has a passkey](../img/verify-passkey.png)

After signing in you land on **Needs a person** (chapter 6). The rail on the left is the whole navigation: Needs a
person, Invoices, New invoice, Contacts, Payment links, Exports, Settings; the shield at the bottom is **Security**, the
arrow signs you out.

![Needs a person, the first page after signing in](../img/needs-person.png)

### Security: your own passkeys and authenticators

**Security** (the shield icon) lists your second factors. Add a passkey by naming it (for example "MacBook Touch ID" or
"YubiKey 5C") and pressing **Add a passkey**; the browser then asks for the fingerprint, face or PIN. An authenticator
app is added on the server with `create_staff_totp`, never in the browser. The last second factor of an account cannot
be removed, and removing the one this session used asks for another first. Every addition and removal is in the audit
log.

![Security, with a passkey added](../img/security-passkey.png)

## Staff and superusers

Anyone with a staff account can issue invoices, record payments and work the queues. Some changes can move money or
reroute a client, so they need more.

### The step-up

These changes need a **superuser** who gave a second factor **within the last ten minutes**:

- creating, rescoping or revoking an API token;
- a rail account and its credentials (and the BTCPay webhook, which stores a secret);
- an organization's US-resident issuer flag and its return hosts;
- a bank account or a fiat payment link of an organization;
- adding an e-mail address to a contact or making one primary (letters, pay links and PDFs go there);
- removing another staff member's second factor.

The pages say where they stand before you press anything:

- a staff member who is not a superuser sees "Only a superuser may …" and nothing changes;

  ![A staff member who is not a superuser](../img/stepup-not-superuser.png)

- a superuser whose last second factor is older than ten minutes sees **Verify again**; it leads to the verify page and
  brings you back to the page you were on.

  ![A change that needs a fresh second factor](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Every step-up change also sends an e-mail to every active superuser: who, what and when, by names and ids and never a
secret, an account number or an address. If a stolen session changes something, the owner of the system hears of it.

The **Team** page (Settings > Team) lists everyone who can sign in, their second factors and the last sign-in, and
lets a superuser remove a colleague's lost factor. New staff users are created with `createsuperuser` or in the hidden
Django admin (the path in `DJANGO_ADMIN_PATH`; superuser plus second factor only; everyone else gets a 404).
