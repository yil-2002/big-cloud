# ---------------------------------------------------------------------------
# hugecloud — Telegram Cloud Bot + FastAPI + Mini App
#
# Bitta obraz ikki rejimda ishlaydi:
#   * web    (sukut) : HTTP port + bot polling bitta jarayonda  -> `python main.py`
#   * worker         : faqat bot polling                        -> `python main.py --mode bot`
#
# Fayllar hech qachon obraz ichida saqlanmaydi: media Telegram serverlarida,
# metadata esa DATABASE_URL ko'rsatgan bazada (PostgreSQL yoki SQLite fayl).
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

# Python'ni "tovushsiz" va tezkor rejimga o'tkazish
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tizim bog'liqliklari: gcc/psycopg kerak emas — asyncpg wheel'lari bor,
# lekin curl health-check va tzdata uchun qo'yildi.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bog'liqliklarni avval o'rnatamiz (Docker layer cache uchun)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Loyiha kodi
COPY . .

# SQLite / arxiv fayllari uchun jild nuqtasi (docker-compose'da volume bilan)
RUN mkdir -p /data
ENV DATABASE_URL="sqlite+aiosqlite:////data/hugecloud.sqlite3"

# PaaS (Render/Heroku/Fly) PORT ni o'zi beradi — sukut 8000
EXPOSE 8000

# Ixtiyoriy: konteyner sog'lig'ini tekshirish (GET / -> "OK")
HEALTHCHECK --interval=60s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1

# root emas, oddiy foydalanuvchi (xavfsizlik)
RUN useradd --create-home --uid 10001 hugecloud \
    && chown -R hugecloud:hugecloud /app /data
USER hugecloud

# `RUN_MODE` orqali rejimni almashtirish mumkin: all | bot | api
CMD ["python", "main.py", "--mode", "all"]
