"""The Compose file, .env.example and README describe a path that actually works."""
import os
import re
import shlex
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management import load_command_class
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)
HTTP_VHOST = "deploy/apache/pay-invoicing.conf.example"  # :80, installed first (ACME webroot, redirect)
HTTPS_VHOST = "deploy/apache/pay-invoicing-ssl.conf.example"  # :443, installed once the certificate exists


class ComposeTests(SimpleTestCase):
    def setUp(self):
        self.compose = yaml.safe_load((BASE / "docker-compose.yml").read_text())
        self.services = self.compose["services"]

    def test_web_is_published_on_loopback_only(self):
        self.assertEqual(self.services["web"]["ports"], ["127.0.0.1:8000:8000"])
        for name, service in self.services.items():
            if name != "web":
                self.assertNotIn("ports", service, name)

    def test_web_creates_its_tables_before_serving(self):
        command = self.services["web"]["command"]
        self.assertLess(command.index("migrate"), command.index("createcachetable"))
        self.assertLess(command.index("createcachetable"), command.index("gunicorn"))

    def test_web_and_worker_share_media_and_the_vault(self):
        for mount in ("media:/app/media", "vault-key:/run/vault", "vault-config:/app/vault"):
            for name in ("web", "worker"):
                self.assertIn(mount, self.services[name]["volumes"], (name, mount))
        self.assertIn("media", self.compose["volumes"])

    def test_key_tmpfs_holds_only_the_key(self):
        self.assertNotIn("CACHE_DIR", (BASE / "docker-compose.yml").read_text())
        self.assertNotIn("CACHE_DIR", (BASE / "config" / "settings" / "base.py").read_text())

    def test_tmpfs_volumes_belong_to_the_app_user(self):
        # The image runs as uid 10001; a tmpfs mounted as root's 0700 is a key nobody can write.
        uid = re.search(r"--uid (\d+)", (BASE / "Dockerfile").read_text()).group(1)
        for volume in ("vault-key", "gpg-tmp"):
            opts = dict(o.partition("=")[::2] for o in self.compose["volumes"][volume]["driver_opts"]["o"].split(","))
            self.assertEqual(self.compose["volumes"][volume]["driver_opts"]["type"], "tmpfs", volume)
            self.assertEqual((opts.get("uid"), opts.get("gid"), opts.get("mode")), (uid, uid, "0700"), volume)

    def test_gpg_keyrings_have_their_own_tmpfs(self):
        # Without GPG_TMP_DIR, production puts the keyrings beside the key, on the 1 MiB key tmpfs.
        for name in ("web", "worker"):
            service = self.services[name]
            self.assertIn("gpg-tmp:/run/gpg", service["volumes"], name)
            self.assertEqual(service["environment"]["GPG_TMP_DIR"], "/run/gpg", name)
            key_dir = service["environment"]["VAULT_KEY_PATH"].rsplit("/", 1)[0]
            self.assertFalse(service["environment"]["GPG_TMP_DIR"].startswith(key_dir + "/"), name)


