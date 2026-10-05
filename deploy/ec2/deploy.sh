#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_NAME="${APP_NAME:-gaia-chatbot}"
IMAGE_NAME="${IMAGE_NAME:-gaia-chatbot:latest}"
HOST_PORT="${HOST_PORT:-127.0.0.1:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
DATA_DIR="${DATA_DIR:-${REPO_ROOT}/.deploy-data}"
BACKUP_ROOT="${BACKUP_ROOT:-${REPO_ROOT}/.deploy-backups}"
STARTUP_TIMEOUT_SECONDS="${STARTUP_TIMEOUT_SECONDS:-180}"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
CANDIDATE="${APP_NAME}-candidate-${RELEASE_ID}"
PREVIOUS="${APP_NAME}-previous-${RELEASE_ID}"
BACKUP_DIR="${BACKUP_ROOT}/${RELEASE_ID}"
LOCK_DIR="${DATA_DIR}.deploy-lock"
OLD_EXISTS=0
OLD_RUNNING=0
OLD_STOPPED=0
OLD_RENAMED=0
SNAPSHOT_COMPLETE=0
CANDIDATE_CREATED=0
CANDIDATE_STARTED=0
COMMITTED=0

[[ -f "${ENV_FILE}" ]] || { echo "Missing environment file" >&2; exit 1; }
[[ "${STARTUP_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || { echo "Invalid startup timeout" >&2; exit 1; }
[[ "${DATA_DIR}" = /* && "${BACKUP_ROOT}" = /* ]] || { echo "Data and backup paths must be absolute" >&2; exit 1; }
[[ "${DATA_DIR}" != / && "${BACKUP_ROOT}" != / ]] || { echo "Use dedicated data and backup directories" >&2; exit 1; }
for seed_path in "${CHROMA_SEED_DIR:-}" "${EMBEDDING_CACHE_SEED_DIR:-}"; do
    [[ -z "${seed_path}" || -d "${seed_path}" ]] || { echo "Configured seed directory does not exist" >&2; exit 1; }
done
case "${BACKUP_ROOT}/" in "${DATA_DIR}/"*) echo "Backups must be outside the data directory" >&2; exit 1 ;; esac
mkdir -p "$(dirname "${DATA_DIR}")"
mkdir "${LOCK_DIR}" || { echo "Another deployment holds ${LOCK_DIR}; investigate before removing it" >&2; exit 1; }

cleanup() {
    local result=$?
    trap - EXIT INT TERM
    if [[ "${COMMITTED}" = 0 ]]; then
        echo "Deployment failed; restoring the previous application where available." >&2
        if [[ "${CANDIDATE_CREATED}" = 1 ]]; then
            docker rm -f "${CANDIDATE}" >/dev/null 2>&1 || true
        fi
        if [[ "${CANDIDATE_STARTED}" = 1 ]]; then
            docker rm -f "${APP_NAME}" >/dev/null 2>&1 || true
        fi
        if [[ "${SNAPSHOT_COMPLETE}" = 1 && "${CANDIDATE_STARTED}" = 1 ]]; then
            # Keep the failed state for diagnosis; never overwrite the snapshot.
            if mv "${DATA_DIR}" "${BACKUP_DIR}/failed-data" && cp -a "${BACKUP_DIR}/data" "${DATA_DIR}"; then
                echo "Restored quiesced data snapshot; failed data retained at ${BACKUP_DIR}/failed-data" >&2
            else
                echo "DATA RESTORE FAILED: do not start an application until ${BACKUP_DIR}/data is restored." >&2
                OLD_RUNNING=0
                result=1
            fi
        fi
        if [[ "${OLD_RENAMED}" = 1 ]]; then
            docker rename "${PREVIOUS}" "${APP_NAME}" || { OLD_RUNNING=0; result=1; }
        fi
        if [[ "${OLD_RUNNING}" = 1 && "${OLD_STOPPED}" = 1 ]]; then
            docker start "${APP_NAME}" >/dev/null || result=1
        fi
    fi
    rmdir "${LOCK_DIR}" 2>/dev/null || true
    exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "${DATA_DIR}/storage" "${BACKUP_DIR}"
if docker inspect "${APP_NAME}" >/dev/null 2>&1; then
    OLD_EXISTS=1
    [[ "$(docker inspect --format '{{.State.Running}}' "${APP_NAME}")" = true ]] && OLD_RUNNING=1
    docker inspect --format '{{.Image}}' "${APP_NAME}" > "${BACKUP_DIR}/previous-image.txt"
fi

# Build and validate container configuration while the existing process serves.
docker build -t "${IMAGE_NAME}" "${REPO_ROOT}"
docker image inspect --format '{{.Id}}' "${IMAGE_NAME}" > "${BACKUP_DIR}/candidate-image.txt"
docker create --name "${CANDIDATE}" --restart unless-stopped --env-file "${ENV_FILE}" \
    -e DB_PATH=/app/data/dharmasala.db -e STORAGE_DIR=/app/data/storage \
    -e CHROMA_PERSIST_DIR=/app/data/chroma_db -e HF_HUB_CACHE=/app/data/model-cache/hub \
    -p "${HOST_PORT}:8000" -v "${DATA_DIR}:/app/data" "${IMAGE_NAME}" >/dev/null
CANDIDATE_CREATED=1
if [[ "${OLD_EXISTS}" = 1 ]]; then
    docker stop --time 45 "${APP_NAME}" >/dev/null
    OLD_STOPPED=1
fi
# Deployment has a maintenance window. All writers, including ingestion jobs,
# must be stopped; SQLite, original images and vectors are copied together.
cp -a "${DATA_DIR}" "${BACKUP_DIR}/data"
SNAPSHOT_COMPLETE=1

seed_from_previous() {
    local old_path="$1" destination="$2" explicit_source="$3"
    [[ ! -e "${destination}" ]] || return 0
    local temp_dir="${destination}.seed-${RELEASE_ID}"
    mkdir -p "$(dirname "${destination}")"
    if [[ -n "${explicit_source}" && -d "${explicit_source}" ]]; then
        cp -a "${explicit_source}" "${temp_dir}"
    elif [[ "${OLD_EXISTS}" = 1 ]] && docker cp "${APP_NAME}:${old_path}" "${temp_dir}" >/dev/null 2>&1; then
        :
    else
        # docker cp can leave partial output on failure.
        [[ ! -e "${temp_dir}" ]] || mv "${temp_dir}" "${BACKUP_DIR}/incomplete-seed-$(basename "${destination}")"
        return 0
    fi
    mv "${temp_dir}" "${destination}"
}
# Explicit sources take precedence. Existing persistent directories are untouched.
seed_from_previous /app/chroma_db "${DATA_DIR}/chroma_db" "${CHROMA_SEED_DIR:-}"
if [[ ! -e "${DATA_DIR}/chroma_db" && -d "${REPO_ROOT}/chroma_db" ]]; then
    cp -a "${REPO_ROOT}/chroma_db" "${DATA_DIR}/chroma_db"
fi
seed_from_previous /root/.cache/huggingface/hub "${DATA_DIR}/model-cache/hub" "${EMBEDDING_CACHE_SEED_DIR:-}"
mkdir -p "${DATA_DIR}/chroma_db" "${DATA_DIR}/model-cache/hub"

if [[ "${OLD_EXISTS}" = 1 ]]; then
    docker rename "${APP_NAME}" "${PREVIOUS}"
    OLD_RENAMED=1
fi
docker rename "${CANDIDATE}" "${APP_NAME}"
CANDIDATE_CREATED=0
CANDIDATE_STARTED=1
docker start "${APP_NAME}" >/dev/null

# This probe performs no paid model calls. Full response and browser acceptance
# remain release gates; models may correctly be not_observed after a restart.
DEADLINE=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
READY=0
while (( SECONDS < DEADLINE )); do
    if docker exec "${APP_NAME}" python -c '
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=8) as response:
    state = json.load(response)
knowledge = state.get("knowledge", {})
backend = knowledge.get("backend")
knowledge_ok = knowledge.get("status") in {"ready", "recent_success"}
# Hosted retrieval cannot be certified by a free local probe; require the
# explicit live acceptance gate separately for a configured hosted backend.
if backend == "pinecone":
    knowledge_ok = knowledge.get("status") in {"not_observed", "recent_success"}
ok = (state.get("database") == "ready" and knowledge_ok
      and state.get("models") in {"not_observed", "recent_success"}
      and state.get("india_only_scope") is True
      and state.get("india_boundary_loaded") is True)
sys.exit(0 if ok else 1)
' >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 3
done
[[ "${READY}" = 1 ]] || { echo "Local readiness failed; automatic rollback follows. Inspect container logs before retrying." >&2; exit 1; }
COMMITTED=1
echo "Candidate started; previous container (if present): ${PREVIOUS}"
echo "Private rollback snapshot: ${BACKUP_DIR}"
echo "Backend is bound to ${HOST_PORT}. Share only the configured HTTPS domain."
echo "Complete WEB_RELEASE_CHECKLIST.md before opening public traffic; local readiness is not live model, browser, or receiver acceptance."
