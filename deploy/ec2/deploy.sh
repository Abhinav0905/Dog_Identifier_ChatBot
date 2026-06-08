#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-gaia-chatbot}"
IMAGE_NAME="${IMAGE_NAME:-gaia-chatbot:latest}"
HOST_PORT="${HOST_PORT:-80}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
DATA_DIR="${DATA_DIR:-${REPO_ROOT}/.deploy-data}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Missing env file: ${ENV_FILE}" >&2
    exit 1
fi

mkdir -p "${DATA_DIR}/storage"

docker build -t "${IMAGE_NAME}" "${REPO_ROOT}"
docker rm -f "${APP_NAME}" >/dev/null 2>&1 || true

docker run -d \
    --name "${APP_NAME}" \
    --restart unless-stopped \
    --env-file "${ENV_FILE}" \
    -e DB_PATH=/app/data/dharmasala.db \
    -e STORAGE_DIR=/app/data/storage \
    -p "${HOST_PORT}:8000" \
    -v "${DATA_DIR}:/app/data" \
    "${IMAGE_NAME}"

docker ps --filter "name=${APP_NAME}"

if [[ "${HOST_PORT}" == "127.0.0.1:8000" ]] \
    && [[ -d /etc/nginx/default.d ]] \
    && [[ -w /etc/nginx/default.d ]]; then
    cat > /etc/nginx/default.d/gaia-chatbot.conf <<'EOF'
location / {
    client_max_body_size 110M;
    client_body_buffer_size 1M;
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
EOF
    nginx -t
    systemctl reload nginx
fi

echo "App should be reachable on http://<your-ec2-public-ip>:${HOST_PORT}"
