#!/usr/bin/env bash
# ===========================================================================
# hugecloud — GitHub Actions runner'ida botni ishga tushirish (VPS'siz rejim)
#
# Oqim:
#   1. ilova (bot polling + API + Mini App) 127.0.0.1:8000 da fon rejimida;
#   2. /healthz tayyor bo'lgach Cloudflare tunnel ochiladi;
#   3. tunnel manzili Cloudflare Worker'ga yoziladi (doimiy PUBLIC_URL);
#   4. har 60 s tekshiruv: tunnel o'lsa — qayta ochiladi, ilova o'lsa — qayta
#      ishga tushiriladi;
#   5. RUN_SECONDS tugagach keyingi workflow run navbatga qo'yiladi va ilova
#      SIGINT bilan toza yopiladi (concurrency guruhi tufayli yangi run faqat
#      shu run tugagach boshlanadi — 409 Conflict bo'lmaydi).
#
# Kerakli env: BOT_TOKEN, DATABASE_URL, PUBLIC_URL, ORIGIN_SECRET
# Ixtiyoriy:   CF_TUNNEL_TOKEN (o'z domeningizdagi named tunnel), RUN_SECONDS
# ===========================================================================
set -uo pipefail

PORT="${PORT:-8000}"
RUN_SECONDS="${RUN_SECONDS:-20700}"   # 5 soat 45 daqiqa (job limiti 6 soat)
APP_LOG="${RUNNER_TEMP:-/tmp}/hugecloud-app.log"
CF_LOG="${RUNNER_TEMP:-/tmp}/cloudflared.log"
APP_PID=""
CF_PID=""
ORIGIN=""

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "::error::$*"; exit 1; }

# --- 0. Tekshiruvlar ---------------------------------------------------------
[ -n "${BOT_TOKEN:-}" ] || die "BOT_TOKEN secret berilmagan"
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL secret berilmagan (Neon/Supabase Postgres kerak — runner diski har safar o'chadi)"
case "$DATABASE_URL" in
  sqlite*) die "DATABASE_URL SQLite bo'lmasligi kerak: runner har 6 soatda yangilanadi va ma'lumot yo'qoladi" ;;
esac
[ -n "${PUBLIC_URL:-}" ] || die "PUBLIC_URL berilmagan (masalan https://hugecloud.<siz>.workers.dev)"
if [ -z "${CF_TUNNEL_TOKEN:-}" ] && [ -z "${ORIGIN_SECRET:-}" ]; then
  die "ORIGIN_SECRET berilmagan (Worker'ga tunnel manzilini yozish uchun)"
fi
PUBLIC_URL="${PUBLIC_URL%/}"

# Bo'sh secret'lar "" bo'lib keladi — pydantic int maydonlarda yiqilmasin.
for var in ADMIN_ID ADMIN_IDS ADMIN_PASSWORD ARCHIVE_CHANNEL_ID TELEGRAM_API_ID \
           TELEGRAM_API_HASH JWT_SECRET BOT_USERNAME BOT_PASSWORD; do
  if [ -z "${!var:-}" ]; then unset "$var"; fi
done
if [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
  export MT_MEDIA_ENABLED=true
else
  export MT_MEDIA_ENABLED=false
fi

export PORT API_PORT="$PORT" API_HOST=127.0.0.1
export BASE_URL="$PUBLIC_URL"
export WEB_APP_URL="$PUBLIC_URL/index.html"
export CORS_ORIGINS="${CORS_ORIGINS:-$PUBLIC_URL}"
mkdir -p data

# --- 1. Ilova ----------------------------------------------------------------
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

# --- 2. Tunnel ---------------------------------------------------------------
start_tunnel() {
  : >"$CF_LOG"
  if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
    # Named tunnel: manzil doimiy (PUBLIC_URL = o'z domeningiz), Worker shart emas.
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
  echo "::add-mask::$ORIGIN"
  # DNS tarqalishini kutish.
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 "$ORIGIN/healthz" >/dev/null 2>&1 && break
    sleep 2
  done
  log "Tunnel ochildi"
}

register_origin() {
  [ -n "${CF_TUNNEL_TOKEN:-}" ] && return 0
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
  if start_tunnel; then
    register_origin "{\"origin\":\"$ORIGIN\"}"
  fi
}

# --- 3. To'xtatish -----------------------------------------------------------
shutdown() {
  log "To'xtatilmoqda…"
  register_origin '{"origin":null}' || true
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
    kill -INT "$APP_PID" 2>/dev/null
    for _ in $(seq 1 25); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 1; done
    kill -9 "$APP_PID" 2>/dev/null || true
  fi
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

# --- 4. Asosiy sikl ----------------------------------------------------------
start_app
start_tunnel || die "Cloudflare tunnel ochilmadi"
register_origin "{\"origin\":\"$ORIGIN\"}"

deadline=$(( $(date +%s) + RUN_SECONDS ))
tunnel_fails=0
app_restarts=0
log "Bot ishlayapti. Tugash vaqti: $(date -u -d "@$deadline" +%H:%M) UTC"

while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 60
  # Ilova yiqilgan bo'lsa — qayta ishga tushirish (ko'pi bilan 5 marta).
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    app_restarts=$((app_restarts + 1))
    tail -n 40 "$APP_LOG"
    [ "$app_restarts" -le 5 ] || die "Ilova 5 martadan ko'p yiqildi"
    echo "::warning::Ilova yiqildi, qayta ishga tushirilmoqda ($app_restarts)"
    start_app
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
queue_next_run
shutdown
log "Yakunlandi"
