#!/usr/bin/env bash
# Deploy ContentOS to a Selectel VPS in one command.
#
#   ./deploy.sh                       # uses defaults below
#   SSH_HOST=root@1.2.3.4 ./deploy.sh
#   DOMAIN=contentos.example.com ACME_EMAIL=me@x ./deploy.sh
#
# What happens:
#   1. SSH-bootstrap the server (Docker install) if needed.
#   2. Sync this repo's code to /opt/contentos/backend.
#   3. Sync the frontend repo to /opt/contentos/frontend.
#   4. Sync compose.prod.yml + Caddyfile to /opt/contentos.
#   5. Generate /opt/contentos/.env on first run with random secrets.
#      Subsequent runs preserve existing secrets, only patching missing keys.
#   6. docker compose pull + build + up -d.
#
# Re-running this script is the deploy mechanism. To roll back, git-checkout
# a previous commit locally and re-run.
set -euo pipefail

SSH_HOST="${SSH_HOST:-root@45.92.176.137}"
REMOTE_DIR="${REMOTE_DIR:-/opt/contentos}"
DOMAIN="${DOMAIN:-_}"
ACME_EMAIL="${ACME_EMAIL:-}"

# Resolve script location → infer project layout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_DIR="$(cd "${BACKEND_DIR}/../.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-${WORKSPACE_DIR}/tools/content-os/frontend}"

if [ ! -d "${FRONTEND_DIR}" ]; then
  echo "❌  Frontend not found at ${FRONTEND_DIR}. Set FRONTEND_DIR=…"
  exit 1
fi

echo "→ Target: ${SSH_HOST}:${REMOTE_DIR}"
echo "→ Backend:  ${BACKEND_DIR}"
echo "→ Frontend: ${FRONTEND_DIR}"
echo

# ---- 1. Bootstrap (idempotent) ----
echo "→ Ensuring Docker is installed on the VPS..."
ssh "${SSH_HOST}" 'mkdir -p /opt/contentos'
scp -q "${SCRIPT_DIR}/bootstrap-server.sh" "${SSH_HOST}:/opt/contentos/bootstrap-server.sh"
ssh "${SSH_HOST}" 'bash /opt/contentos/bootstrap-server.sh'

# ---- 2 + 3. Sync code ----
echo "→ Syncing backend..."
rsync -az --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
  --exclude='node_modules' --exclude='.next' --exclude='deploy/.env*' \
  "${BACKEND_DIR}/" "${SSH_HOST}:${REMOTE_DIR}/backend/"

echo "→ Syncing frontend..."
rsync -az --delete \
  --exclude='.git' --exclude='node_modules' --exclude='.next' \
  --exclude='.env*' \
  "${FRONTEND_DIR}/" "${SSH_HOST}:${REMOTE_DIR}/frontend/"

# ---- 4. Compose files ----
echo "→ Syncing compose + Caddyfile..."
scp -q "${SCRIPT_DIR}/compose.prod.yml" "${SSH_HOST}:${REMOTE_DIR}/compose.prod.yml"
ssh "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/caddy"
scp -q "${SCRIPT_DIR}/Caddyfile" "${SSH_HOST}:${REMOTE_DIR}/caddy/Caddyfile"
scp -q "${SCRIPT_DIR}/.env.prod.example" "${SSH_HOST}:${REMOTE_DIR}/.env.example"

# ---- 5. Secrets ----
echo "→ Reconciling .env (generates secrets on first run)..."
ssh "${SSH_HOST}" DOMAIN="${DOMAIN}" ACME_EMAIL="${ACME_EMAIL}" \
  REMOTE_DIR="${REMOTE_DIR}" 'bash -s' <<'REMOTE_SH'
set -euo pipefail
cd "${REMOTE_DIR}"

if [ ! -f .env ]; then
  cp .env.example .env

  # Generate strong secrets we always need.
  JWT=$(docker run --rm python:3.11-slim python -c \
    "import secrets; print(secrets.token_urlsafe(64))")
  ENC=$(docker run --rm python:3.11-slim sh -c \
    "pip install --quiet cryptography==43.0.1 && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
  PG=$(docker run --rm python:3.11-slim python -c \
    "import secrets; print(secrets.token_urlsafe(32))")

  sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${JWT}|" .env
  sed -i "s|^SECRETS_ENCRYPTION_KEY=.*|SECRETS_ENCRYPTION_KEY=${ENC}|" .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG}|" .env

  echo "  ✓ Generated JWT_SECRET / SECRETS_ENCRYPTION_KEY / POSTGRES_PASSWORD"
fi

# Apply DOMAIN/ACME_EMAIL/PUBLIC_URL_* on every deploy (these may change).
if [ -n "${DOMAIN:-}" ]; then
  sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env
  if [ "${DOMAIN}" = "_" ]; then
    # Bootstrap mode: caller hits https://<vps-ip>.
    IP=$(curl -s ifconfig.me || echo localhost)
    sed -i "s|^PUBLIC_URL_API=.*|PUBLIC_URL_API=https://${IP}/api|" .env
    sed -i "s|^PUBLIC_URL_FRONT=.*|PUBLIC_URL_FRONT=https://${IP}|" .env
  else
    sed -i "s|^PUBLIC_URL_API=.*|PUBLIC_URL_API=https://api.${DOMAIN}|" .env
    sed -i "s|^PUBLIC_URL_FRONT=.*|PUBLIC_URL_FRONT=https://${DOMAIN}|" .env
  fi
fi
if [ -n "${ACME_EMAIL:-}" ]; then
  sed -i "s|^ACME_EMAIL=.*|ACME_EMAIL=${ACME_EMAIL}|" .env
fi

echo "  ✓ .env reconciled"
REMOTE_SH

# ---- 6. Docker compose ----
echo "→ docker compose up..."
ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && \
  docker compose -f compose.prod.yml --env-file .env build && \
  docker compose -f compose.prod.yml --env-file .env up -d && \
  docker compose -f compose.prod.yml ps"

echo
echo "✓ Deploy complete."
echo "→ Frontend: https://${DOMAIN}"
echo "→ API:      https://api.${DOMAIN}/health"
echo
echo "Next steps if you haven't yet:"
echo "  • Edit /opt/contentos/.env on the VPS to set COMETAPI_KEY and TELEGRAM_BOT_TOKEN"
echo "    then re-run: ssh ${SSH_HOST} 'cd ${REMOTE_DIR} && docker compose -f compose.prod.yml restart api worker'"
