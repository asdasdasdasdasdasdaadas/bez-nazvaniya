#!/usr/bin/env bash
# Ставит зависимости и запускает бота.
# Если есть Docker — собирает контейнер. Иначе ставит Python-окружение в .venv.

set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
cd "$root"

env_file="$root/.env"
pid_file="$root/.bot.pid"
log_file="$root/bot.log"

say() {
  printf '%s\n' "$*"
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

read_env() {
  local key="$1"
  local line
  [[ -f "$env_file" ]] || return 0
  line="$(grep -E "^${key}=" "$env_file" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

upsert_env() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$env_file" ]]; then
    grep -v -E "^${key}=" "$env_file" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$env_file"
}

ask() {
  local prompt="$1"
  local secret="${2:-0}"
  local value=""
  if [[ ! -t 0 ]]; then
    die "Заполните BOT_TOKEN и OWNER_ID в .env и запустите скрипт снова."
  fi
  if [[ "$secret" == "1" ]]; then
    read -r -s -p "$prompt" value
    printf '\n'
  else
    read -r -p "$prompt" value
  fi
  printf '%s' "$value"
}

have_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

prepare_env() {
  local token owner
  if [[ ! -f "$env_file" && -f "$root/.env.example" ]]; then
    cp "$root/.env.example" "$env_file"
  fi

  token="$(read_env BOT_TOKEN)"
  owner="$(read_env OWNER_ID)"

  if [[ -z "$token" ]]; then
    token="$(ask "Токен бота из @BotFather: " 1)"
  fi
  if [[ -z "$owner" ]]; then
    owner="$(ask "Ваш числовой Telegram ID (его пишет @userinfobot): " 0)"
  fi

  [[ -n "$token" && "$token" == *:* ]] || die "Токен должен быть вида 123456:ABC..."
  [[ "$owner" =~ ^[0-9]+$ ]] || die "OWNER_ID должен состоять только из цифр."

  upsert_env BOT_TOKEN "$token"
  upsert_env OWNER_ID "$owner"
  if [[ -z "$(read_env DATABASE_PATH)" ]]; then
    upsert_env DATABASE_PATH "bot.db"
  fi
  chmod 600 "$env_file" || true
}

start_docker() {
  docker info >/dev/null 2>&1 || die "Docker не запущен. Откройте Docker Desktop и запустите скрипт снова."
  say "Собираю и запускаю контейнер..."
  compose up -d --build
  say "Бот запущен."
  say "Логи: docker compose logs -f"
  say "Остановка: docker compose down"
}

start_python() {
  local python
  command -v python3 >/dev/null 2>&1 || die "Нет ни Docker, ни python3. Установите одно из двух."
  python="$(command -v python3)"
  "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Нужен Python 3.9 или новее."

  say "Docker не найден. Ставлю окружение Python..."
  if [[ ! -d "$root/.venv" ]]; then
    "$python" -m venv "$root/.venv"
  fi
  "$root/.venv/bin/pip" install --upgrade pip
  "$root/.venv/bin/pip" install -r "$root/requirements.txt"

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file" || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid" || true
      sleep 1
    fi
  fi

  nohup "$root/.venv/bin/python" -m bot >>"$log_file" 2>&1 &
  printf '%s\n' "$!" >"$pid_file"
  sleep 1
  if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    die "Бот не остался запущенным. Смотрите $log_file"
  fi
  say "Бот запущен, pid $(cat "$pid_file")."
  say "Логи: tail -f bot.log"
  say "Остановка: kill \$(cat .bot.pid)"
}

prepare_env

if have_docker; then
  start_docker
else
  start_python
fi