class EnvExampleTests(SimpleTestCase):
    def test_database_url_password_matches_postgres_password(self):
        env = dict(
            line.split("=", 1) for line in (BASE / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )
        password = re.match(r"postgres://[^:]+:([^@]+)@", env["DATABASE_URL"]).group(1)
        self.assertEqual(password, env["POSTGRES_PASSWORD"])


class ReadmeTests(SimpleTestCase):
    def setUp(self):
        self.readme = (BASE / "docs/operations.md").read_text()

    def test_compose_path_covers_every_step(self):
        compose = self.readme.split("## Docker Compose")[1].split("\n## ")[0]
        for step in ("migrate", "createcachetable", "createsuperuser", "init_vault", "unseal_vault", "/unlock/",
                     "create_staff_totp", "create_api_token"):
            self.assertIn(step, compose, step)
        self.assertIn("does not unseal", compose)

    def test_deployment_guide_covers_systemd_and_apache(self):
        guide = self.readme.split("## Deploying with systemd and Apache")[1]
        for needle in ("useradd", "pay-web.socket", "pay-web.service", "pay-worker.service",
                       "/run/pay-invoicing-web/gunicorn.sock", "/unlock/",
                       "WhiteNoise", "certbot", "/etc/sealbill/env", "healthcheck", "CREATE DATABASE"):
            self.assertIn(needle, guide, needle)

    def test_proxy_headers_are_documented(self):
        for needle in ("X-Forwarded-For", "X-Forwarded-Proto", "LAST"):
            self.assertIn(needle, self.readme)

    def test_every_management_command_line_parses(self):
        lines = re.findall(r"manage\.py ((?:create_api_token|create_staff_totp|revoke_api_token)[^\n`#|]*)", self.readme)
        self.assertTrue(lines)
        for line in lines:
            words = shlex.split(line.replace("<", "").replace(">", ""))
            app = {"create_staff_totp": "apps.staff"}.get(words[0], "apps.api")
            command = load_command_class(app, words[0])
            command.create_parser("manage.py", words[0]).parse_args(words[1:])  # SystemExit on bad syntax


STUB_ENV = {
    "SECRET_KEY": "x" * 60, "ALLOWED_HOSTS": "pay.example.org", "DATABASE_URL": "postgres://u:p@localhost/db",
    "SITE_URL": "https://pay.example.org", "VAULT_KEY_PATH": "/run/pay-invoicing/vault.key",
    "DJANGO_ADMIN_PATH": "x7Kq-prod-admin-path-0123456789/",
}


def _production(env, code="print('ok')"):
    import os
    import subprocess
    import sys

    full = {"PATH": os.environ.get("PATH", ""), "DJANGO_SETTINGS_MODULE": "config.settings.production", **env}
    return subprocess.run(
        [sys.executable, "-c", "import config.settings.production as s\n" + code],
        cwd=BASE, env=full, capture_output=True, text=True, timeout=60,
    )


class ProductionSettingsTests(SimpleTestCase):
    def test_production_refuses_missing_env(self):
        self.assertEqual(_production(STUB_ENV).returncode, 0, _production(STUB_ENV).stderr)
        for name in ("SECRET_KEY", "ALLOWED_HOSTS", "DATABASE_URL", "SITE_URL", "VAULT_KEY_PATH", "DJANGO_ADMIN_PATH"):
            with self.subTest(name):
                env = {k: v for k, v in STUB_ENV.items() if k != name}
                done = _production(env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("ImproperlyConfigured", done.stderr)

    def test_production_security_settings(self):
        done = _production(STUB_ENV, "print(s.SECURE_SSL_REDIRECT, s.SECURE_CONTENT_TYPE_NOSNIFF, s.X_FRAME_OPTIONS,"
                                     " s.CSRF_TRUSTED_ORIGINS, s.GPG_TMP_DIR, s.STORAGES['staticfiles']['BACKEND'],"
                                     " s.DEBUG)")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout.strip(),
            "True True DENY ['https://pay.example.org'] /run/pay-invoicing/gpg "
            "whitenoise.storage.CompressedManifestStaticFilesStorage False",
        )

    def test_whitenoise_after_security_middleware(self):
        middleware = settings.MIDDLEWARE
        self.assertEqual(middleware[0], "apps.core.proxy.ProxySecretMiddleware")  # tests_proxy
        self.assertEqual(middleware[1], "django.middleware.security.SecurityMiddleware")
        self.assertEqual(middleware[2], "whitenoise.middleware.WhiteNoiseMiddleware")
        self.assertNotIn("Manifest", settings.STORAGES["staticfiles"]["BACKEND"])  # tests need no collectstatic

    def test_proxy_secret_comes_from_the_environment_and_is_optional(self):
        # Optional: the Docker path has no Apache in front to send it.
        done = _production(STUB_ENV, "print(repr(s.PROXY_SECRET))")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "''"), done.stderr)
        done = _production({**STUB_ENV, "PROXY_SECRET": "from-the-env"}, "print(s.PROXY_SECRET)")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "from-the-env"), done.stderr)


