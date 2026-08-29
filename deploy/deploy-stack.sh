#!/usr/bin/env bash
#
# Развернуть ContentOS на сервере, настроенном через tools/infra/bootstrap.sh.
#
#   ./deploy-stack.sh 167.233.109.195 draft.neurin.tech
#
# Отличия от прежнего deploy.sh, который работал только на Selectel:
#
# * нет проверки чужого systemd-сервиса `mml-backend` — она обрывала запуск
#   на любой другой машине;
# * нет nginx и certbot — TLS берёт Caddy, он же маршрутизирует по доменам;
# * Postgres и Redis поднимаются в стеке, а не ожидаются на хосте, и пароль
#   Redis больше не читается из конфига постороннего продукта.
#
# Повторный запуск — обычный способ выкатить обновление.
set -euo pipefail

HOST="${1:?укажи IP сервера}"
DOMAIN="${2:-draft.neurin.tech}"
SSH="root@${HOST}"
REMOTE=/opt/stacks/contentos

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$(cd "${BACKEND_DIR}/../content-os/frontend" && pwd)}"

echo "Сервер:   ${SSH}"
echo "Домен:    ${DOMAIN}"
echo "Бэкенд:   ${BACKEND_DIR}"
echo "Фронтенд: ${FRONTEND_DIR}"
echo

# ── 1. Секреты ───────────────────────────────────────────────────────────────
# Генерируются на сервере и переживают повторные запуски: перезаписать их
# значит разлогинить всех и потерять доступ к зашифрованным полям.
echo "1/5 Секреты"
ssh "$SSH" 'bash -s' <<'REMOTE_SECRETS'
set -euo pipefail
install -d -m 700 /etc/contentos
F=/etc/contentos/secrets.env
touch "$F"; chmod 600 "$F"

set_if_absent() {
  grep -q "^$1=" "$F" || echo "$1=$2" >> "$F"
}

set_if_absent JWT_SECRET              "$(openssl rand -hex 32)"
set_if_absent SECRETS_ENCRYPTION_KEY  "$(openssl rand -hex 32)"
set_if_absent POSTGRES_PASSWORD       "$(openssl rand -hex 24)"
set_if_absent REDIS_PASSWORD          "$(openssl rand -hex 24)"

# Внешние ключи. Заглушки, чтобы приложение поднялось: SDK Anthropic требует
# непустой api_key, но реле на ai.neurin.tech подставляет свой, поэтому
# значение здесь ни на что не влияет до перехода на прямые вызовы.
set_if_absent AI_PROVIDER             "anthropic"
set_if_absent ANTHROPIC_BASE_URL      "https://ai.neurin.tech"
set_if_absent ANTHROPIC_API_KEY       "placeholder-replace-me"
set_if_absent ANTHROPIC_PROXY_KEY     ""
set_if_absent COMETAPI_KEY            ""
set_if_absent TELEGRAM_BOT_TOKEN      ""

echo "   секреты на месте: $(grep -c '=' "$F") переменных"
REMOTE_SECRETS

# ── 2. Код ───────────────────────────────────────────────────────────────────
echo "2/5 Синхронизирую код"
ssh "$SSH" "mkdir -p ${REMOTE}"
RSYNC_EXCLUDES=(--exclude=node_modules --exclude=.next --exclude=__pycache__
                --exclude=.venv --exclude=.git --exclude='.DS_Store'
                --exclude='*.log' --exclude=deploy/.env)
rsync -azq --delete "${RSYNC_EXCLUDES[@]}" "${BACKEND_DIR}/"  "${SSH}:${REMOTE}/backend/"
rsync -azq --delete "${RSYNC_EXCLUDES[@]}" "${FRONTEND_DIR}/" "${SSH}:${REMOTE}/frontend/"
scp -q "${SCRIPT_DIR}/compose.stack.yml" "${SSH}:${REMOTE}/compose.yml"

# ── 3. Окружение стека ───────────────────────────────────────────────────────
echo "3/5 Окружение"
ssh "$SSH" "cat > ${REMOTE}/.env" <<EOF
DOMAIN=${DOMAIN}
PUBLIC_URL_FRONT=https://${DOMAIN}
PUBLIC_URL_API=https://${DOMAIN}
EOF

# ── 4. Сборка и запуск ───────────────────────────────────────────────────────
echo "4/5 Собираю и поднимаю"
ssh "$SSH" "bash -s" <<REMOTE_UP
set -euo pipefail
cd ${REMOTE}
TMP=\$(mktemp); trap 'rm -f "\$TMP"' EXIT
cat .env /etc/contentos/secrets.env > "\$TMP"
docker compose --env-file "\$TMP" build
docker compose --env-file "\$TMP" up -d
docker compose --env-file "\$TMP" ps
REMOTE_UP

# ── 5. Маршрут в Caddy ───────────────────────────────────────────────────────
# Один блок на продукт. Сертификат Let's Encrypt берётся автоматически, как
# только A-запись домена смотрит на этот сервер.
echo "5/5 Маршрут в Caddy"
ssh "$SSH" DOMAIN="$DOMAIN" 'bash -s' <<'REMOTE_CADDY'
set -euo pipefail
CF=/opt/caddy/Caddyfile
if ! grep -q "^${DOMAIN} {" "$CF" 2>/dev/null; then
  cat >> "$CF" <<EOF

${DOMAIN} {
    encode zstd gzip
    handle /api/* {
        reverse_proxy contentos-api-1:8000
    }
    handle /health {
        reverse_proxy contentos-api-1:8000
    }
    handle {
        reverse_proxy contentos-frontend-1:3000
    }
}
EOF
  echo "   блок ${DOMAIN} добавлен"
else
  echo "   блок ${DOMAIN} уже есть"
fi
docker exec caddy-caddy-1 caddy reload --config /etc/caddy/Caddyfile 2>&1 | tail -2 \
  || echo "   ВНИМАНИЕ: Caddy не перечитал конфиг"
REMOTE_CADDY

echo
echo "Готово. Проверка:"
echo "  ssh ${SSH} 'curl -s -o /dev/null -w \"%{http_code}\\n\" -H \"Host: ${DOMAIN}\" http://127.0.0.1/health'"
echo "Домен переключать только после того, как этот запрос вернёт 200."
