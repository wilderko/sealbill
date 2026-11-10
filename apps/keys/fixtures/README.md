# Test-only OpenPGP keys

Everything in this directory exists for the test suite and nothing else. These
keys were generated once, in a throw-away `GNUPGHOME`, for the made-up
identities `alice@example.test` and `bob@example.test`. They protect nothing
and must never be used for real mail.

| File | What it is |
|---|---|
| `alice_pub.asc` | Alice: ed25519 primary (sign, certify) + cv25519 encryption subkey, both expiring 2099-12-31 |
| `alice_sec.asc` | Alice's secret key, **without a passphrase**. It is here solely so tests can decrypt what was encrypted to `alice_pub.asc`. |
| `expired_pub.asc` | A key for `alice@example.test` made with `--faked-system-time 20200101T000000` that expired on 2021-01-01 |
| `revoked_pub.asc` | A key for `alice@example.test` with its own revocation certificate applied |
| `signonly_pub.asc` | A key for `alice@example.test` with an ed25519 primary and no encryption subkey |
| `subkey_expired_pub.asc` | A key for `alice@example.test` whose primary (made 2020-01-01 with a faked clock) never expires, while its only encryption subkey expired on 2021-01-01. Fingerprint `24AB7EC99988BA9CFDCE3EBCD8A5A45485A35A78` |
| `subkey_revoked_pub.asc` | A key for `alice@example.test` whose primary never expires, while its only encryption subkey is revoked. Fingerprint `208975927889F186DE7C8A9D3BC1EF994FC26190` |
| `two_keys_pub.asc` | `alice_pub.asc` followed by Bob's public key, two armored blocks in one file |

S/MIME certificates are not stored here: the tests generate them with
`cryptography` as they run.

## How the two subkey fixtures were made

With GnuPG 2.4.4, in a throw-away `GNUPGHOME` (afterwards: `gpgconf --kill all`,
`gpgconf --remove-socketdir`, then remove the directory), and
`g() { gpg --batch --pinentry-mode loopback --passphrase '' "$@"; }`:

```bash
# subkey_expired_pub.asc: the primary never expires; the encryption subkey expired in 2021.
g --faked-system-time 20200101T000000 --quick-gen-key 'Alice Subkey Expired <alice@example.test>' ed25519 sign,cert never
g --faked-system-time 20200101T000000 --quick-add-key "$FPR" cv25519 encr 2021-01-01
gpg --armor --export "$FPR" > subkey_expired_pub.asc

# subkey_revoked_pub.asc: the primary never expires; the encryption subkey is revoked.
g --quick-gen-key 'Alice Subkey Revoked <alice@example.test>' ed25519 sign,cert never
g --quick-add-key "$FPR" cv25519 encr never
printf 'key 1\nrevkey\ny\n0\n\ny\nsave\n' | g --command-fd 0 --edit-key "$FPR"
gpg --armor --export "$FPR" > subkey_revoked_pub.asc
```

`$FPR` is the primary's fingerprint, read with `gpg --with-colons --list-keys`.
`gpg --with-colons` on the result shows `sub:e:` (expired) and `sub:r:` (revoked)
under a valid `pub:u:` primary.
