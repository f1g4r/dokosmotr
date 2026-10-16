#!/usr/bin/env bash
# Run from a reviewed Git checkout. Never deletes existing data or directories.
set -euo pipefail
cd "$(dirname "$0")"
mode=preserve
case "${1:-}" in
  --lan) mode=lan ;;
  --local) mode=local ;;
  --help|-h) echo 'Usage: ./install.sh [--lan|--local]'; echo 'New installs are accessible on the LAN; --lan migrates an existing loopback-only configuration.'; exit 0 ;;
  '') ;;
  *) echo "Неизвестный параметр: $1" >&2; exit 1 ;;
esac
[[ $# -le 1 ]] || { echo 'Передай только один режим: --lan или --local.' >&2; exit 1; }
command -v docker >/dev/null || { echo 'Установи Docker Engine и Docker Compose v2.' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
set_env() {
  local key="$1" value="$2" temporary
  temporary=$(mktemp .env.XXXXXX)
  awk -v key="$key" -v value="$value" '
    $0 ~ "^" key "=" { if (!found) print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' .env > "$temporary"
  chmod 600 "$temporary"
  mv "$temporary" .env
}
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  if [[ "$(uname -s)" == Linux ]]; then
    socket_gid=$(stat -c '%g' /var/run/docker.sock)
    set_env DOCKER_GID "$socket_gid"
  fi
fi
if [[ "$mode" == lan ]]; then
  set_env BIND_ADDRESS 0.0.0.0
  set_env PUBLIC_ORIGIN ''
  set_env COOKIE_SECURE false
elif [[ "$mode" == local ]]; then
  set_env BIND_ADDRESS 127.0.0.1
  set_env PUBLIC_ORIGIN ''
  set_env COOKIE_SECURE false
fi
compose=(docker compose -p dokosmotr -f docker-compose.yml)
if [[ "$(uname -s)" == Linux ]]; then compose+=(-f compose.linux.yml); fi
"${compose[@]}" config --quiet
"${compose[@]}" build
if ! "${compose[@]}" run --rm --no-deps docker-dashboard python -c 'import os, pathlib, sys; sys.exit(not pathlib.Path(os.environ["DATA_DIR"], "users.json").is_file())'; then
  "${compose[@]}" run --rm --no-deps docker-dashboard python app.py user --username admin --role admin
fi
"${compose[@]}" up -d --wait --wait-timeout 120
# Report the effective published port; don't execute .env as shell code.
published=$("${compose[@]}" port docker-dashboard 5000 | head -n 1)
port="${published##*:}"
printf '\nДокосмотр запущен. Порт: %s\n' "$published"
if [[ "$published" == 127.0.0.1:* || "$published" == '[::1]:'* ]]; then
  printf 'Локальный доступ: http://localhost:%s\n' "$port"
  printf 'Для доступа с другого ПК: ./install.sh --lan\n'
else
  printf 'Открой на другом ПК: http://IP-СЕРВЕРА:%s\n' "$port"
  addresses=''
  if [[ "$(uname -s)" == Linux ]]; then addresses=$(hostname -I 2>/dev/null || true)
  elif command -v ipconfig >/dev/null; then addresses=$(ipconfig getifaddr en0 2>/dev/null || true); fi
  for address in $addresses; do
    case "$address" in *:*) printf '  http://[%s]:%s\n' "$address" "$port" ;; *) printf '  http://%s:%s\n' "$address" "$port" ;; esac
  done
  printf 'Если настроен HTTPS-прокси, используй адрес PUBLIC_ORIGIN из .env.\n'
fi
