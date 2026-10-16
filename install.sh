#!/usr/bin/env bash
# Run from a reviewed Git checkout. Never deletes existing data or directories.
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo 'Установи Docker Engine и Docker Compose v2.' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  if [[ "$(uname -s)" == Linux ]]; then
    socket_gid=$(stat -c '%g' /var/run/docker.sock)
    sed -i "s/^DOCKER_GID=.*/DOCKER_GID=$socket_gid/" .env
  fi
fi
compose=(docker compose -p dokosmotr -f docker-compose.yml)
if [[ "$(uname -s)" == Linux ]]; then compose+=(-f compose.linux.yml); fi
"${compose[@]}" config --quiet
"${compose[@]}" build
if ! "${compose[@]}" run --rm --no-deps docker-dashboard python -c 'import os, pathlib, sys; sys.exit(not pathlib.Path(os.environ["DATA_DIR"], "users.json").is_file())'; then
  "${compose[@]}" run --rm --no-deps docker-dashboard python app.py user --username admin --role admin
fi
"${compose[@]}" up -d --wait --wait-timeout 120
printf '\nДокосмотр запущен. Адрес указан в PUBLIC_ORIGIN файла .env.\n'
printf 'По умолчанию: http://localhost:5080 — пользователь admin.\n'
