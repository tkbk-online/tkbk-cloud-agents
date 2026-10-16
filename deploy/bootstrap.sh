#!/usr/bin/env bash
# TKBK Cloud Agents — bootstrap of a fresh Ubuntu 24.04 VPS in one go.
#
#   curl -fsSL https://raw.githubusercontent.com/tkbk-online/tkbk-cloud-agents/main/deploy/bootstrap.sh | sudo bash
#   # or: sudo bash deploy/bootstrap.sh   (from a clone)
#
# What it does (idempotent):
#   1. apt: docker-ce + compose plugin, ufw, git, python3
#   2. ufw: allow 22, 80, 443; deny the rest (Docker publishes only Caddy's 80/443)
#   3. /opt/tkbk-agents: clone (or pull) the repo, create .env with a fresh AGENTS_INTERNAL_SECRET
#   4. pull the sandbox image, build the orchestrator, `docker compose up -d`
#   5. print what the owner must do by hand (DNS, LICENSE_PUBLIC_KEY, first task)
#
# Env overrides: AGENTS_DOMAIN (agents.tkbk.online), REPO_URL, INSTALL_DIR (/opt/tkbk-agents),
#                SANDBOX_IMAGE (ghcr.io/openhands/agent-server:1.55.0-python), WITH_STUB=1 (local LLM stub).
set -euo pipefail

AGENTS_DOMAIN="${AGENTS_DOMAIN:-agents.tkbk.online}"
REPO_URL="${REPO_URL:-https://github.com/tkbk-online/tkbk-cloud-agents.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tkbk-agents}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-ghcr.io/openhands/agent-server:1.55.0-python}"
WITH_STUB="${WITH_STUB:-0}"

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
need_root() { [ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }; }
need_root

log "1/5 packages: docker, ufw, git"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git ufw python3
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker --version && docker compose version
# More address space for per-task networks (one /24 per task) and json logs capped.
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "default-address-pools": [
    {"base": "172.17.0.0/12", "size": 24},
    {"base": "192.168.0.0/16", "size": 24}
  ],
  "log-driver": "json-file",
  "log-opts": {"max-size": "20m", "max-file": "3"}
}
EOF
  systemctl restart docker
fi

log "2/5 firewall: 22, 80, 443"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status | sed 's/^/   /'

log "3/5 files in $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
mkdir -p data
if [ ! -f .env ]; then
  cp .env.example .env
  secret="$(openssl rand -hex 32)"
  sed -i "s|^AGENTS_INTERNAL_SECRET=.*|AGENTS_INTERNAL_SECRET=${secret}|" .env
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://${AGENTS_DOMAIN}|" .env
  sed -i "s|^SANDBOX_IMAGE=.*|SANDBOX_IMAGE=${SANDBOX_IMAGE}|" .env
  echo "AGENTS_DOMAIN=${AGENTS_DOMAIN}" >> .env
  if [ "$WITH_STUB" = "1" ]; then
    sed -i "s|^LICENSE_MODE=.*|LICENSE_MODE=stub|; s|^TKBK_OPENAI_BASE_URL=.*|TKBK_OPENAI_BASE_URL=http://tkbk-llm-stub:8089/v1|; s|^DEFAULT_MODEL=.*|DEFAULT_MODEL=stub|; s|^TKBK_KEY_CHECK=.*|TKBK_KEY_CHECK=0|" .env
    echo "SANDBOX_ATTACH_CONTAINERS=tkbk-agents-egress,tkbk-agents-api,tkbk-llm-stub" >> .env
    echo "SANDBOX_NO_PROXY=localhost,127.0.0.1,tkbk-llm-stub" >> .env
  fi
  chmod 600 .env
  echo "   .env created (secret generated); edit it later if needed"
else
  echo "   .env exists, keeping it"
fi

log "4/5 images and containers"
docker pull "$SANDBOX_IMAGE"
profiles=()
[ "$WITH_STUB" = "1" ] && profiles=(--profile stub)
docker compose "${profiles[@]}" build
docker compose "${profiles[@]}" up -d
sleep 3
docker compose ps
docker exec tkbk-agents-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8090/health').read().decode())"

log "5/5 what is left for the owner"
cat <<EOF
   DNS:      A ${AGENTS_DOMAIN} -> $(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')   (Caddy issues the certificate on first request)
   Check:    curl -s https://${AGENTS_DOMAIN}/health
   Smoke:    cd ${INSTALL_DIR} && python3 scripts/smoke_sandbox.py          (sandbox + egress, no key)
   Licence:  .env -> LICENSE_PUBLIC_KEY=<public key from the site admin>   (signature check; optional)
   Secret:   .env -> AGENTS_INTERNAL_SECRET is the HMAC secret for the bot/site; the IDE uses Bearer cr_…
   First run: see README.md «Первая задача»
EOF
