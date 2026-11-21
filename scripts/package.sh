#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Loyihani tarqatish arxiviga yig'ish (.zip va .tar.gz).
# Kechirilmaydigan fayllar: .env, __pycache__, .git, *.sqlite3, venv.
#
#   bash scripts/package.sh [chiqish papkasi]
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$(dirname "${ROOT}")}"
STAMP="$(date +%Y%m%d)"
NAME="hugecloud-${STAMP}"

cd "${ROOT}"
STAGING="$(mktemp -d)/${NAME}"
mkdir -p "${STAGING}"

# rsync bo'lmasa ham ishlashi uchun tar+find kombinatsiyasi
tar -cf - \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='./.venv' \
  --exclude='./venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='*.sqlite3' \
  --exclude='*.sqlite3-*' \
  --exclude='./data' \
  . | (cd "${STAGING}" && tar -xf -)

mkdir -p "${OUT_DIR}"
ZIP_PATH="${OUT_DIR}/${NAME}.zip"
TAR_PATH="${OUT_DIR}/${NAME}.tar.gz"

( cd "$(dirname "${STAGING}")" && zip -qr "${ZIP_PATH}" "${NAME}" ) || {
  echo "zip topilmadi — faqat .tar.gz yaratiladi"; ZIP_PATH="";
}
( cd "$(dirname "${STAGING}")" && tar -czf "${TAR_PATH}" "${NAME}" )

echo "Tayyor:"
[[ -n "${ZIP_PATH}" ]] && echo "  ${ZIP_PATH} ($(du -h "${ZIP_PATH}" | cut -f1))"
echo "  ${TAR_PATH} ($(du -h "${TAR_PATH}" | cut -f1))"
