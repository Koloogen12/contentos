#!/usr/bin/env bash
# Run ONCE on a fresh Ubuntu/Debian VPS (root). Installs Docker + Compose
# and creates /opt/contentos. Idempotent — safe to re-run.
set -euo pipefail

apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl gnupg git

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  . /etc/os-release
  DOCKER_OS="${ID}"
  if [ "${DOCKER_OS}" = "ubuntu" ]; then
    REPO_URL="https://download.docker.com/linux/ubuntu"
  else
    REPO_URL="https://download.docker.com/linux/debian"
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${REPO_URL} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

mkdir -p /opt/contentos /opt/contentos/caddy

echo "Server bootstrap complete."
docker --version
docker compose version
