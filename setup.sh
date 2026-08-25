#!/usr/bin/env bash
# One-command setup for memecountbot.
#   ./setup.sh            interactive: ask for keys, write .env, start the bot
#   ./setup.sh --local    run with a local Python venv instead of Docker
set -euo pipefail

cd "$(dirname "$0")"

BOLD=$(tput bold 2>/dev/null || true); DIM=$(tput dim 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true); GREEN=$(tput setaf 2 2>/dev/null || true)
YEL=$(tput setaf 3 2>/dev/null || true); OFF=$(tput sgr0 2>/dev/null || true)

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$OFF" "$*"; }
die()  { printf '%s✗ %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

MODE="docker"
[[ "${1:-}" == "--local" ]] && MODE="local"

say ""
say "${BOLD}memecountbot setup${OFF}"
say "${DIM}counts memes per person per day and calls out anyone over the limit${OFF}"
say ""

# ---------------------------------------------------------------- credentials
if [[ -f .env ]]; then
  ok ".env already exists — keeping it (delete it to start over)"
else
  say "Two things are needed. Both are free to obtain:"
  say "  1. A Telegram bot token  ${DIM}https://t.me/BotFather -> /newbot${OFF}"
  say "  2. An OpenRouter API key ${DIM}https://openrouter.ai/keys${OFF}"
  say ""

  BOT_TOKEN="${BOT_TOKEN:-}"
  while [[ -z "$BOT_TOKEN" ]]; do
    read -r -p "Telegram bot token: " BOT_TOKEN
  done

  OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
  while [[ -z "$OPENROUTER_API_KEY" ]]; do
    read -r -p "OpenRouter API key: " OPENROUTER_API_KEY
  done

  read -r -p "Memes allowed per person per day [5]: " DAILY_LIMIT
  DAILY_LIMIT="${DAILY_LIMIT:-5}"

  read -r -p "Timezone for the daily reset [Europe/Moscow]: " TIMEZONE
  TIMEZONE="${TIMEZONE:-Europe/Moscow}"

  cp .env.example .env
  # Replace only the first occurrence of each key, leaving the comments intact.
  python3 - "$BOT_TOKEN" "$OPENROUTER_API_KEY" "$DAILY_LIMIT" "$TIMEZONE" <<'PY'
import sys, pathlib
token, key, limit, tz = sys.argv[1:5]
values = {"BOT_TOKEN": token, "OPENROUTER_API_KEY": key,
          "DAILY_LIMIT": limit, "TIMEZONE": tz}
path = pathlib.Path(".env")
out = []
for line in path.read_text(encoding="utf-8").splitlines():
    name = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
    if name in values:
        out.append(f"{name}={values.pop(name)}")
    else:
        out.append(line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  chmod 600 .env
  ok "wrote .env"
fi

# ------------------------------------------------------------------- warning asset
shopt -s nullglob
ASSETS=(assets/warning.*)
shopt -u nullglob
if [[ ${#ASSETS[@]} -eq 0 ]]; then
  warn "no assets/warning.* found — the bot will reply with WARNING_TEXT instead."
  say "  ${DIM}Drop your screenshot at assets/warning.jpg to have it replied with.${OFF}"
else
  ok "warning asset: ${ASSETS[0]}"
fi

# --------------------------------------------------------------------- launch
if [[ "$MODE" == "docker" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    die "docker not found. Install Docker, or re-run with: ./setup.sh --local"
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "docker compose not found. Install the Compose plugin, or use: ./setup.sh --local"
  fi

  say ""
  say "Building and starting…"
  "${COMPOSE[@]}" up -d --build
  ok "bot is running"
  say ""
  say "  logs:    ${BOLD}${COMPOSE[*]} logs -f${OFF}"
  say "  stop:    ${BOLD}${COMPOSE[*]} down${OFF}"
  say "  restart: ${BOLD}${COMPOSE[*]} restart${OFF}"
else
  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  [[ -d .venv ]] || python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  ok "dependencies installed"
  say ""
  say "Starting the bot (Ctrl-C to stop)…"
  exec ./.venv/bin/python -m bot.main
fi

say ""
say "${BOLD}Two things left to do in Telegram:${OFF}"
say "  1. ${BOLD}Disable privacy mode${OFF} so the bot can see normal messages:"
say "     @BotFather -> /setprivacy -> pick your bot -> ${BOLD}Disable${OFF}"
say "  2. Add the bot to your group ${DIM}(re-add it if it was already there)${OFF}"
say ""
