# Sourced by backup-db.sh and restore-db.sh. Turns DATABASE_URL into PG* variables (and, when the
# URL carries a password, a 0600 passfile), so the password never appears in a command line
# (ps, /proc/*/cmdline). Needs $d: a private temporary directory the caller creates and removes.
#
# Two forms of DATABASE_URL:
#   postgres://user:password@host:port/dbname          TCP; the password goes to the passfile
#   postgres:///dbname?host=/var/run/postgresql        the Unix socket in that directory (peer
#                                                      authentication: no password, no passfile)
# An empty host means libpq's default socket directory (PGHOST stays unset). A host that is a
# directory (from ?host= or percent-encoded, %2Fvar%2Frun%2Fpostgresql) is a socket directory.
# The user is the URL's (or ?user=), else the account running the script, as libpq would choose.
load_pg_env() {
  if [ -z "${DATABASE_URL:-}" ]; then
    [ -n "${PGDATABASE:-}" ] || { echo "set DATABASE_URL or PGDATABASE (and the other PG* variables)" >&2; return 1; }
    return 0
  fi
  # The URL is read from the environment, not argv; only shell-quoted assignments come back.
  local parsed
  parsed=$(python3 -c '
import os, pwd, shlex, sys
from urllib.parse import urlsplit, unquote, parse_qs
u = urlsplit(os.environ["DATABASE_URL"])
if u.scheme not in ("postgres", "postgresql") or not u.path.strip("/"):
    sys.exit("DATABASE_URL must look like postgres://user:password@host:port/dbname "
             "or postgres:///dbname?host=/var/run/postgresql")
query = {k: v[-1] for k, v in parse_qs(u.query).items()}
hostport = u.netloc.rpartition("@")[2]
if hostport.startswith("["):  # [IPv6]:port
    netloc_host = hostport[1:hostport.find("]")]
else:
    netloc_host = unquote(hostport.split(":", 1)[0])  # not u.hostname: it lowercases a directory
host = query.get("host") or netloc_host
port = query.get("port") or (str(u.port) if u.port else "5432")
user = unquote(u.username or "") or query.get("user", "") or pwd.getpwuid(os.geteuid()).pw_name
pw = unquote(u.password or "") or query.get("password", "")
db = unquote(u.path.lstrip("/"))
esc = lambda s: s.replace("\\", "\\\\").replace(":", "\\:")
# A socket connection matches the passfile line whose host field is the socket directory, or
# "localhost" for the default one.
line = ":".join(esc(x) for x in (host or "localhost", port, db, user, pw)) if pw else ""
out = {"PGHOST": host, "PGPORT": port, "PGUSER": user, "PGDATABASE": db, "PGPASS_LINE": line}
ssl = query.get("sslmode")
if ssl:
    out["PGSSLMODE"] = ssl
for k, v in out.items():
    print("%s=%s" % (k, shlex.quote(v)))
') || { echo "could not parse DATABASE_URL" >&2; return 1; }
  eval "$parsed"
  if [ -n "$PGHOST" ]; then export PGHOST; else unset PGHOST; fi
  export PGPORT PGUSER PGDATABASE
  [ -z "${PGSSLMODE:-}" ] || export PGSSLMODE
  if [ -n "$PGPASS_LINE" ]; then
    printf '%s\n' "$PGPASS_LINE" > "$d/pgpass"
    chmod 600 "$d/pgpass"
    export PGPASSFILE="$d/pgpass"
  fi
  unset PGPASS_LINE parsed
}
