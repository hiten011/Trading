#!/usr/bin/env bash
# First-time setup: create the config files and check the environment.
set -euo pipefail

cd "$(dirname "$0")/.."

info()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m!!\033[0m  %s\n' "$*"; }
ok()    { printf '\033[0;32mOK\033[0m  %s\n' "$*"; }

info "Checking Docker"
command -v docker >/dev/null 2>&1 || { warn "Docker is not installed - see https://docs.docker.com/get-docker/"; exit 1; }
docker info >/dev/null 2>&1 || { warn "Docker is installed but the daemon is not running. Start Docker Desktop and retry."; exit 1; }
ok "Docker is running"

if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "Created .env from .env.example"
else
  ok ".env already exists (left alone)"
fi

if [[ ! -f secrets/.env.dev ]]; then
  cp secrets/.env.dev.example secrets/.env.dev
  chmod 600 secrets/.env.dev
  ok "Created secrets/.env.dev - now fill in TOKEN and chat_idADMIN"
else
  ok "secrets/.env.dev already exists (left alone)"
fi

mkdir -p data/results
ok "Created data/results for the candle cache"

echo
info "Next steps"
cat <<'STEPS'
  1. Put your Telegram bot token and chat id in secrets/.env.dev
       TOKEN='<from @BotFather>'
       chat_idADMIN='<from @userinfobot>'
  2. Verify them:        make check-telegram
  3. Download candles:   make data
  4. Test your scan:     make dry-run
  5. Go live:            make up
STEPS