class PackagingFilesTests(SimpleTestCase):
    def read(self, path):
        return (BASE / path).read_text()

    def test_systemd_units_declare_user_umask_runtime_dir_and_hardening(self):
        self.assertFalse(list((BASE / "deploy" / "systemd").glob("*.example")))
        for name in ("pay-web.service", "pay-worker.service"):
            text = self.read(f"deploy/systemd/{name}")
            for line in (
                "User=payinv", "Group=payinv", "UMask=0077", "RuntimeDirectory=pay-invoicing",
                "RuntimeDirectoryMode=0700", "RuntimeDirectoryPreserve=yes",
                "Environment=VAULT_KEY_PATH=/run/pay-invoicing/vault.key",
                "Environment=GPG_TMP_DIR=/run/pay-invoicing/gpg", "Environment=MEDIA_ROOT=/srv/sealbill/media", "EnvironmentFile=/etc/sealbill/env",
                "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                "ReadWritePaths=/srv/sealbill/media /srv/sealbill/vault",
                "WorkingDirectory=/srv/sealbill/app",
            ):
                self.assertIn(line, text, (name, line))
        web = self.read("deploy/systemd/pay-web.service")
        for needle in ("migrate --noinput", "createcachetable", "collectstatic --noinput", "ExecReload=/bin/kill -HUP",
                       "--workers 3", "--timeout 30"):
            self.assertIn(needle, web)
        self.assertIn("Environment=STATIC_ROOT=/run/pay-invoicing/static", web)  # ProtectSystem=strict
        self.assertNotIn("access-logfile", web)
        worker = self.read("deploy/systemd/pay-worker.service")
        for needle in ("manage.py run_worker", "Restart=always", "TimeoutStopSec=60"):
            self.assertIn(needle, worker)

    def test_apache_example_sets_forwarded_proto_and_body_limit(self):
        text = self.read(HTTPS_VHOST)
        for needle in (
            "<VirtualHost *:443>", "ServerName ${PAY_HOST}", "ProxyPreserveHost On",
            'RequestHeader set X-Forwarded-Proto "https"', "LimitRequestBody 2097152",
            'LocationMatch "^/i/[^/]+/key/"', "LimitRequestBody 131072", "options-ssl-apache.conf",
            "SSLCertificateFile /etc/letsencrypt/live/${PAY_HOST}/fullchain.pem",
            "env=!capability", "env=capability",
        ):
            self.assertIn(needle, text, needle)
        http = self.read(HTTP_VHOST)
        for needle in ("<VirtualHost *:80>", "ServerName ${PAY_HOST}", "RewriteRule"):
            self.assertIn(needle, http, needle)
        self.assertNotIn("<VirtualHost *:443>", http)  # installed before the certificate exists
        self.assertNotIn("<VirtualHost *:80>", text)

    def test_tls_options_file_is_optional_and_the_readme_says_what_replaces_it(self):
        # Only certbot's Apache plugin writes options-ssl-apache.conf; certbot certonly --webroot does not.
        https = self.read(HTTPS_VHOST)
        self.assertRegex(https, r"(?m)^\s*IncludeOptional /etc/letsencrypt/options-ssl-apache\.conf\s*$")
        self.assertNotRegex(https, r"(?m)^\s*Include /etc/letsencrypt/")
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for needle in ("options-ssl-apache.conf", "SSLProtocol -all +TLSv1.2 +TLSv1.3", "SSLCipherSuite ECDHE-"):
            self.assertIn(needle, guide, needle)

    def test_apache_proxies_to_the_unix_socket_and_no_port_is_left(self):
        text = self.read(HTTPS_VHOST)
        self.assertIn("ProxyPass / unix:/run/pay-invoicing-web/gunicorn.sock|http://pay-invoicing/", text)
        self.assertIn("ProxyPassReverse / http://pay-invoicing/", text)
        port = "80" + "11"
        for path in (HTTP_VHOST, HTTPS_VHOST, "deploy/systemd/pay-web.socket", "deploy/systemd/pay-web.service",
                     "deploy/systemd/pay-worker.service", "docs/operations.md", "deploy/env.example",
                     "deploy/cron/pay-invoicing.cron.example"):
            self.assertNotIn(port, self.read(path), path)
            self.assertNotIn("127.0.0.1:80", self.read(path).replace("127.0.0.1:8000", ""), path)

    def test_systemd_owns_the_socket_only_apache_can_reach(self):
        # pay-web.socket creates the socket and hands it to gunicorn (LISTEN_FDS). gunicorn 26.2 never binds,
        # chowns, chmods or unlinks an inherited socket (gunicorn/sock.py, arbiter.py). Bound by gunicorn
        # itself, the socket was chowned to the process's group, payinv, and Apache (www-data) was refused.
        sock = self.read("deploy/systemd/pay-web.socket").splitlines()
        for line in ("ListenStream=/run/pay-invoicing-web/gunicorn.sock", "SocketUser=payinv", "SocketGroup=www-data",
                     "SocketMode=0660", "DirectoryMode=0755", "[Install]", "WantedBy=sockets.target"):
            self.assertIn(line, sock, line)
        web = self.read("deploy/systemd/pay-web.service")
        self.assertIn("Requires=pay-web.socket", web.splitlines())
        after = next(ln for ln in web.splitlines() if ln.startswith("After="))
        self.assertIn("pay-web.socket", after.split("=", 1)[1].split())
        exec_start = next(ln for ln in web.splitlines() if ln.startswith("ExecStart="))
        for flag in ("--bind", "-b ", "--umask"):  # ignored under LISTEN_FDS; a bind of its own is the bug
            self.assertNotIn(flag, exec_start)
        # Its default control socket ($HOME/.gunicorn) is on the read-only file system: off, not a warning.
        self.assertIn("--no-control-socket", exec_start)
        self.assertNotIn("install -d", web)  # systemd makes the directory, root-owned, before gunicorn starts
        self.assertNotRegex(web, r"(?m)^ReadWritePaths=.*pay-invoicing-web")  # gunicorn writes nothing there
        readme = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for needle in ("X-Forwarded-For", "127.0.0.1", "www-data", "/run/pay-invoicing-web", "pay-web.socket",
                       "systemctl enable --now pay-web.socket pay-web pay-worker", "0660", "LISTEN_FDS"):
            self.assertIn(needle, readme, needle)
        self.assertNotIn("2750", readme)
        self.assertNotIn("0770", readme)

    def test_apache_sends_the_proxy_secret_that_www_data_cannot_read(self):
        # www-data runs PHP on the host too, and can reach the socket: only Apache knows the secret.
        https = self.read(HTTPS_VHOST)
        vhost = https.split("<VirtualHost *:443>")[1].split("</VirtualHost>")[0]
        # Include, not IncludeOptional: without the file Apache must refuse to start, not proxy without it.
        self.assertRegex(vhost, r"(?m)^\s*Include /etc/apache2/pay-invoicing-proxy-secret\.conf\s*$")
        self.assertIn('RequestHeader set X-Pay-Proxy-Secret "<value>"', https)  # set replaces the client's
        self.assertNotIn("X-Pay-Proxy-Secret", self.read(HTTP_VHOST))  # :80 proxies nothing
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for path, text in ((HTTPS_VHOST, https), ("docs/operations.md", guide)):
            self.assertNotRegex(text, r"RequestHeader\s+(?!set\s)\S+\s+X-Pay-Proxy-Secret", path)
        for needle in ('RequestHeader set X-Pay-Proxy-Secret "<value>"',
                       "chown root:root /etc/apache2/pay-invoicing-proxy-secret.conf",
                       "chmod 0600 /etc/apache2/pay-invoicing-proxy-secret.conf", "PROXY_SECRET=<value>",
                       "php-fpm", "mod_info"):
            self.assertIn(needle, guide, needle)
        self.assertRegex(self.read("deploy/env.example"), r"(?m)^PROXY_SECRET=change-me$")

    def test_readme_turns_extended_status_off(self):
        # WEB-4: mod_status keeps each worker slot's last request line after the request has ended, and
        # with ExtendedStatus On prints it for idle slots too: every local account reads /i/, /p/ and
        # /k/ tokens off /server-status. A <Location> rule is not enough, since a tenant's .htaccess can
        # SetHandler server-status for itself.
        step = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        step = step.split("8. **Apache**")[1].split("\n9. ")[0]
        for needle in ("ExtendedStatus Off", "a2dismod status", "/server-status", "SetHandler server-status",
                       "mod_info"):
            self.assertIn(needle, step, needle)

    def test_readme_says_what_www_data_can_do_to_the_socket(self):
        # WEB-5: a process running as www-data skips Apache and holds the sync workers with half-sent
        # requests. The fix is on the host (Apache under its own user, or per-site PHP-FPM pools); another
        # gunicorn worker class would only move the bar, so it stays as it is.
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for needle in ("every PHP site", "half-sent requests", "APACHE_RUN_USER", "per-site users",
                       "SocketGroup=", "worker class"):
            self.assertIn(needle, guide, needle)
        self.assertNotIn("--worker-class", self.read("deploy/systemd/pay-web.service"))

    def test_readme_restore_stops_the_units_and_unseals_with_the_backups_password(self):
        notes = self.read("docs/operations.md").split("- **Backups**")[1].split("\n## ")[0]
        for needle in ("systemctl stop pay-web.socket pay-web pay-worker", "PAY_RESTORE_SKIP_UNIT_CHECK=1",
                       "docker compose stop web worker", "VAULT_KEY_PATH",
                       "start the units, then unseal with the backup's password"):
            self.assertIn(needle, notes, needle)

    def test_units_wait_for_postgresql_and_cache_on_tmpfs(self):
        for name in ("pay-web.service", "pay-worker.service"):
            text = self.read(f"deploy/systemd/{name}")
            self.assertIn("Wants=postgresql.service", text, name)
            after = next(ln for ln in text.splitlines() if ln.startswith("After="))
            self.assertIn("postgresql.service", after, name)
            self.assertIn("Environment=XDG_CACHE_HOME=/run/pay-invoicing/cache", text, name)

    @staticmethod
    def log_config(text):
        formats = {name: fmt for fmt, name in
                   re.findall(r'(?m)^\s*LogFormat\s+"((?:[^"\\]|\\.)*)"\s+(\S+)\s*$', text)}
        logs = re.findall(r"(?m)^\s*CustomLog\s+(\S+)\s+(\S+)(?:\s+(env=\S+))?\s*$", text)
        return formats, logs

    def test_access_logs_carry_no_query_string_and_no_referer(self):
        for path in (HTTP_VHOST, HTTPS_VHOST):
            formats, logs = self.log_config(self.read(path))
            self.assertTrue(formats, path)
            self.assertTrue(logs, path)
            for name, fmt in formats.items():
                for banned in ("%r", "%q", "Referer", "%{Referer}i", "%>r"):
                    self.assertNotIn(banned, fmt, (path, name))
            for _file, name, _condition in logs:
                self.assertIn(name, formats, (path, name))  # a format of ours: never combined or common
        # :443: the URL (path only) except under the capability paths, where the path IS the token.
        formats, logs = self.log_config(self.read(HTTPS_VHOST))
        seen = set()
        for _file, name, condition in logs:
            if condition == "env=capability":
                self.assertNotIn("%U", formats[name], name)
                seen.add("capability")
            else:
                self.assertEqual(condition, "env=!capability", name)
                self.assertIn('\\"%m %U %H\\"', formats[name], name)
                seen.add("plain")
        self.assertEqual(seen, {"capability", "plain"})
        # :80 serves only the ACME files and a redirect: no URL at all, whatever was asked for.
        formats, logs = self.log_config(self.read(HTTP_VHOST))
        for _file, name, condition in logs:
            self.assertEqual(condition, "", name)  # one line for every request
            self.assertNotIn("%U", formats[name], name)
            self.assertIn('\\"%m\\" %>s %b %D', formats[name], name)
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        self.assertIn("query string", guide)

    def test_capability_requests_are_marked_on_the_normalised_path(self):
        # SetEnvIf Request_URI sees the raw URI: //i/<token>/ and /%69/<token>/ slipped past it, were
        # proxied and logged with %U, which is the decoded, slash-merged path. <LocationMatch> matches
        # that same normalised path; mod_env applies SetEnv in fixups, before mod_log_config's env= test.
        https = self.read(HTTPS_VHOST)
        block = re.search(r'(?ms)^\s*<LocationMatch "\^/\(i\|p\|k\|l\)/">\s*$(.*?)^\s*</LocationMatch>', https)
        self.assertIsNotNone(block)
        self.assertRegex(block.group(1), r"(?m)^\s*SetEnv capability 1\s*$")
        # Kept for requests refused before fixups run (a malformed path: 400, an encoded slash: 404).
        self.assertIn('SetEnvIf Request_URI "^/(i|p|k|l)/" capability', https)
        self.assertNotIn("SetEnvIf", self.read(HTTP_VHOST))  # nothing there logs a URL to choose for

    def test_apache_logs_never_carry_a_capability_token(self):
        https = self.read(HTTPS_VHOST)
        http = self.read(HTTP_VHOST)
        # 1. the :80 vhost has its own CustomLog (else the global other-vhosts log writes %r)
        self.assertIn("CustomLog", http)
        # 2. mod_proxy errors carry the request path
        self.assertIn("LogLevel warn proxy:crit proxy_http:crit", https)

    def test_certificate_renewals_are_never_proxied_or_redirected(self):
        http = self.read(HTTP_VHOST)
        self.assertIn("Alias /.well-known/acme-challenge/ /var/www/letsencrypt/.well-known/acme-challenge/", http)
        self.assertIn('<Directory "/var/www/letsencrypt/.well-known/acme-challenge/">', http)
        cond = http.index(r"RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/")
        self.assertLess(cond, http.index("RewriteRule"))
        https = self.read(HTTPS_VHOST)
        self.assertLess(https.index("ProxyPass /.well-known/acme-challenge/ !"), https.index("ProxyPass / unix:"))
        self.assertIn("Alias /.well-known/acme-challenge/", https)
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        first, cert, second = (guide.index("pay-invoicing.conf.example"),
                               guide.index("certbot certonly --webroot -w /var/www/letsencrypt"),
                               guide.index("pay-invoicing-ssl.conf.example"))
        self.assertLess(first, cert)
        self.assertLess(cert, second)
        self.assertIn("--deploy-hook", guide)  # webroot renewals do not reload Apache by themselves

    def test_readme_install_details(self):
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for needle in ("uv sync --frozen --no-dev --compile-bytecode", "/etc/sealbill/backup-passphrase",
                       "postgresql@18-main", "off the host", "postgres:///payinv?host=/var/run/postgresql"):
            self.assertIn(needle, guide, needle)
        self.assertNotIn("uv sync --frozen --no-dev\n", guide)
        self.assertNotIn("uv sync --frozen --no-dev`", guide)

    def test_readme_installs_every_system_package_the_dockerfile_does(self):
        # Measured on the host: without fonts-noto-cjk Hangul prints as .notdef boxes, and the regular
        # font's subset gets an encoding pypdf mis-decodes. The systemd path needs what the image has.
        docker = self.read("Dockerfile")
        install = re.search(r"apt-get install -y --no-install-recommends((?:[^&]|&(?!&))*)&&", docker).group(1)
        packages = install.replace("\\", " ").split()
        self.assertIn("fonts-noto-cjk", packages)
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        step = guide.split("0. **System packages**")[1].split("\n1. ")[0]
        command = re.search(r"apt-get install (?:[^\n]*\\\n)*[^\n]*", step).group(0)
        for package in packages:
            self.assertIn(package, command.replace("\\", " ").split(), package)
        self.assertLess(guide.index("0. **System packages**"), guide.index("1. **User and directories**"))

    def test_payinv_manage_runs_as_payinv_with_the_units_environment(self):
        script = self.read("deploy/payinv-manage")
        self.assertTrue(os.access(BASE / "deploy" / "payinv-manage", os.X_OK))
        self.assertIn("sudo -u payinv", script)
        self.assertIn(". /etc/sealbill/env", script)
        self.assertIn("DJANGO_SETTINGS_MODULE=config.settings.production", script)
        self.assertIn('manage.py "$@"\' payinv-manage "$@"', script)

    def test_readme_explains_log_redaction_interpreter_and_quoting(self):
        guide = self.read("docs/operations.md").split("## Deploying with systemd and Apache")[1]
        for needle in ("a2disconf other-vhosts-access-log", "proxy:crit", "journald", "uv venv --python /usr/bin/python3 ",
                       "UV_PYTHON_DOWNLOADS=never", "payinv-manage unseal_vault"):
            self.assertIn(needle, guide, needle)
        env = self.read("deploy/env.example")
        self.assertIn("bash", env)
        self.assertIn("PGPASSFILE", env)

    def test_cron_and_env_examples(self):
        cron = self.read("deploy/cron/pay-invoicing.cron.example")
        self.assertIn("backup-db.sh", cron)
        self.assertIn("manage.py healthcheck", cron)
        self.assertIn("*/10", cron)
        env = self.read("deploy/env.example")
        for name in ("SECRET_KEY", "ALLOWED_HOSTS", "DATABASE_URL", "SITE_URL", "VAULT_KEY_PATH", "DJANGO_ADMIN_PATH"):
            self.assertIn(name + "=", env)
        self.assertNotRegex(env, r"(?m)^SECRET_KEY=(?!change-me)\S")

    def test_dockerfile_non_root_and_pinned_uv(self):
        text = self.read("Dockerfile")
        self.assertNotEqual(re.search(r"(?m)^USER (\S+)", text).group(1), "root")
        pin = re.search(r"ghcr\.io/astral-sh/uv:(\S+)", text).group(1)
        self.assertNotEqual(pin, "latest")
        self.assertRegex(pin, r"^\d+\.\d+")
        self.assertIn("gnupg", text)
        self.assertIn("openssl", text)
        self.assertIn("collectstatic", text)
        self.assertIn(".env.*", self.read(".dockerignore"))

    def test_dockerfile_creates_the_volume_directories_for_the_app_user(self):
        # A named volume takes the owner of the image directory it is mounted on.
        text = self.read("Dockerfile")
        setup = next(block for block in text.split("\nRUN ") if "useradd" in block)
        for path in ("/app/vault", "/app/media"):
            self.assertIn(path, setup, path)
        self.assertRegex(setup, r"chown pay:pay [^\n]*/app/vault")
        self.assertRegex(setup, r"chown pay:pay [^\n]*/app/media")


