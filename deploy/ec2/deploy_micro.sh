#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-gaia-chatbot}"
APP_USER="${APP_USER:-ec2-user}"
APP_DIR="${APP_DIR:-/home/${APP_USER}/gaia-chatbot}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"
DATA_DIR="${DATA_DIR:-${APP_DIR}/.deploy-data}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Missing env file: ${ENV_FILE}" >&2
    exit 1
fi

cd "${APP_DIR}"

mkdir -p "${DATA_DIR}/storage"
# The checked-in/local .env may contain blank runtime paths. On systemd those
# blanks can override the service values, so keep path ownership in the unit.
sed -i '/^DB_PATH=/d;/^STORAGE_DIR=/d;/^RAG_VECTOR_BACKEND=/d' "${ENV_FILE}"

if [[ ! -d "${VENV_DIR}" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r requirements.micro.txt

if [[ -f "${APP_DIR}/dharamsala.db" && ! -f "${DATA_DIR}/dharmasala.db" ]]; then
    cp "${APP_DIR}/dharamsala.db" "${DATA_DIR}/dharmasala.db"
fi
chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"

cat >/etc/systemd/system/${APP_NAME}.service <<EOF
[Unit]
Description=Ask Dorjee / Gaia Chatbot
After=network.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=DB_PATH=${DATA_DIR}/dharmasala.db
Environment=STORAGE_DIR=${DATA_DIR}/storage
Environment=RAG_VECTOR_BACKEND=sqlite
Environment=GUNICORN_WORKERS=1
ExecStart=${VENV_DIR}/bin/gunicorn -k uvicorn.workers.UvicornWorker -w 1 --timeout 180 --graceful-timeout 30 -b 127.0.0.1:8000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /etc/nginx/conf.d
cat >/etc/nginx/conf.d/${APP_NAME}.conf <<'EOF'
server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 110M;
    client_body_buffer_size 1M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

systemctl daemon-reload
systemctl enable "${APP_NAME}"
systemctl restart "${APP_NAME}"
systemctl enable nginx
nginx -t
systemctl restart nginx

systemctl --no-pager --full status "${APP_NAME}" | sed -n '1,18p'
echo "App should be reachable on http://<your-ec2-public-ip>/"
