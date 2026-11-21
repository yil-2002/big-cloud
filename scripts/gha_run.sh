#!/usr/bin/env bash
# ===========================================================================
# hugecloud — GitHub Actions runner'ida botni ishga tushirish (VPS'siz rejim)
#
# Minimal sozlama: faqat BOT_TOKEN + ARCHIVE_CHANNEL_ID (+ ADMIN_ID).
#
# BAZA:
#   * DATABASE_URL berilmagan -> SQLite, nusxasi yopiq kanalda (pin qilingan
#     xabar). Ishga tushishda tiklanadi, har DB_SYNC_SECONDS da va to'xtashda
#     kanalga yuboriladi (scripts/tg_db_sync.py).
#   * DATABASE_URL berilgan (Neon/Supabase Postgres) -> to'g'ridan-to'g'ri.
#
# MANZIL (Mini App):
#   * PUBLIC_URL + ORIGIN_SECRET -> doimiy Cloudflare Worker manzili;
#   * CF_TUNNEL_TOKEN + PUBLIC_URL -> o'z domeningizdagi named tunnel;
#   * hech biri yo'q -> trycloudflare manzili to'g'ridan-to'g'ri ishlatiladi
#     (har ~6 soatda o'zgaradi, bot menyu tugmasini o'zi yangilaydi).
#
# RUN_SECONDS tugagach keyingi run navbatga qo'yiladi va ilova toza yopiladi
# (concurrency guruhi tufayli bir vaqtda faqat bitta bot ishlaydi).
# ===========================================================================
set -uo pipefail

PORT="${PORT:-8000}"
RUN_SECONDS="${RUN_SECONDS:-20700}"          # 5 soat 45 daqiqa (job limiti 6 soat)
DB_SYNC_SECONDS="${DB_SYNC_SECONDS:-300}"    # kanalga zaxira oralig'i
DB_FILE="data/hugecloud.sqlite3"
APP_LOG="${RUNNER_TEMP:-/tmp}/hugecloud-app.log"
CF_LOG="${RUNNER_TEMP:-/tmp}/cloudflared.log"
APP_PID=""
CF_PID=""
ORIGIN=""
PUBLIC_URL="${PUBLIC_URL:-}"
PUBLIC_URL="${PUBLIC_URL%/}"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "::error::$*"; exit 1; }

# --- 0. Tekshiruvlar va rejimlar --------------------------------------------
[ -n "${BOT_TOKEN:-}" ] || die "BOT_TOKEN secret topilmadi. Settings -> Secrets and variables -> Actions -> Repository secrets ga qo'shing (Environment secrets emas)."

# Bo'sh secret'lar "" bo'lib keladi — pydantic int maydonlarda yiqilmasin.
for var in ADMIN_ID ADMIN_IDS ADMIN_PASSWORD ARCHIVE_CHANNEL_ID TELEGRAM_API_ID \
           TELEGRAM_API_HASH JWT_SECRET BOT_USERNAME BOT_PASSWORD DATABASE_URL \
           ORIGIN_SECRET CF_TUNNEL_TOKEN; do
  if [ -z "${!var:-}" ]; then unset "$var"; fi
done

if [ -n "${DATABASE_URL:-}" ]; then
  case "$DATABASE_URL" in
    sqlite*) die "DATABASE_URL SQLite bo'lmasligi kerak (runner diski o'chadi). Uni o'chirib qo'ying — baza kanalda saqlanadi." ;;
  esac
  DB_MODE="postgres"
else
  [ -n "${ARCHIVE_CHANNEL_ID:-}" ] || die "ARCHIVE_CHANNEL_ID secret kerak: baza yopiq kanalda saqlanadi (bot kanalda admin bo'lsin)."
  DB_MODE="telegram"
  export DATABASE_URL="sqlite+aiosqlite:///./$DB_FILE"
fi

if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  [ -n "$PUBLIC_URL" ] || die "CF_TUNNEL_TOKEN bilan PUBLIC_URL (o'z domeningiz) ham kerak"
  URL_MODE="named"
