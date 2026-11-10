FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    fonts-noto-core fonts-noto-cjk \
    gnupg openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

# The volumes' mount points exist in the image, owned by the app user: a new named volume takes the
# owner of the directory it is mounted on (vault.json and uploaded logos are written as pay).
# The two tmpfs mounts (the key, the gpg keyrings) get uid/gid 10001 in docker-compose.yml.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin pay \
    && mkdir -p /app/vault /app/media /run/vault /run/gpg \
    && chown pay:pay /app /app/vault /app/media /run/vault /run/gpg \
    && chmod 0700 /app/vault /app/media /run/vault /run/gpg

WORKDIR /app
USER pay
COPY --chown=pay:pay pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --chown=pay:pay . .

ENV PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PYTHONUNBUFFERED=1

# Static files are collected at build time, with throw-away values that exist only for this step.
RUN SECRET_KEY=build-only ALLOWED_HOSTS=build.invalid DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    SITE_URL=https://build.invalid VAULT_KEY_PATH=/tmp/build-vault.key \
    DJANGO_ADMIN_PATH=build-only-admin-path-unused/ ALLOW_EMPTY_PROXY_SECRET=true \
    python manage.py collectstatic --noinput

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "30"]
