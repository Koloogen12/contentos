#!/usr/bin/env bash
# Idempotent server-side init for ContentOS.
#
# Runs as root on the existing Selectel VPS that already hosts mml /
# ohmybet / neurin / etc. We DO NOT touch the existing Postgres/Redis/
# nginx — we add ourselves to them:
#   - install pgvector + pgcrypto on the native PG16
#   - create role `contentos` + DB `contentos` + extensions
#   - reuse existing Redis with master requirepass on DB index 5
#   - create OS user `contentos` and /opt/contentos/{backend,frontend,files}
#   - generate /etc/contentos/secrets.env (mode 600)
#
# Idempotent: re-running is safe. Existing values in secrets.env are kept;
# only missing keys are filled with fresh randoms.
set -euo pipefail

OS_USER=contentos
APP_ROOT=/opt/contentos
SECRETS_FILE=/etc/contentos/secrets.env
PG_DB=contentos
PG_USER=contentos
REDIS_DB_INDEX=5

echo "→ 1/6  Install pgvector for Postgres 16…"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-16-pgvector

echo "→ 2/6  Create OS user $OS_USER (if missing)…"
if ! id "$OS_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$OS_USER"
fi

mkdir -p "$APP_ROOT"/{backend,frontend,files,deploy}
chown -R "$OS_USER:$OS_USER" "$APP_ROOT"

mkdir -p /etc/contentos
chmod 0750 /etc/contentos

echo "→ 3/6  Reconcile secrets at $SECRETS_FILE…"
if [ ! -f "$SECRETS_FILE" ]; then
  install -m 600 /dev/null "$SECRETS_FILE"
fi
chmod 600 "$SECRETS_FILE"
chown root:root "$SECRETS_FILE"

# Helper: ensure key exists in secrets file with a value (idempotent).
set_if_absent() {
  local key="$1"; local value="$2"
  if grep -q "^${key}=" "$SECRETS_FILE"; then return 0; fi
  printf '%s=%s\n' "$key" "$value" >> "$SECRETS_FILE"
}

# Generate strong randoms via Python so we don't depend on openssl flags.
gen_token() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
gen_password() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
gen_fernet() {
  python3 - <<'PY'
import os, base64
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
}

set_if_absent JWT_SECRET                  "$(gen_token)"
set_if_absent SECRETS_ENCRYPTION_KEY      "$(gen_fernet)"
set_if_absent POSTGRES_PASSWORD           "$(gen_password)"
set_if_absent COMETAPI_KEY                ""
set_if_absent TELEGRAM_BOT_TOKEN          ""
set_if_absent S3_ENDPOINT_URL             ""
set_if_absent S3_ACCESS_KEY               ""
set_if_absent S3_SECRET_KEY               ""

# Read back what we have
PG_PASS="$(grep '^POSTGRES_PASSWORD=' "$SECRETS_FILE" | cut -d= -f2-)"
JWT="$(grep '^JWT_SECRET=' "$SECRETS_FILE" | cut -d= -f2-)"

# Capture redis master password from the existing ohmybet include
REDIS_MASTER="$(grep '^requirepass ' /etc/redis/redis.conf.d-ohmybet.conf 2>/dev/null | awk '{print $2}')"
if [ -z "$REDIS_MASTER" ]; then
  echo "!! Could not read Redis password from /etc/redis/redis.conf.d-ohmybet.conf — abort."
  exit 1
fi
set_if_absent REDIS_PASSWORD "$REDIS_MASTER"

echo "→ 4/6  Create Postgres role + DB + extensions…"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$do\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PG_USER') THEN
    CREATE ROLE $PG_USER LOGIN PASSWORD '$PG_PASS';
  ELSE
    ALTER ROLE $PG_USER PASSWORD '$PG_PASS';
  END IF;
END
\$do\$;

SELECT 'CREATE DATABASE $PG_DB OWNER $PG_USER ENCODING UTF8 LC_COLLATE ''C.UTF-8'' LC_CTYPE ''C.UTF-8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$PG_DB')
\\gexec
SQL

# Extensions live inside the new DB
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$PG_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO contentos;
SQL

echo "→ 5/6  Verify Postgres connectivity from host…"
PGPASSWORD="$PG_PASS" psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DB" -c "SELECT 1" >/dev/null
echo "   ✓ contentos@127.0.0.1:5432/$PG_DB OK"

echo "→ 6/6  Verify Redis connectivity (DB $REDIS_DB_INDEX)…"
redis-cli -a "$REDIS_MASTER" -n "$REDIS_DB_INDEX" PING >/dev/null
echo "   ✓ redis://...@127.0.0.1:6379/$REDIS_DB_INDEX OK"

cat <<EOF

============================================================
Server init complete.

OS user:     $OS_USER
App root:    $APP_ROOT
Secrets:     $SECRETS_FILE  (root, mode 600)
Postgres:    $PG_USER@127.0.0.1:5432/$PG_DB
Redis:       127.0.0.1:6379/$REDIS_DB_INDEX (master requirepass)

Generated (only if missing — re-runs preserve existing values):
  - JWT_SECRET, SECRETS_ENCRYPTION_KEY, POSTGRES_PASSWORD

Still empty (paste manually before bringing the stack up):
  - COMETAPI_KEY        (required)
  - TELEGRAM_BOT_TOKEN  (optional)
  - S3_*                (optional, falls back to local files)
============================================================
EOF