elif [ -n "$PUBLIC_URL" ] && [ -n "${ORIGIN_SECRET:-}" ]; then
  URL_MODE="worker"
else
  URL_MODE="direct"
fi
log "Rejim: baza=$DB_MODE, manzil=$URL_MODE"

if [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
  export MT_MEDIA_ENABLED=true
else
  export MT_MEDIA_ENABLED=false
fi
export PORT API_PORT="$PORT" API_HOST=127.0.0.1
mkdir -p data

# --- 1. Baza ------------------------------------------------------------------
db_backup() {
  [ "$DB_MODE" = "telegram" ] || return 0
  python scripts/tg_db_sync.py backup "$DB_FILE" \
    || echo "::warning::Bazani kanalga yuborib bo'lmadi (keyingi urinishda qaytariladi)"
}

if [ "$DB_MODE" = "telegram" ]; then
  python scripts/tg_db_sync.py restore "$DB_FILE" \
    || die "Bazani kanaldan tiklab bo'lmadi — ma'lumot yo'qolmasligi uchun bot ishga tushirilmadi"
fi

# --- 2. Ilova -----------------------------------------------------------------
export_urls() {
  local url="$1"
  export BASE_URL="$url"
  export WEB_APP_URL="$url/index.html"
  export CORS_ORIGINS="${CORS_ORIGINS_OVERRIDE:-$url}"
}

start_app() {
  log "Ilova ishga tushmoqda…"
  python main.py --mode all >>"$APP_LOG" 2>&1 &
  APP_PID=$!
  for _ in $(seq 1 90); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      log "Ilova tayyor (pid $APP_PID)"
      return 0
    fi
    if ! kill -0 "$APP_PID" 2>/dev/null; then
      tail -n 60 "$APP_LOG"
      die "Ilova ishga tushishda yiqildi (yuqoridagi log)"
    fi
    sleep 1
  done
  tail -n 60 "$APP_LOG"
  die "Ilova 90 s ichida /healthz ga javob bermadi"
}

stop_app() {
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
    kill -INT "$APP_PID" 2>/dev/null
    for _ in $(seq 1 25); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 1; done
    kill -9 "$APP_PID" 2>/dev/null || true
  fi
  APP_PID=""
}

# --- 3. Tunnel ----------------------------------------------------------------
start_tunnel() {
  : >"$CF_LOG"
  if [ "$URL_MODE" = "named" ]; then
    cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" >>"$CF_LOG" 2>&1 &
    CF_PID=$!
    ORIGIN="$PUBLIC_URL"
    sleep 5
    return 0
  fi
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >>"$CF_LOG" 2>&1 &
  CF_PID=$!
  ORIGIN=""
  for _ in $(seq 1 60); do
    ORIGIN=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' "$CF_LOG" | head -n1 || true)
    [ -n "$ORIGIN" ] && break
    sleep 1
  done
  [ -n "$ORIGIN" ] || { tail -n 30 "$CF_LOG"; return 1; }
  [ "$URL_MODE" = "worker" ] && echo "::add-mask::$ORIGIN"
  log "Tunnel ochildi${URL_MODE:+ ($URL_MODE)}"
}

wait_tunnel_ready() {
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 "$ORIGIN/healthz" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

register_origin() {
  [ "$URL_MODE" = "worker" ] || return 0
  local payload="$1"
  for attempt in 1 2 3 4 5; do
    if curl -fsS --max-time 15 -X POST "$PUBLIC_URL/__hc/origin" \
        -H "Authorization: Bearer $ORIGIN_SECRET" \
        -H "Content-Type: application/json" \
        -d "$payload" >/dev/null; then
      log "Worker yangilandi"
      return 0
    fi
    log "Worker'ga yozib bo'lmadi (urinish $attempt)"
    sleep $((attempt * 3))
  done
  echo "::warning::Worker'ga tunnel manzilini yozib bo'lmadi — PUBLIC_URL va ORIGIN_SECRET ni tekshiring"
  return 1
}

restart_tunnel() {
  [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null
  wait "$CF_PID" 2>/dev/null
  start_tunnel || return 1
  if [ "$URL_MODE" = "direct" ]; then
    # Manzil o'zgardi: ilova yangi BASE_URL bilan qayta ishga tushadi va
    # Telegram menyu tugmasini yangi manzilga o'rnatadi.
    export_urls "$ORIGIN"
    stop_app
    start_app
  fi
  wait_tunnel_ready || true
  register_origin "{\"origin\":\"$ORIGIN\"}"
}

# --- 4. To'xtatish ------------------------------------------------------------
shutdown() {
  log "To'xtatilmoqda…"
  register_origin '{"origin":null}' || true
  stop_app
  db_backup            # ilova to'xtagach — eng so'nggi holat kanalga
  [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null || true
}
trap 'shutdown; exit 0' INT TERM

queue_next_run() {
  # GITHUB_TOKEN bilan workflow_dispatch yangi run yarata oladi (actions: write).
  if [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GH_TOKEN:-}" ]; then
    gh workflow run "${GITHUB_WORKFLOW_REF_FILE:-bot.yml}" --repo "$GITHUB_REPOSITORY" --ref "${GITHUB_REF_NAME:-main}" \
      && log "Keyingi run navbatga qo'yildi" \
      || echo "::warning::Keyingi runni navbatga qo'yib bo'lmadi — soatlik cron baribir qayta ishga tushiradi"
  fi
}

# --- 5. Asosiy oqim -----------------------------------------------------------
if [ "$URL_MODE" = "direct" ]; then
  # Manzil oldindan kerak (BASE_URL / menyu tugmasi) — avval tunnel.
  start_tunnel || die "Cloudflare tunnel ochilmadi"
  export_urls "$ORIGIN"
  start_app
  wait_tunnel_ready || echo "::warning::Tunnel hali javob bermayapti (DNS tarqalmoqda)"
  log "Mini App manzili: $ORIGIN/index.html"
else
  export_urls "$PUBLIC_URL"
  start_app
  start_tunnel || die "Cloudflare tunnel ochilmadi"
  wait_tunnel_ready || true
  register_origin "{\"origin\":\"$ORIGIN\"}"
fi

deadline=$(( $(date +%s) + RUN_SECONDS ))
next_sync=$(( $(date +%s) + DB_SYNC_SECONDS ))
tunnel_fails=0
app_restarts=0
log "Bot ishlayapti. Tugash vaqti: $(date -u -d "@$deadline" +%H:%M) UTC"

while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 60
  # Ilova yiqilgan bo'lsa — qayta ishga tushirish (ko'pi bilan 5 marta).
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    app_restarts=$((app_restarts + 1))
    tail -n 40 "$APP_LOG"
    if [ "$app_restarts" -gt 5 ]; then
      db_backup
      die "Ilova 5 martadan ko'p yiqildi"
    fi
    echo "::warning::Ilova yiqildi, qayta ishga tushirilmoqda ($app_restarts)"
    start_app
  fi
  # Davriy zaxira (faqat o'zgarish bo'lsa yuboriladi).
  if [ "$(date +%s)" -ge "$next_sync" ]; then
    db_backup
    next_sync=$(( $(date +%s) + DB_SYNC_SECONDS ))
  fi
  # Tunnel sog'ligini tashqaridan tekshirish.
  if curl -fsS --max-time 10 "$ORIGIN/healthz" >/dev/null 2>&1; then
    tunnel_fails=0
  else
    tunnel_fails=$((tunnel_fails + 1))
    if [ "$tunnel_fails" -ge 3 ]; then
      echo "::warning::Tunnel javob bermayapti — qayta ochilmoqda"
      restart_tunnel
      tunnel_fails=0
    fi
  fi
done

log "Vaqt tugadi — navbatdagi runga topshirilmoqda"
shutdown
queue_next_run
log "Yakunlandi"
