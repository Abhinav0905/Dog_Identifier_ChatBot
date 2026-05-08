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
echo "App should be reachable on http://<your-ec2-public-ip>:${HOST_PORT}"
