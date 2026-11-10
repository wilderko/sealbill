# Contributing to Sealbill

Thank you for helping. Sealbill handles other people's money and personal data, so the bar for a
change is that it is tested, that it fails closed, and that it says plainly what it does.

## Setting up

```bash
uv sync                               # Python 3.12+, plus the system packages in README.md
uv run manage.py test --parallel 8    # SQLite in memory, no network, no .env needed
node --test tools/migrate/            # optional: the Request Finance export script (Node 20+)
```

The test settings (`config/settings/test.py`) switch off every outbound call: chain RPCs,
exchange rates, webhooks and DNS lookups. Keep it that way: a test that needs an answer from the
outside mocks it. Tests that assert PostgreSQL behaviour skip on SQLite; run them with
`DATABASE_URL` pointing at a PostgreSQL server where the test runner may create a database.

Never run the suite on a machine that holds real credentials or a real `.env`.

## Making a change

- **Write the test first**, and make it fail for the right reason before you fix anything.
- **Fail closed.** When the vault is sealed, a value cannot be decrypted or an outside service does
  not answer, refuse and say why. "Could not tell" must never read as "it is fine".
- **No personal data in logs, errors, management command output or audit entries in plaintext.**
  Name records by id. Refusal messages name the problem, never the value.
- **Money is integers in minor units**, or `Decimal` parsed from strings. Never a float.
- **Client-facing text exists in all nine languages.** Add the string with `{% translate %}` or
  `gettext`, run `uv run manage.py makemessages -a --no-wrap`, translate every new entry in
  `locale/*/LC_MESSAGES/django.po` by hand, remove its `#, fuzzy` flag, and compile with
  `uv run manage.py compilemessages`. Never clear fuzzy flags in bulk: a fuzzy match is often a
  similar-looking string that means something else.
- **Templates follow the Content Security Policy**: no inline `<script>` without the
  `nonce="{{ csp_nonce }}"` attribute, no `on*=` attributes, no `style=` attributes.
- **A new outbound call** goes through `apps/rails/http.py` (rails) or the webhook delivery code, so
  the switches that keep tests and sealed states offline cover it.
- **A new migration** must be safe on a database that holds encrypted data: if it needs the vault,
  it stops with "unseal the vault first" and changes nothing.
- Keep the operations manual (`docs/operations.md`) in step with the code: several tests read it.

## Pull requests

Describe what changes for staff, clients or operators, how you tested it, and anything a reviewer
should look at twice. Small pull requests are reviewed faster than large ones.

Report security problems privately, as described in [SECURITY.md](SECURITY.md), not in a pull
request or an issue.

By contributing you agree that your contribution is licensed under the GNU Affero General Public
License, version 3 only, like the rest of Sealbill.
