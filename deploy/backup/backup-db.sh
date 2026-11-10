#!/usr/bin/env bash
# Encrypted nightly backup: the PostgreSQL dump AND the vault config, in one archive.
# The dump alone is unreadable: its encrypted fields open only with the vault password and
# the salt kept in vault.json. Configure through the environment (or a file it is started from):
#   BACKUP_DIR              target directory (created 0700)
#   BACKUP_PASSPHRASE_FILE  file holding the passphrase, never in the repository: 0640 root:payinv
#                           in the README's layout (the backup runs as payinv; only root may change it)
#   VAULT_CONFIG_PATH       the app's vault.json (salt + sentinel; never the key)
#   DATABASE_URL            the database URI (as in the app's environment file): either
#                           postgres:///dbname?host=/var/run/postgresql (the Unix socket, peer
#                           authentication, no password) or postgres://user:password@host/dbname.
#                           It is split into PG* variables, and a password into a private passfile,
#                           so it never reaches a command line (deploy/backup/pgenv.sh).
#                           Without it, set PGHOST, PGUSER, PGDATABASE (and PGPASSFILE if needed).
# Writes pay-<UTC time>.tar.enc: tar(db.dump, vault.json) encrypted with openssl AES-256-CBC.
set -euo pipefail
umask 077

: "${BACKUP_DIR:?set BACKUP_DIR}"
: "${BACKUP_PASSPHRASE_FILE:?set BACKUP_PASSPHRASE_FILE}"
: "${VAULT_CONFIG_PATH:?set VAULT_CONFIG_PATH (the vault.json the database was encrypted under)}"
# Work from /: started from a directory this account cannot read (sudo -u payinv from /root), find
# could not return to it and the script exited 1 after the archive was written. Relative paths are
# made absolute first.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
abs() { case $1 in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
BACKUP_DIR=$(abs "$BACKUP_DIR")
BACKUP_PASSPHRASE_FILE=$(abs "$BACKUP_PASSPHRASE_FILE")
VAULT_CONFIG_PATH=$(abs "$VAULT_CONFIG_PATH")
cd /
[ -r "$VAULT_CONFIG_PATH" ] || { echo "cannot read the vault config $VAULT_CONFIG_PATH" >&2; exit 1; }
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

out="$BACKUP_DIR/pay-$(date -u +%Y%m%dT%H%M%SZ).tar.enc"
tmp="$out.partial"
d=$(mktemp -d)
trap 'rm -f "$tmp"; rm -rf "$d"' EXIT
# shellcheck source=pgenv.sh
. "$here/pgenv.sh"
load_pg_env

mkdir "$d/archive"
# No positional argument: the connection comes from the PG* variables and PGPASSFILE.
pg_dump --format=custom > "$d/archive/db.dump"
cp "$VAULT_CONFIG_PATH" "$d/archive/vault.json"
tar -C "$d/archive" -cf - db.dump vault.json \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$BACKUP_PASSPHRASE_FILE" -out "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$out"

find "$BACKUP_DIR" -maxdepth 1 -name 'pay-*.enc' -type f -mtime "+$KEEP_DAYS" -delete
echo "wrote $out"
