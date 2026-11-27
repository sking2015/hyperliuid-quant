#!/usr/bin/env bash
set -eu

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null 2>&1; then
    echo 'Install Docker Engine and the Docker Compose v2 plugin first.' >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo 'Docker Compose v2 is required (docker compose version).' >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo 'Docker is unavailable. Check the Docker service and this account’s Docker socket permissions.' >&2
    docker info >/dev/null
    exit 1
fi
if [ "$(docker info --format '{{.OSType}}')" != linux ]; then
    echo 'This project requires a Linux Docker engine.' >&2
    exit 1
fi

service_failed() {
    docker compose ps -a || true
    echo 'For details, run: docker compose logs --tail 80' >&2
    exit 1
}

echo 'Building and starting HL Quant Lab...'
docker compose up --build --detach --wait --wait-timeout 120 || service_failed

for ((attempt = 1; attempt <= 30; attempt++)); do
    if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://frontend/api/v1/health', timeout=2).close()" >/dev/null 2>&1; then
        printf '%s\n' 'Frontend: http://127.0.0.1:5173' 'API docs: http://127.0.0.1:8000/docs'
        exit 0
    fi
    sleep 1
done

echo 'The frontend could not reach the API health endpoint.' >&2
service_failed
