# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Do not open a public issue, and do not include real client data
in the report.

Say what an attacker needs (a client link, a staff account, an API token and its scopes, a local
account on the same host, nothing at all), what they get, and how to reproduce it, ideally as a
test against this repository. We answer within a week, agree a disclosure date with you, and credit
you in the fix unless you prefer not to be named.

## Supported versions

Sealbill has no releases yet. Fixes go to the `main` branch only; run a recent `main`.

## Scope

In scope: the Django application under `apps/` and `config/`, the MCP server under `mcp_server/`,
the deployment examples under `deploy/`, and the migration tools under `tools/migrate/`.

The design assumes the following, and a report that relies on breaking one of these is still
welcome, but it describes a limitation rather than a bug:

- **Root on the server, or the service account, while the vault is unsealed, can read client
  data.** The vault key has to be in memory and on the tmpfs for the application to work. What the
  vault protects against is a copy of the database or a backup without the vault password, and a
  stolen disk after a reboot.
- **The chain watchers read public endpoints.** A lying RPC or indexer can delay a payment or make
  one look missing. Set your own endpoints (`EVM_RPC_URLS`, `SOLANA_RPC_URLS`, `TRONGRID_API_URL`)
  if that matters to you. Native ETH sent by a contract (an internal transfer) is not seen at all;
  the operations manual says what to do about it.
- **TON is off** (`TON_ENABLED=false`): the arrival of a TON payment cannot be verified yet, so it
  must stay off.
- **Shared hosts.** On a host where other accounts run software as Apache's group (`www-data`, as
  PHP-FPM often does), those accounts can reach the gunicorn socket. The proxy secret stops them
  from forging the client address, but not from tying up the workers. The operations manual
  describes the host-level fix.

## Good practice for operators

- Use a generated vault password of 24 or more characters. Keep it, `vault.json` and the backup
  passphrase somewhere other than the server: without all three no backup can be read.
- Give API tokens the fewest scopes that work. The MCP server refuses to start with a token that
  holds `payments:confirm` or `admin`.
- Give BTCPay and Lightning only receive-only credentials. Sealbill refuses one that can spend,
  except an LNbits key, which it cannot tell apart from an admin key.
- Use new receiving addresses that only Sealbill uses: payments are matched by their exact amount.
- Never run the test suite on a host that holds real credentials.