class McpIsOptionalTests(SimpleTestCase):
    """The MCP server is an optional dependency group: the service installs and runs without it."""

    def test_mcp_is_an_optional_group_that_dev_installs(self):
        import tomllib

        project = tomllib.loads((BASE / "pyproject.toml").read_text())
        names = [re.split(r"[<>=~!\[ ;]", dep, maxsplit=1)[0].lower() for dep in project["project"]["dependencies"]]
        self.assertNotIn("mcp", names)
        self.assertTrue(any(dep.startswith("mcp") for dep in project["dependency-groups"]["mcp"]))
        self.assertIn({"include-group": "mcp"}, project["dependency-groups"]["dev"])
        section = (BASE / "docs/operations.md").read_text().split("## MCP server")[1].split("\n## ")[0]
        self.assertIn("uv sync --group mcp", section)

    def test_the_service_never_imports_mcp(self):
        for path in [*(BASE / "apps").rglob("*.py"), *(BASE / "config").rglob("*.py")]:
            self.assertNotRegex(path.read_text(), r"(?m)^\s*(import mcp\b|from mcp[ .])", str(path))

    def test_mcp_server_tests_skip_cleanly_without_mcp(self):
        import subprocess
        import sys

        code = ("import sys, unittest\n"
                "sys.modules['mcp'] = None  # as if the group were not installed\n"
                "suite = unittest.defaultTestLoader.loadTestsFromName('mcp_server.tests')\n"
                "result = unittest.TextTestRunner(stream=open('/dev/null', 'w')).run(suite)\n"
                "print(result.testsRun, len(result.skipped), len(result.errors), len(result.failures))\n")
        done = subprocess.run([sys.executable, "-c", code], cwd=BASE, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        ran, skipped, errors, failures = map(int, done.stdout.split())
        self.assertEqual((errors, failures), (0, 0), done.stdout)
        self.assertGreater(skipped, 0)
        self.assertGreater(ran - skipped, 0)  # the HTTP client's tests still run


class AdminPathDeployTests(SimpleTestCase):
    """Production refuses to start without DJANGO_ADMIN_PATH, so every path to a production start sets it."""

    def test_dockerfile_collectstatic_gets_a_build_only_admin_path_that_passes_the_check(self):
        from config.admin_path import check_admin_path

        run = re.search(r"(?ms)^RUN (SECRET_KEY=.*?collectstatic --noinput)", (BASE / "Dockerfile").read_text()).group(1)
        value = re.search(r"DJANGO_ADMIN_PATH=(\S+)", run).group(1)
        check_admin_path(value, ("healthz", "theme", "unlock"))

    def test_env_example_and_compose_readme_name_it(self):
        env = (BASE / ".env.example").read_text()
        self.assertRegex(env, r"(?m)^DJANGO_ADMIN_PATH=\S+/$")
        compose = (BASE / "docs/operations.md").read_text().split("## Docker Compose")[1].split("\n## ")[0]
        self.assertIn("DJANGO_ADMIN_PATH", compose)

    def test_the_placeholder_in_the_examples_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured

        from config.admin_path import check_admin_path

        for example in (".env.example", "deploy/env.example"):
            value = re.search(r"(?m)^DJANGO_ADMIN_PATH=(\S+)$", (BASE / example).read_text()).group(1)
            with self.assertRaises(ImproperlyConfigured, msg=example):
                check_admin_path(value, ("healthz",))
