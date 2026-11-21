#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# VPS uchun to'liq deploy: kodni yangilash -> migratsiya -> servisni qayta
# ishga tushirish -> logni tekshirish.
#
#   bash deploy/deploy.sh                 # joriy papkada
#   bash deploy/deploy.sh /root/big-cloud # aniq papkada
# ---------------------------------------------------------------------------
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE="${SERVICE:-hugecloud}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${APP_DIR}"
echo "==> ${APP_DIR}"

if [[ -d .git ]]; then
  echo "==> git pull"
  git pull --ff-only || echo "Ogohlantirish: git pull bajarilmadi"
fi

echo "==> Bog'liqliklar"
"${PYTHON_BIN}" -m pip install -q -r requirements.txt

echo "==> Migratsiya"
"${PYTHON_BIN}" scripts/migrate.py --backup

echo "==> Testlar (ixtiyoriy, SKIP_TESTS=1 bilan o'tkazib yuboriladi)"
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  "${PYTHON_BIN}" -m pytest -q || echo "Ogohlantirish: testlar yiqildi"
fi

if systemctl list-unit-files | grep -q "^${SERVICE}.service"; then
  echo "==> systemctl restart ${SERVICE}"
  systemctl restart "${SERVICE}"
  sleep 4
  echo "==> journalctl -u ${SERVICE} -n 50"
  journalctl -u "${SERVICE}" -n 50 --no-pager
else
  echo "systemd servisi topilmadi. O'rnatish: sudo bash deploy/systemd-install.sh ${APP_DIR}" >&2
  exit 1
fi
