#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# hugecloud ni VPS'da systemd servisi sifatida o'rnatish.
#
#   sudo bash deploy/systemd-install.sh [/root/big-cloud]
#
# Skript: papkani aniqlaydi, venv/bog'liqliklarni o'rnatadi, migratsiyani
# bajaradi, unit faylni /etc/systemd/system'ga yozadi va servisni yoqadi.
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${1:-/root/big-cloud}"
SERVICE_NAME="hugecloud"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

echo "==> Loyiha papkasi: ${APP_DIR}"
if [[ ! -f "${APP_DIR}/main.py" ]]; then
  echo "XATO: ${APP_DIR}/main.py topilmadi. To'g'ri papkani argument qilib bering." >&2
  exit 1
fi

echo "==> Bog'liqliklar o'rnatilmoqda"
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "==> .env topilmadi — .env.example nusxalanmoqda (keyin to'ldiring!)"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
fi

echo "==> Sxema migratsiyasi (ma'lumot o'chmaydi)"
cd "${APP_DIR}"
"${PYTHON_BIN}" scripts/migrate.py --backup || echo "Ogohlantirish: migratsiya bajarilmadi (startup avtomatik qayta urinadi)"

echo "==> Unit fayl yozilmoqda: ${UNIT_PATH}"
sed -e "s#WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#" \
    -e "s#EnvironmentFile=.*#EnvironmentFile=${APP_DIR}/.env#" \
    -e "s#ExecStart=.*#ExecStart=${PYTHON_BIN} ${APP_DIR}/main.py --mode all#" \
    "${APP_DIR}/deploy/hugecloud.service" > "${UNIT_PATH}"

echo "==> Servis yoqilmoqda va ishga tushirilmoqda"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

sleep 3
echo "==> Holat:"
systemctl --no-pager --full status "${SERVICE_NAME}" | head -n 12 || true
echo
echo "Loglarni kuzatish:  journalctl -u ${SERVICE_NAME} -n 50 -f"
