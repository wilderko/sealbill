#!/usr/bin/env bash
# Restore an archive made by backup-db.sh: the database (into DATABASE_URL, or the PG*
# variables) and the vault config (to VAULT_CONFIG_PATH).
# Usage: restore-db.sh [--force] [--unverified] pay-DATE.tar.enc
# The archive is checked against pay-DATE.tar.enc.hmac (archive-mac.py) before anything is decrypted:
# a changed archive, or one made with another passphrase, is refused. An archive made before
# 2026-11-11 has no .hmac and is refused too, unless --unverified says to restore it unchecked.
# Needs BACKUP_PASSPHRASE_FILE, VAULT_CONFIG_PATH and VAULT_KEY_PATH (the app's tmpfs key file).
# An existing vault.json that differs from the archive's is never replaced without --force:
# it may be the only copy that opens another database. Nothing is restored when it refuses.
#
# It also refuses while pay-web.socket, pay-web or pay-worker is active: a process holding the old
# key would fail every restored letter and encrypt new rows with a key the restored vault cannot
# derive (audit DATA-2). Stop them first; it never stops or starts them itself. Where these units do
# not exist (Docker Compose: stop the web and worker containers yourself), it refuses as well (none of
# the three installed, or no systemctl at all) until PAY_RESTORE_SKIP_UNIT_CHECK=1 is set. After
# installing vault.json it removes the old key file (never vault.json itself), so the units come up
# sealed: start them, then unseal with the backup's password.
set -euo pipefail
umask 077
force=0
unverified=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1; shift ;;
    --unverified) unverified=1; shift ;;
    --) shift; break ;;
    -*) echo "usage: $0 [--force] [--unverified] <archive.tar.enc>" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -eq 1 ] || { echo "usage: $0 [--force] [--unverified] <archive.tar.enc>" >&2; exit 2; }
: "${BACKUP_PASSPHRASE_FILE:?set BACKUP_PASSPHRASE_FILE}"
: "${VAULT_CONFIG_PATH:?set VAULT_CONFIG_PATH (where the app reads vault.json)}"
: "${VAULT_KEY_PATH:?set VAULT_KEY_PATH (the key file of the vault being replaced; it is removed)}"
# Work from / (a directory this account cannot read must not break anything); relative paths first.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
abs() { case $1 in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
archive=$(abs "$1")
BACKUP_PASSPHRASE_FILE=$(abs "$BACKUP_PASSPHRASE_FILE")
VAULT_CONFIG_PATH=$(abs "$VAULT_CONFIG_PATH")
VAULT_KEY_PATH=$(abs "$VAULT_KEY_PATH")
cd /
# The key file is removed after vault.json is installed: the same file would remove vault.json.
if [ "$(realpath -m -- "$VAULT_KEY_PATH")" = "$(realpath -m -- "$VAULT_CONFIG_PATH")" ] \
    || [ "$VAULT_KEY_PATH" -ef "$VAULT_CONFIG_PATH" ]; then
  echo "VAULT_KEY_PATH names the same file as VAULT_CONFIG_PATH; nothing restored." >&2
  exit 1
fi

if [ "${PAY_RESTORE_SKIP_UNIT_CHECK:-}" != "1" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found: cannot tell whether pay-web and pay-worker are running; nothing restored." >&2
    echo "Stop the app first (Docker Compose: docker compose stop web worker), then run again with" >&2
    echo "PAY_RESTORE_SKIP_UNIT_CHECK=1." >&2
    exit 1
  fi
  # A unit that is not installed is "inactive" to is-active too, so where none of them is (Docker on
  # a host that runs systemd) the check would pass whatever is running. Not this layout: refuse.
  installed=0
  for unit in pay-web.socket pay-web pay-worker; do
    if [ "$(systemctl show -p LoadState --value "$unit" 2>/dev/null)" = "loaded" ]; then
      installed=1
    fi
  done
  if [ "$installed" -ne 1 ]; then
    echo "pay-web.socket, pay-web and pay-worker are not installed here (LoadState not-found): this is not" >&2
    echo "the systemd layout, so whether the app runs cannot be told; nothing restored. Stop it yourself" >&2
    echo "(Docker Compose: docker compose stop web worker), then run again with PAY_RESTORE_SKIP_UNIT_CHECK=1." >&2
    exit 1
  fi
  running=""
  for unit in pay-web.socket pay-web pay-worker; do
    if systemctl is-active --quiet "$unit"; then
      running="$running $unit"
    fi
  done
  if [ -n "$running" ]; then
    echo "active:$running; nothing restored. A running process would keep the old vault key." >&2
    echo "Stop them first: systemctl stop pay-web.socket pay-web pay-worker" >&2
    exit 1
  fi
fi

# Integrity first: nothing is decrypted from an archive that cannot be shown to be the one written.
if [ -e "$archive.hmac" ]; then
  if ! python3 -I "$here/archive-mac.py" verify "$archive" "$BACKUP_PASSPHRASE_FILE" "$archive.hmac"; then
    echo "$archive does not match $archive.hmac: it was changed, or this is not the passphrase it was" >&2
    echo "made with; nothing restored." >&2
    exit 1
  fi
elif [ "$unverified" -ne 1 ]; then
  echo "$archive has no $archive.hmac, so whether it was changed cannot be checked; nothing restored." >&2
  echo "An archive made before 2026-11-11 has none: run again with --unverified to restore it unchecked." >&2
  exit 1
fi

d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT

mkdir "$d/x"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$archive" -pass "file:$BACKUP_PASSPHRASE_FILE" \
  | tar -xf - -C "$d/x" db.dump vault.json
[ -s "$d/x/db.dump" ] && [ -s "$d/x/vault.json" ] || { echo "the archive lacks db.dump or vault.json" >&2; exit 1; }

if [ -e "$VAULT_CONFIG_PATH" ] && ! cmp -s "$d/x/vault.json" "$VAULT_CONFIG_PATH" && [ "$force" -ne 1 ]; then
  echo "$VAULT_CONFIG_PATH exists and differs from the archive's vault.json; nothing restored." >&2
  echo "Keep a copy of it, then run again with --force to replace it." >&2
  exit 1
fi

# shellcheck source=pgenv.sh
. "$here/pgenv.sh"
load_pg_env
pg_restore --clean --if-exists --no-owner --dbname="$PGDATABASE" < "$d/x/db.dump"

mkdir -p "$(dirname "$VAULT_CONFIG_PATH")"
staged="$VAULT_CONFIG_PATH.restore.$$"
install -m 600 "$d/x/vault.json" "$staged"
mv -f "$staged" "$VAULT_CONFIG_PATH"
# The key of the vault just replaced must not be picked up when the units start.
if [ "$VAULT_KEY_PATH" -ef "$VAULT_CONFIG_PATH" ]; then  # checked above; never remove vault.json
  echo "VAULT_KEY_PATH is $VAULT_CONFIG_PATH; it was not removed." >&2
  exit 1
fi
rm -f -- "$VAULT_KEY_PATH"
echo "restored the database $PGDATABASE and $VAULT_CONFIG_PATH; removed $VAULT_KEY_PATH"
echo "Now start the units, then unseal with the backup's password:"
echo "  systemctl start pay-web.socket pay-web pay-worker, then payinv-manage unseal_vault (or /unlock/)"
