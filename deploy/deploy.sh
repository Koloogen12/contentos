#!/usr/bin/env bash
# Deploy ContentOS to the existing Selectel `mml-prod-1` server.
# Re-running this script is the deploy mechanism (rsync + docker rebuild).
#
# Usage from a developer machine:
#   ./deploy.sh
#
# Prereqs (one-time, manual):
#   1. ssh-copy-id root@135.106.146.200    (Danil's id_ed25519 already on the box)
#   2. DNS A-records for draft.neurin.tech AND api.draft.neurin.tech → 135.106.146.200
#   3. server-init.sh has been run once on the server (creates DB, role, OS user, secrets)
#   4. /etc/contentos/secrets.env has COMETAPI_KEY filled in (and optional TG_BOT_TOKEN)
#
# What this script does on every run:
#   1. rsync backend + frontend code → /opt/contentos/{backend,frontend}
#   2. sync compose + nginx config + .env to /opt/contentos/deploy/
#   3. docker compose build + up -d (api/worker rebuild, frontend rebuild)
#   4. (first run) install nginx site, run certbot for SSL
set -euo pipefail

SSH_HOST="${SSH_HOST:-root@135.106.146.200}"
REMOTE_DIR="/opt/contentos"
DOMAIN="${DOMAIN:-draft.neurin.tech}"
ACME_EMAIL="${ACME_EMAIL:-leomih659@gmail.com}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_DIR="$(cd "${BACKEND_DIR}/../.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-${WORKSPACE_DIR}/tools/content-os/frontend}"

if [ ! -d "${FRONTEND_DIR}" ]; then
  echo "Frontend not found at ${FRONTEND_DIR}. Set FRONTEND_DIR=..."
  exit 1
fi

echo "Target: ${SSH_HOST}:${REMOTE_DIR}"
echo "Domain: ${DOMAIN}"
echo "Backend:  ${BACKEND_DIR}"
echo "Frontend: ${FRONTEND_DIR}"
echo

echo "1/6 Sanity check the server (must NOT touch mml-backend)..."
ssh "${SSH_HOST}" "systemctl is-active mml-backend || (echo 'mml-backend is not active - abort' && exit 1)"

echo "2/6 Sync deploy artefacts to ${REMOTE_DIR}/deploy/..."
ssh "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}/deploy"
scp -q "${SCRIPT_DIR}/server-init.sh" "${SCRIPT_DIR}/compose.prod.yml" \
    "${SCRIPT_DIR}/nginx-contentos.conf" "${SCRIPT_DIR}/.env.prod.example" \
    "${SSH_HOST}:${REMOTE_DIR}/deploy/"

# Ensure init has been run (creates DB, secrets, OS user). Idempotent.
echo "3/6 Run server-init (idempotent)..."
ssh "${SSH_HOST}" "bash ${REMOTE_DIR}/deploy/server-init.sh"

echo "4/6 Rsync backend + frontend code..."
RSYNC_EXCLUDES=(--exclude=node_modules --exclude=.next --exclude=__pycache__
                --exclude=.venv --exclude=.git --exclude='.DS_Store'
                --exclude='*.log' --exclude=deploy/.env)

rsync -azq --delete "${RSYNC_EXCLUDES[@]}" \
  "${BACKEND_DIR}/" "${SSH_HOST}:${REMOTE_DIR}/backend/"
rsync -azq --delete "${RSYNC_EXCLUDES[@]}" \
  "${FRONTEND_DIR}/" "${SSH_HOST}:${REMOTE_DIR}/frontend/"

ssh "${SSH_HOST}" "chown -R contentos:contentos ${REMOTE_DIR}/backend ${REMOTE_DIR}/frontend"

echo "5/6 Reconcile /opt/contentos/deploy/.env with chosen DOMAIN..."
ssh "${SSH_HOST}" DOMAIN="${DOMAIN}" ACME_EMAIL="${ACME_EMAIL}" 'bash -s' <<'REMOTE_SH'
set -euo pipefail
cd /opt/contentos/deploy
if [ ! -f .env ]; then
  cp .env.prod.example .env
fi
sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env
sed -i "s|^PUBLIC_URL_FRONT=.*|PUBLIC_URL_FRONT=https://${DOMAIN}|" .env
# Single-domain mode — same host for frontend AND /api/* (no api.* split).
sed -i "s|^PUBLIC_URL_API=.*|PUBLIC_URL_API=https://${DOMAIN}|" .env
sed -i "s|^ACME_EMAIL=.*|ACME_EMAIL=${ACME_EMAIL}|" .env
echo ".env reconciled (DOMAIN=${DOMAIN})"
REMOTE_SH

echo "6/6 docker compose build + up -d (api/worker/frontend)..."
ssh "${SSH_HOST}" 'bash -s' <<'REMOTE_COMPOSE'
set -euo pipefail
cd /opt/contentos/deploy
# Merge .env + secrets.env into one file docker compose can consume
# (docker compose v2 supports multiple --env-file but `bash -c` ssh
# escaping is fragile, so we just concatenate to a tmp file).
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
cat .env /etc/contentos/secrets.env > "$TMP_ENV"
docker compose -f compose.prod.yml --env-file "$TMP_ENV" build
docker compose -f compose.prod.yml --env-file "$TMP_ENV" up -d
docker compose -f compose.prod.yml --env-file "$TMP_ENV" ps
REMOTE_COMPOSE

echo
echo "Bringing nginx site online (idempotent)..."
ssh "${SSH_HOST}" DOMAIN="${DOMAIN}" 'bash -s' <<'REMOTE_NGINX'
set -euo pipefail
sed "s/__DOMAIN__/${DOMAIN}/g" /opt/contentos/deploy/nginx-contentos.conf \
    > /etc/nginx/sites-available/contentos
ln -sf /etc/nginx/sites-available/contentos /etc/nginx/sites-enabled/contentos
nginx -t
systemctl reload nginx
echo "nginx site contentos active"
REMOTE_NGINX

echo
echo "certbot for SSL (idempotent - skips if cert already valid)..."
ssh "${SSH_HOST}" DOMAIN="${DOMAIN}" ACME_EMAIL="${ACME_EMAIL}" 'bash -s' <<'REMOTE_CERTBOT'
set -euo pipefail
if certbot certificates 2>/dev/null | grep -q "Domains: ${DOMAIN}"; then
  echo "Cert already issued; skipping"
else
  certbot --nginx \
    -d "${DOMAIN}" \
    --email "${ACME_EMAIL}" --agree-tos --no-eff-email --redirect \
    --non-interactive
fi
REMOTE_CERTBOT

echo
echo "Deploy complete."
echo "App:    https://${DOMAIN}"
echo "API:    https://${DOMAIN}/api/v1/  (health: https://${DOMAIN}/health)"
echo "Logs:     ssh ${SSH_HOST} 'cd ${REMOTE_DIR}/deploy && docker compose logs -f api worker frontend'"
