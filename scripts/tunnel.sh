#!/usr/bin/env bash
set -e

LOG_FILE="/var/log/cloudflared-hugecloud.log"
ENV_FILE="/root/big-cloud/.env"

echo "[$(date -u)] Starting cloudflared tunnel..."
rm -f "$LOG_FILE"
/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8000 --logfile "$LOG_FILE" &
CLOUDFLARED_PID=$!

trap "kill $CLOUDFLARED_PID 2>/dev/null || true; exit 0" SIGINT SIGTERM

# Tunnel URL paydo bo'lishini kutamiz (maksimal 30 soniya)
URL=""
for i in $(seq 1 30); do
    sleep 1
    if [ -f "$LOG_FILE" ]; then
        URL=$(grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' "$LOG_FILE" | head -n 1 || true)
        if [ -n "$URL" ]; then
            break
        fi
    fi
done

if [ -z "$URL" ]; then
    echo "[$(date -u)] ERROR: Could not get tunnel URL within 30 seconds."
    kill "$CLOUDFLARED_PID" 2>/dev/null || true
    exit 1
fi

echo "[$(date -u)] Tunnel URL acquired: $URL"

# .env faylidagi BASE_URL ni yangilash
if grep -q "^BASE_URL=" "$ENV_FILE"; then
    sed -i "s|^BASE_URL=.*|BASE_URL=$URL|" "$ENV_FILE"
else
    echo "BASE_URL=$URL" >> "$ENV_FILE"
fi

echo "[$(date -u)] BASE_URL updated in $ENV_FILE to $URL"

# Menyu tugmasi `/index.html` ga qaratilsin: root (`/`) health-check bo'lib
# qolgan va ``Accept: */*`` yuboradigan WebView'da "OK" ko'rinardi.
if grep -q "^WEB_APP_URL=" "$ENV_FILE"; then
    sed -i "s|^WEB_APP_URL=.*|WEB_APP_URL=$URL/index.html|" "$ENV_FILE"
else
    echo "WEB_APP_URL=$URL/index.html" >> "$ENV_FILE"
fi
echo "[$(date -u)] WEB_APP_URL updated in $ENV_FILE to $URL/index.html"

# Ilovani qayta ishga tushiramiz (BASE_URL yangi manzilga o'tishi uchun).
if systemctl is-active --quiet bigcloud; then
    echo "[$(date -u)] Restarting bigcloud service..."
    systemctl restart bigcloud
fi

# MUHIM: quick-tunnel manzili har ishga tushishda o'zgaradi. Telegram menyu
# tugmasi eski manzilda qolib, foydalanuvchi "sayt ochilmadi" xatosini
# ko'rmasligi uchun uni avtomatik yangilaymiz (2026-09-23 hodisasi:
# operator Hetzner IP'ni to'sgani uchun tunnel yagona ishlaydigan yo'l bo'lib
# qoldi — shuning uchun bu qadam majburiy).
BOT_TOKEN="$(sed -n 's/^BOT_TOKEN=//p' "$ENV_FILE" | head -1 | tr -d '\"' | tr -d "'" | xargs)"
if [ -n "$BOT_TOKEN" ]; then
    ok=$(curl -s --max-time 20 -X POST "https://api.telegram.org/bot$BOT_TOKEN/setChatMenuButton" \
        -H 'Content-Type: application/json' \
        -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"☁️ Bulut\",\"web_app\":{\"url\":\"$URL/index.html\"}}}" \
        | grep -c '"ok":true' || true)
    echo "[$(date -u)] Telegram menu button -> $URL/index.html (ok=$ok)"
fi

echo "[$(date -u)] Cloudflare tunnel is running. Waiting for process..."
wait "$CLOUDFLARED_PID"
