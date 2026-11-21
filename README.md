# ☁️ hugecloud — Telegram Cloud Bot (Production)

**Telegram'ning o'zi — sizning bulutli diskingiz.** Fayllar Telegram serverlarida
saqlanadi, bazada faqat `file_id` (metadata) turadi. Shuning uchun disk hajmi
Telegram'nikiga teng: **cheksiz**.

Bu repo — eski `telegram-cloud-bot` ning production darajasidagi davomi:
to'liq tipli async Python 3.11+, aiogram 3, FastAPI, SQLAlchemy 2 (async),
Telegram Mini App, shaxsiy arxiv kanali, login/parol autentifikatsiya,
inline rejim va saralash/guruhlash.

---

## Mundarija

1. [Imkoniyatlar](#1-imkoniyatlar)
2. [Arxitektura](#2-arxitektura)
3. [Loyiha tuzilishi](#3-loyiha-tuzilishi)
4. [Tezkor boshlash (5 daqiqa)](#4-tezkor-boshlash-5-daqiqa)
5. [Migratsiya (eski bazadan)](#5-migratsiya-eski-bazadan)
6. [Sozlamalar (.env)](#6-sozlamalar-env)
7. [Bot buyruqlari](#7-bot-buyruqlari)
8. [REST API](#8-rest-api)
9. [Telegram Mini App](#9-telegram-mini-app)
10. [Inline rejim](#10-inline-rejim)
11. [Deploy: systemd (VPS)](#11-deploy-systemd-vps)
12. [Deploy: Docker](#12-deploy-docker)
13. [Deploy: Render / Heroku](#13-deploy-render--heroku)
14. [Testlar](#14-testlar)
15. [Muammolarni hal qilish](#15-muammolarni-hal-qilish)

---

## 1. Imkoniyatlar

| # | Funksiya | Tavsif |
|---|----------|--------|
| 1 | ♾️ **Cheksiz kvota** | `STORAGE_QUOTA=0` (sukut) → UI'da "Cheksiz (Unlimited)". Hajm cheklovi yo'q. |
| 2 | 🔁 **Deduplikatsiya** | Bir xil `file_unique_id` ikkinchi marta yuklansa — yangi fayl **yaratilmaydi**, mavjud metadata bir zumda ulanadi (`Bu fayl allaqachon bulutda bor`). |
| 3 | 🛡️ **Shaxsiy arxiv kanali** | Har bir yuklama fonda `ARCHIVE_CHANNEL_ID` ga nusxalanadi; kanal `message_id` va `file_id` bazada saqlanadi. Asl xabar o'chsa ham fayl qoladi va `best_file_id` orqali chaqiriladi. |
| 4 | 🔐 **Login + parol** | Har foydalanuvchining o'z hisobi (`/register`, `/login`). Parol PBKDF2-HMAC-SHA256 (210 000 iteratsiya, stdlib). Hisobsiz foydalanuvchi bulut funksiyalarini ochmaydi. |
| 6 | 🔃 **Saralash** | 4 xil: 🕒 Eng yangi / Eng eski, 📦 Eng katta / Eng kichik (+ 🔤 A→Z / Z→A). Tanlov bazada saqlanadi. |
| 7 | 🗂️ **Guruhlar** | 🎬 Medialar, 🎵 Audiolar, 📄 Hujjatlar, 📦 Arxivlar, 🤖 APK/IPA, 📌 Muhimlar + custom papkalar. |
| 8 | 🔎 **Inline rejim** | Istalgan chatda `@bot so'rov` — sizning bulut fayllaringiz, ulashish kartalari bilan. Guruh prefikslari: `@bot video hisobot`. |
| 9 | 🌐 **Mini App + REST API** | Telegram WebApp: fayllar, pleer (video/audio), ulashish, statistika, hisob ekrani. |
| 10 | 🧹 **Xavfsiz migratsiya** | `ALTER TABLE ADD COLUMN` — mavjud yozuvlar **o'chmaydi**, avtomatik backup. |

**Saqlangan legacy mexanika:** fayllar Telegram'da qoladi (`file_id` orqali chaqiriladi),
umumiy `BOT_PASSWORD` darvozasi, 📌 muhim fayllar, 🤖 APK/IPA tabi, faylni
"media sifatida ko'rish", sahifa hajmi 5 ta, health-check `GET /` → `OK`.

---

## 2. Arxitektura

```
                      ┌──────────────────────────────────────────┐
   Telegram  ───────▶ │  BOT (aiogram 3)                         │
   (polling/webhook)  │  middlewares → handlers → services        │
                      └───────────────┬──────────────────────────┘
                                      │
   Browser / Mini App ──▶ FastAPI ────┤   Bitta jarayon (`RUN_MODE=all`)
   (web/index.html)       /api/v1     │   yoki alohida (`bot` / `api`)
                                      ▼
                          ┌───────────────────────┐   ┌──────────────────────┐
                          │ services/ (biznes)    │──▶│ DB: PostgreSQL/SQLite │
                          │ storage, auth, vault, │   │ users, files, folders │
                          │ share, cache, limiter │   │ shared_links,         │
                          └───────────┬───────────┘   │ audit_logs,           │
                                      │               │ system_flags          │
                                      ▼               └──────────────────────┘
                          Telegram Bot API
                          (media yuborish + 🛡️ arxiv kanali)
```

**Qatlam qoidalari**

* `handlers/` — faqat Telegram mantiqi (matn, klaviatura, FSM); SQL yo'q.
* `services/` — biznes mantiq va DB; aiogram turlarini bilmaydi.
* `api/` — HTTP shartnomasi; xuddi shu `services/` ni chaqiradi.
* `db/models/` — ORM; bitta manba (bot ham, API ham shu modellarni o'qiydi).

**Middleware zanjiri** (tartib muhim):
`UpdateLogging` → `DatabaseSession` (har update'ga bitta sessiya + DI) →
`Throttling` → `AuthMiddleware` (User qatorini yuklaydi) →
`PasswordGateMiddleware` (legacy umumiy parol) → `AuthGuardMiddleware`
(🔐 hisob talabi) → `AlbumMiddleware`.

---

## 3. Loyiha tuzilishi

```
cloud-bot/
├── main.py                  # Kirish nuqtasi: --mode all|bot|api|migrate
├── config.py                # Barcha sozlamalar (pydantic-settings, .env)
├── db/
│   ├── database.py          # async engine + sessiya fabrikasi + init_db
│   └── models/              # user, file, folder, shared_link, audit, flag, password
├── services/
│   ├── storage.py           # fayl/papka/kvota/dedup/saralash/guruhlar
│   ├── auth.py              # register/login/lockout/change_password
│   ├── vault.py             # 🛡️ shaxsiy arxiv kanali (nusxalash + tiklash)
│   ├── share.py             # ulashish havolalari + parol + stream ticket
│   ├── cache.py             # Redis yoki in-memory
│   ├── rate_limiter.py      # slayding oyna (Redis/in-memory)
│   ├── audit.py             # xavfsizlik jurnali
│   └── telegram.py          # media yuborish, album, file_id yordamchilari
├── bot/
│   ├── setup.py             # bot + dispatcher + router/middleware yig'ish
│   ├── handlers/            # start, commands, files, browsing, upload,
│   │                        # share, inline, auth, callbacks
│   ├── middlewares/         # auth.py (sessiya/DI), auth_guard.py (🔐 darvoza),
│   │                        # password_gate.py, throttling.py, album.py
│   ├── keyboards/           # inline + reply klaviaturalar (hugecloud.py)
│   ├── states.py            # FSM: AuthState, AdminState, PasswordState, ...
│   ├── texts.py             # inglizcha asos
│   ├── texts_uz.py          # o'zbekcha legacy qatlam
│   └── texts_hugecloud.py    # 62 ta yangi matn + install_language()
├── api/
│   ├── app.py               # create_app(): router'lar, CORS, Mini App mount
│   ├── dependencies.py      # JWT/initData, throttle, owner talabi
│   ├── security.py          # initData imzosi + JWT
│   ├── serializers.py       # ORM → JSON
│   ├── schemas.py           # Pydantic modellari
│   └── routes/              # auth, files, folders, shares, stream, system
├── web/index.html           # Telegram Mini App (bitta fayl, build yo'q)
├── scripts/migrate.py       # xavfsiz sxema migratsiyasi (+ backup)
├── scripts/seed_demo.py     # idempotent demo ma'lumotlari (demo / demo1234)
├── tests/                   # 461 test (pytest + pytest-asyncio), 23 fayl
├── deploy/                  # hugecloud.service, systemd-install.sh, deploy.sh
├── pyproject.toml           # ruff (lint/format) sozlamalari + target-version=py311
├── render.yaml              # Render Blueprint (web + keep-alive cron + PostgreSQL)
├── LICENSE (MIT), .dockerignore
├── Dockerfile, docker-compose.yml, Procfile, Makefile
├── .github/workflows/bot.yml    # VPS'siz rejim: bot GitHub Actions'da
├── .github/workflows/deploy-worker.yml  # Cloudflare Worker (doimiy manzil)
└── cloudflare/                  # Worker kodi (worker.js, wrangler.toml)
```

---

## 4. Tezkor boshlash (5 daqiqa)

**Talab:** Python 3.11+ (3.13'da sinovdan o'tgan).

```bash
# 1) Kodni olish
git clone https://github.com/yil-2002/telegram-cloud-bot hugecloud
cd hugecloud

# 2) Virtual muhit + bog'liqliklar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # testlar uchun: -r requirements-dev.txt

# 3) Sozlamalar
cp .env.example .env
nano .env
#   BOT_TOKEN=@BotFather'dan olingan token
#   BOT_USERNAME=sizning_bot_username
#   ARCHIVE_CHANNEL_ID=-100xxxxxxxxxx        (ixtiyoriy, lekin tavsiya)
#   DATABASE_URL=sqlite+aiosqlite:///./data/hugecloud.sqlite3   (lokal uchun)

# 4) Sxemani yaratish/yangilash (ma'lumot o'chmaydi)
python scripts/migrate.py

# 5) Ishga tushirish (bot + API + Mini App bitta jarayonda)
python main.py
#   yoki:  python main.py --mode all
#   yoki:  RUN_MODE=bot python main.py      (faqat bot)

# 6) Tekshirish
curl http://localhost:8000/healthz         # -> OK  (health-check)
curl http://localhost:8000/api/v1/system/health
# Mini App: http://localhost:8000/  (brauzerda ochilsa HTML qaytadi)
# Swagger : http://localhost:8000/docs
```

Telegram'da botingizni oching → `/start` → `/register <login> <parol>` → `/files`.

> 💡 **Telegram ID'ingizni bilish:** botga `/id` yozing.

### Arxiv kanalini ulash (🛡️ tavsiya etiladi)

1. Maxfiy kanal yarating (masalan `My Cloud Archive`).
2. Botni kanalda **admin** qiling (xabar yuborish huquqi bilan).
3. `.env` ga kanal ID'sini yozing: `ARCHIVE_CHANNEL_ID=-1001234567890`
   (ID'ni bilish uchun: kanalda `@userinfobot` yoki kanalni `web.telegram.org`
   da ochib, URL'dagi `-100...` qismini oling).
4. Botni qayta ishga tushiring. Holatni tekshirish:
   `GET /api/v1/system/version`.

Endi har bir yuklama fonda kanalda nusxalanadi. Agar asl `file_id` ishlamay
qolsa, bot faylni arxiv nusxasidan tiklaydi (`services/vault.py: recover_file`).

---

## 5. Migratsiya (eski bazadan)

Eski bot (`files`, `folders`, `users` jadvallari) bazasini yangilash:

```bash
python scripts/migrate.py --backup        # SQLite bo'lsa avtomatik nusxa oladi
python scripts/migrate.py --dry-run       # faqat rejani ko'rsatadi (o'zgartirmaydi)
```

Nima qiladi:

1. Yetishmayotgan **jadvallar**ni yaratadi: `audit_logs`, `system_flags`,
   `folders`, `shared_links`.
2. Yetishmayotgan **ustunlar**ni qo'shadi (`ALTER TABLE ... ADD COLUMN`):
   `users.login`, `users.password_hash`, `users.is_authenticated`,
   `users.is_blocked`, `users.last_seen_at`, `files.archive_*`,
   `files.file_unique_id`, `files.is_pinned` va h.k.
3. **Indekslar**ni yaratadi (`CREATE INDEX IF NOT EXISTS`).

> ✅ **Ma'lumot o'chmaydi.** `ADD COLUMN` mavjud qatorlarga tegmaydi — yangi
> ustunlar `NULL`/default qiymat oladi. Skript idempotent: ikki marta ishga
> tushirsangiz ham xato bermaydi (hammasi "skipped" bo'ladi).

Startup'da ham avtomatik tekshiriladi (`DB_AUTO_MIGRATE=true`, sukut):
yetishmayotgan ustunlar o'zi qo'shiladi, log'ga yoziladi.

---

## 6. Sozlamalar (.env)

Barcha o'zgaruvchilar izohi bilan `.env.example` faylida. Eng muhimlari:

| O'zgaruvchi | Sukut | Maqsad |
|---|---|---|
| `BOT_TOKEN` | — | **Majburiy.** @BotFather tokeni. |
| `ARCHIVE_CHANNEL_ID` | *(bo'sh)* | 🛡️ Arxiv kanali. Bo'sh = o'chirilgan. |
| `AUTH_REQUIRED` | `true` | 🔐 Hisobsiz foydalanuvchini cheklash. |
| `AUTH_ALLOW_REGISTER` | `true` | `/register` ochiqmi. |
| `STORAGE_QUOTA` | `0` | `0` = **cheksiz**; aks holda baytlar. |
| `STORAGE_DEDUPE` | `true` | `file_unique_id` bo'yicha deduplikatsiya. |
| `DATABASE_URL` | postgres | `postgresql+asyncpg://...` yoki `sqlite+aiosqlite:///...`. |
| `REDIS_URL` | localhost | Bo'lmasa in-memory keshga o'tadi. |
| `RUN_MODE` | `all` | `all` / `bot` / `api` / `migrate`. |
| `PORT` | `8000` | PaaS o'zi beradi. |
| `JWT_SECRET` | *(bo'sh)* | Bo'sh bo'lsa `BOT_TOKEN` dan hosil qilinadi. |
| `WEBHOOK_ENABLED` | `false` | Polling o'rniga webhook (PaaS uchun). |
| `APP_LANGUAGE` | `uz` | `uz` = o'zbekcha interfeys. |
| `PAGE_SIZE` | `5` | Legacy: sahifadagi fayllar soni. |

> 🔐 **Xavfsizlik:** `JWT_SECRET` sukut qiymati yo'q — bo'sh qoldirsangiz
> `BOT_TOKEN` dan HMAC-SHA256 orqali hosil qilinadi. Shuning uchun hech qachon
> "hammaga ma'lum" kalit bilan ishlamaysiz. Bir necha servis bitta sessiya
> bazasini bo'lishsa, aniq `JWT_SECRET` bering (≥ 48 belgi).

---

## 7. Bot buyruqlari

### Hamma uchun ochiq (hatto hisobsiz)

| Buyruq | Vazifa |
|---|---|
| `/start` | Salom + asosiy menyu |
| `/register <login> <parol>` | 🔐 Hisob ochish (dialog rejimi ham bor) |
| `/login <login> <parol>` | Tizimga kirish |
| `/logout` | Chiqish |
| `/help` | Yordam |
| `/id` | Telegram ID'ngiz |
| `/cancel` | Amaldagi dialogni bekor qilish |

### Bulut buyruqlari (hisob talab qilinadi)

| Buyruq | Vazifa |
|---|---|
| `/files` | 🎬 Medialar · 🎵 Audiolar · 📄 Hujjatlar · 📦 Arxivlar · 🤖 APK/IPA · 📌 Muhimlar |
| `/storage` | ☁️ Mening bulutim (statistika, kvota "Cheksiz") |
| `/stats` | 📊 Raqamlar |
| `/search <so'rov>` | 🔎 Qidirish |
| `/shares` | 🔗 Ulashish havolalari |
| `/password` | Parolni almashtirish |

Fayl yuboring → bot uni saqlaydi, dedup tekshiradi va fonda arxivlaydi.
Saralash tugmasi ro'yxat tepasida: `🕒 Eng yangi`, `🕒 Eng eski`,
`📦 Eng katta`, `📦 Eng kichik`, `🔤 A→Z`, `🔤 Z→A`.
## 8. REST API

Prefiks: `/api/v1`. Hujjatlar: `/docs` (Swagger) va `/redoc`.

### Autentifikatsiya

| Metod | Yo'l | Tavsif |
|---|---|---|
| `POST` | `/auth/telegram` | Mini App `initData` → JWT (`access_token` va `token`). |
| `POST` | `/auth/register` | Hisob ochish. Identifikator: `X-Telegram-Init-Data` yoki JWT. |
| `POST` | `/auth/login` | Login + parol → JWT. Xato: `401` (parol), `404` (login yo'q), `403` (blok), `423` (qulf). |
| `POST` | `/auth/logout` | Sessiyani yopish. |
| `GET` | `/auth/me` | Profil (`unlimited`, `quota_label`, `is_authenticated`). |
| `PATCH` | `/auth/me` | Profilni yangilash. |
| `GET` | `/auth/me/stats` | 📊 Statistika (`files`, `used`, `quota`, `by_category`). |

### Fayllar

| Metod | Yo'l | Tavsif |
|---|---|---|
| `GET` | `/files` | Ro'yxat. Parametrlar: `group` (`media/audio/document/archive/apps/pinned/all`), `sort_by` (`created_at/size/name/file_size/views`), `sort_desc`, `search`, `apps_only`, `pinned`, `page`, `page_size`. |
| `POST` | `/files` | Metadata qo'shish (dedup: `file_unique_id`). |
| `POST` | `/files/{id}/pin` | 📌 Muhim belgisi. |
| `POST` | `/files/{id}/share` | 🔗 Havola yaratish. |
| `GET` | `/files/{id}/stream` | Media stream (Range support). |
| `DELETE` | `/files/{id}` | O'chirish. |
| `GET/POST` | `/shares`, `/shares/{token}` | Havolalar ro'yxati / bekor qilish. |
| `GET` | `/stream/{ticket}` | Imzolangan qisqa muddatli playback URL. |

### Tizim

| Metod | Yo'l | Tavsif |
|---|---|---|
| `GET` | `/` (prefikssiz) | **Content negotiation**: brauzer/Telegram → Mini App HTML; `curl` → `OK`; `Accept: application/json` yoki `?format=json` → batafsil holat. |
| `GET` | `/healthz` (prefikssiz) | Health-check → har doim oddiy `OK` (Docker/Kubernetes/PaaS uchun). |
| `GET` | `/index.html` | Mini App sahifasi (to'g'ridan-to'g'ri manzil). |
| `GET` | `/system/health` | DB/kesh holati, uptime. |
| `GET` | `/system/version` | Versiya + funksiyalar (kvota, arxiv, page_size). |
| `GET` | `/system/stats` | 👑 Tizim statistikasi. |
| `POST` | `/system/broadcast` | 👑 Broadcast. |
| `GET/PUT` | `/system/flags/{key}` | 👑 Jonli sozlamalar. |
| `GET` | `/system/audit` | 👑 Audit jurnali. |

**Misol:**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"login":"alisher","password":"parol123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/files?group=media&sort_by=size&sort_desc=true&page=1"
```

Limitlar: har endpoint uchun slayding oyna (`RATE_LIMIT_*`). Anonim so'rovlar
(login/register) **IP bo'yicha** limitlanadi — ortiqcha urinishda `429` +
`Retry-After`.

---

## 9. Telegram Mini App

`web/index.html` — bitta fayl, build qadami yo'q (framework'siz, Telegram WebApp
SDK bilan). Sahifalar:

📋 Barchasi · 🎬 Medialar · 🎵 Audiolar · 📄 Hujjatlar · 📦 Arxivlar ·
🤖 APK/IPA · 📌 Muhimlar · 🔗 Ulashilgan · 📊 Statistika · 👤 Hisob

Imkoniyatlar: saralash (6 xil), debounced qidiruv, fayl kartalari (hajm/sana/
ko'rishlar, 📌 pin, 🔗 share, 🗑️ delete), `<video>`/`<audio>` pleer
(Range-qobiliyatli stream URL orqali), havolani nusxalash/bekor qilish,
login/register formasi, haptic feedback va toast'lar.

### Dizayn tizimi va mavzu almashtirgichi 🌗

SaaS ko'rinishi, og'ir legacy gradientlarsiz. Barcha ranglar ikkita CSS token
blokida (`:root` va `:root[data-theme="dark"]`) — komponentlar faqat `var(--…)`:

| | Yorug' | Qorong'i |
|---|---|---|
| Fon (`--bg`) | `#FFFFFF` | `#0F172A` |
| Sirt (`--surface`) | `#FFFFFF` | `#1E293B` |
| Brend (`--brand`) | `#0066FF` | `#0066FF` |

Mavzu **paint'dan oldin** qo'llanadi (6 qatorli sinxron skript → FOUC yo'q),
manbalar tartibi: `localStorage` → `Telegram.colorScheme` → `prefers-color-scheme`.
Tanlov serverga `PATCH /auth/me` orqali yoziladi (tekis maydonlar:
`{"theme":"dark","view_mode":"grid"}`), Telegram oynasining sarlavha/fon rangi
ham sinxronlanadi.

### Zamonaviy pleer ▶️

Maxsus boshqaruv (play/pause, ±10s, tezlik `0.5…2×`, ovoz, fullscreen, PiP,
klaviatura), bufer zonasi, ticket muddati tugashidan 30s oldin avtomatik
yangilash, 3 marta qayta urinish + oxirgi pozitsiyadan davom (ortib boruvchi
kechikish), `Media Session` (qulf ekrani/garnitura).

Ijrodan oldin `HEAD /stream/{ticket}` **pre-flight** qilinadi: `4xx` bo'lsa
(qodiq ticket, fayl yo'q) foydalanuvchiga aniq o'zbekcha sabab ko'rsatiladi va
behuda qayta urinishlar bo'lmaydi; `5xx`/tarmoq bo'lsa pleer o'zi tiklaydi.

### Suzuvchi amallar paneli (FAB) ⬆️📥

O'ng-pastda qat'iy joylashgan, ko'tarilgan soyali pill tugma: **Yuklash**
(`sendData: {type:"hugecloud.upload", …}`) va **Import**
(`{type:"hugecloud.import", token}`) — bot tomonda `bot/handlers/miniapp.py` qabul qiladi.

### Demo ma'lumotlari bilan sinash

```bash
python scripts/seed_demo.py     # demo / demo1234, 11 fayl, 2 papka, 2 havola
python main.py --mode api       # http://localhost:8000/
```

`file_id` lar soxta, shuning uchun media **ijrosi** `telegram_file_not_found`
beradi (bu kutilgan); interfeys, mavzu, saralash, havolalar va pleer boshqaruvi
to'liq ishlaydi. Haqiqiy ijro uchun `.env` ga o'z `BOT_TOKEN` ingizni yozing.

**BotFather'da ulash:**

```
@BotFather → /mybots → <botingiz> → Bot Settings → Menu Button
  → Configure Menu Button
  → URL: https://sizning-domeningiz/          (SERVE_WEB=true bo'lsa ildizda)
  → Nom: ☁️ Bulut
```

Lokal sinov uchun: `WEB_APP_URL=http://localhost:8000/` va Telegram Desktop'da
bot menyusini oching (yoki `https://web.telegram.org` orqali).

---

## 10. Inline rejim

Istalgan chatda: `@sizning_bot <so'rov>` — o'z bulut fayllaringiz qidiriladi.

| So'rov | Natija |
|---|---|
| `@bot hisobot` | Nomi "hisobot" bo'lgan fayllar |
| `@bot video hisobot` | 🎬 faqat media guruhida |
| `@bot audio qoshiq` | 🎵 audio |
| `@bot doc shartnoma` | 📄 hujjatlar |
| `@bot arxiv` | 📦 arxivlar |
| `@bot apk telegram` | 🤖 APK/IPA |
| `@bot muhimlar` | 📌 pinlanganlar |
| `@bot` (bo'sh) | Eng yangi fayllar |

Har natija — fayl kartasi (nom, hajm, kategoriya) yoki ulashish havolasi.
Hisobsiz foydalanuvchiga bo'sh natija qaytadi (🔐 darvoza inline'ni ham qamrab oladi).

**Yoqish:** @BotFather → Bot Settings → **Inline Mode** → ON.

---

## 11. Deploy: systemd (VPS)

Bu — `systemctl restart hugecloud` + `journalctl` sxemasi.

```bash
# Kodni /root/big-cloud ga joylang (yoki o'z papkangizga)
sudo git clone https://github.com/yil-2002/telegram-cloud-bot /root/big-cloud
cd /root/big-cloud
sudo pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # token, DB, arxiv

# Servisni o'rnatish (unit fayl + enable + start + migratsiya)
sudo bash deploy/systemd-install.sh /root/big-cloud
```

Boshqarish:

```bash
sudo systemctl restart hugecloud          # qayta ishga tushirish
sudo systemctl status hugecloud           # holat
sudo journalctl -u hugecloud -n 50 --no-pager   # oxirgi loglar
sudo journalctl -u hugecloud -f           # jonli kuzatish
```

Keyingi deploy'lar uchun:

```bash
sudo bash deploy/deploy.sh /root/big-cloud
# = git pull → pip install → migratsiya (backup bilan) → testlar → restart → log
```

Unit fayl: `deploy/hugecloud.service` — `Restart=always`, `LimitNOFILE=65535`,
`ProtectSystem=full`, `NoNewPrivileges=true`, loglar `journald` ga tushadi.

> 📝 Agar papkangiz `/root/big-cloud` bo'lmasa, `deploy/systemd-install.sh`
> argument sifatida to'g'ri yo'lni bering — unit fayl shunga moslashtiriladi.

---

## 12. Deploy: Docker

```bash
# To'liq stek: PostgreSQL + Redis + app
docker compose up -d --build
docker compose logs -f app

# Migratsiya
docker compose exec app python scripts/migrate.py

# Sxemani startup'da tekshirish (DB_AUTO_MIGRATE=true) avtomatik bajariladi
```

Yakkalik obraz:

```bash
docker build -t hugecloud .
docker run -d --name hugecloud --env-file .env -p 8000:8000 \
  -v hugecloud_data:/data hugecloud
# Faqat bot kerak bo'lsa:
docker run -d --env-file .env -e RUN_MODE=bot hugecloud
```

Obraz `python:3.11-slim` asosida, root bo'lmagan foydalanuvchi (`uid 10001`),
`HEALTHCHECK` `GET /` ni tekshiradi. SQLite tanlansa ma'lumot `/data` jildida
saqlanadi.

---

## 13. Deploy: Render / Heroku

Bepul tariflarda servis uxlab qoladi va **polling uziladi**. Ikki yechim bor:

### A) Webhook rejimi (tavsiya)

```
RUN_MODE=all
WEBHOOK_ENABLED=true
WEBHOOK_URL=https://hugecloud.onrender.com
WEBHOOK_PATH=/api/webhook
WEBHOOK_SECRET=<tasodifiy uzun satr>
SERVE_WEB=true
PORT=10000            # Render o'zi beradi
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
```

Ishga tushirish buyrug'i: `python main.py --mode all` (`Procfile` da bor).
Health-check uchun `/healthz` ni bering (yoki `/` — brauzer bo'lmagan
so'rovlarda u ham `200 OK` qaytaradi).

> ℹ️ **`GET /` content negotiation:** Telegram'dagi Menu Button aynan `/` ni
> ochadi, shuning uchun brauzer (`Accept: text/html`) Mini App HTML'ini oladi;
> `curl` kabi oddiy probe'lar esa `OK` matnini. Monitoring uchun
> `/api/v1/system/health` (JSON) mavjud.

### B) VPS'siz: GitHub Actions + Cloudflare

Server ijarasiz variant: bot GitHub Actions runner'ida, doimiy manzil —
Cloudflare Worker, baza — Neon/Supabase Postgres. Batafsil qo'llanma:
**[DEPLOY_GITHUB_CLOUDFLARE.md](DEPLOY_GITHUB_CLOUDFLARE.md)**.

**Render sozlamalari:**

| Maydon | Qiymat |
|---|---|
| Runtime | Docker (yoki Native/Python) |
| Start command | `python main.py --mode all` |
| Health check path | `/healthz` (yoki `/` — ikkalasi ham `200`) |
| Env | `.env.example` dagi qiymatlar |

---

## 14. Testlar

```bash
pip install -r requirements-dev.txt
make test                     # yoki: python -m pytest
python -m pytest -k vault     # faqat arxiv testlari
python -m pytest -q --maxfail=1
```

**461 test**, tashqi servislarsiz (Telegram, Redis, PostgreSQL talab qilinmaydi —
SQLite + stub'lar). Bitta istisno: `test_startup_smoke.py` haqiqiy
`python main.py --mode all` jarayonini ishga tushiradi, lekin **soxta token**
bilan — tarmoqqa chiqmaydi, faqat ichki oqimni tekshiradi.

| Fayl | Qamrov |
|---|---|
| `tests/test_password.py` | PBKDF2 hash, legacy sha256, rehash belgisi |
| `tests/test_auth.py` | register/login/qulflash/bloklash/parol almashtirish + audit |
| `tests/test_storage.py` | cheksiz kvota, **dedup**, saralash matritsasi, guruhlar, 📌 pin, sahifalash, qidiruv |
| `tests/test_vault.py` | arxivlash, retry (429), fon vazifasi, `recover_file`, kanal holati |
| `tests/test_admin.py` | 📊 statistika, 👥 ro'yxat/blok/parol tiklash, 📢 broadcast, ⚙️ `system_flags` |
| `tests/test_bot.py` | inline prefikslari, 🔐 darvoza middleware (message/callback/inline), 🇺🇿 matnlar |
| `tests/test_setup.py` | router/middleware yig'ilishi va tartibi, API yo'llari |
| `tests/test_api.py` | initData imzosi, JWT, auth/files/system endpointlari, rate-limit, `/` content negotiation, `group=pinned`, `folder_id` validatsiyasi, 🔗 ommaviy havola payload'i |
| `tests/test_migrate.py` | eski sxema → yangi (ma'lumot **saqlanadi**), idempotent, dry-run |
| `tests/test_config.py` | `.env.example` haqiqatan yuklanadi |
| `tests/test_webhook.py` | 🪝 bitta port, secret tekshiruvi, **har bir buyruq** dispetcherdan o'tadi, `setWebhook` retry |
| `tests/test_webhook_error_containment.py` | handler yiqilsa ham Telegram'ga `200 {"ok":true}` (retry storm yo'q) |
| `tests/test_filters.py` | 🛡️ AST regressiya: `Command(...) \| F.text.in_(...)` aralashmasligi (`or_f` shart) |
| `tests/test_stream.py` | media oqimi: ticket HMAC, Range `206`, HEAD, `?download=1`, ulashish havolalari, **ko'rinish semantikasi** (PRIVATE/UNLISTED/PUBLIC) |
| `tests/test_error_middleware.py` | xato konteksti (`type=… chat=… update_id=…`), `CancelledError` o'tkaziladi |
| `tests/test_texts_integrity.py` | 🇺🇿 ishlatilgan **har bir** `texts.X` mavjud (startup'da til o'rnatilishi shart) |
| `tests/test_startup_smoke.py` | haqiqiy jarayon: `/healthz`, Mini App, webhook, log formati, til o'rnatildi |
| `tests/test_deploy_files.py` | `render.yaml`, compose, Dockerfile, systemd, Procfile, `.env.example` izchilligi |
| `tests/test_telegram_metadata.py` | 📤 `extract_file_info`: hujjat/rasm/video/audio, kategoriya, fayl nomini tozalash |
| `tests/test_undefined_names.py` | 🔎 `symtable` bilan **aniqlanmagan nom** skaneri (`NameError` sinfi bug'lari) |
| `tests/test_python_compat.py` | 🐍 kod `python:3.11-slim` obrazida ishlaydi (3.12 f-string sintaksisi taqiqlangan) |
| `tests/test_gate_flow.py` | 🚪 `/start` darvozasi: `resolve_session`, payload parking, kirishdan keyin deep-link tiklanishi, mehmon buyruqlari |
| `tests/test_webapp.py` | 🎨 Mini App shartnomasi: dizayn tokenlari, mavzu almashtirgichi, FAB, pleer (HEAD pre-flight, retry), tekis `PATCH`, xato kodlari↔backend sinxroni, Node'da sof funksiyalar |

Boshqa foydali buyruqlar:

```bash
make lint          # compileall + import tekshiruvi + ruff (bor bo'lsa)
make compat        # Python 3.11 (Docker obraz) sintaksis mosligi
make fmt           # ruff format + --fix (ixtiyoriy)
make migrate       # sxemani yangilash
make archive       # dist arxivini yig'ish (.zip + .tar.gz)
```

---

## 15. Muammolarni hal qilish

| Muammo | Yechim |
|---|---|
| `BOT_TOKEN Field required` | `.env` yaratilmagan yoki token bo'sh. `cp .env.example .env` va to'ldiring. |
| Bot javob bermaydi | `journalctl -u hugecloud -f` — ko'pincha token noto'g'ri yoki boshqa jarayon polling qilmoqda (`409 Conflict`). |
| `Unauthorized to send messages` (arxiv) | Bot kanalda admin emas yoki "xabar yuborish" huquqi yo'q (bu — kanal huquqi). |
| Fayl saqlanmayapti | `STORAGE_MAX_FILE_SIZE` (2 GiB Bot API chegarasi) va `is_blocked` holatini tekshiring. |
| `Redis unavailable` ogohlantirishi | Xato emas — in-memory keshga o'tadi. Production'da `REDIS_URL` bering. |
| Mini App bo'sh / "OK" ko'rinadi | `SERVE_WEB=true` bo'lishi kerak; brauzerda `/` ni oching (curl `Accept: text/html` yubormasa `OK` qaytadi — bu normal). |
| Bot polling'i yiqildi, lekin API ishlayapti | Normal: polling `2s → 5s → 10s → 30s` pauza bilan qayta urinadi, API esa xizmat ko'rsatishda davom etadi. |
| `/docs` ochilmayapti | `API_DOCS_ENABLED=true` (production'da o'chirish tavsiya etiladi). |
| Eski ustunlar yo'q (`no such column: login`) | `python scripts/migrate.py` yoki `DB_AUTO_MIGRATE=true`. |
| Login band | Foydalanuvchi boshqa login tanlashi kerak; mavjud hisobning parolini egasi tiklaydi. |
| Hisob qulflangan | `AUTH_MAX_ATTEMPTS` marta xato parol; `AUTH_LOCKOUT` soniyadan keyin ochiladi (egasi tiklasa darhol). |
| `setWebhook failed ... Retrying` | `WEBHOOK_URL` ochiq (HTTPS) manzil bo'lishi va `BOT_TOKEN` to'g'ri bo'lishi kerak. Server `5s → 15s → 30s → 60s` pauza bilan qayta urinadi; **API bu vaqtda ham xizmat ko'rsatadi**. |
| Bot javob bermaydi, log'da `Unauthorized` | Webhook rejimida Telegram update yubormayotgan bo'lishi mumkin. Tekshiruv: `curl -X POST $WEBHOOK_URL/api/webhook -H 'X-Telegram-Bot-Api-Secret-Token: ...'` → `403` bo'lsa sekret noto'g'ri, `400` bo'lsa marshrut ishlayapti. |
| Log'da `Unhandled error while processing update` | Handler xatosi, ammo Telegram'ga `200` qaytarilgan (update qayta yuborilmaydi). Satrda kontekst bor: `type=… update_id=… chat=… user=…`. |
| `'bot.texts' has no attribute …` | Til o'rnatilmagan. `main.py` buni startup'da bajaradi (`Language uz installed` log'ini tekshiring); `amain()` ni chetlab o'tib chaqirsangiz `ensure_language(settings)` qo'shing. |

**Loglar:** `LOG_LEVEL=DEBUG` — batafsil (lekin `file_id` lar ko'rinadi,
production'da ehtiyot). `DB_ECHO=true` — barcha SQL.

---

## Production chidamliligi (startup shartnomasi)

Ishga tushish ketma-ketligi **shunday** qurilganki, bitta tashqi servis ishdan
chiqsa ham qolganlari xizmat ko'rsatishda davom etadi:

| Qatlam | Xatti-harakat | Tekshiruv |
|---|---|---|
| 🇺🇿 Til | `install_language()` startup'da majburiy (`main()` va `amain()` idempotent `ensure_language` orqali) — aks holda `bot.texts` bo'sh bo'lib, birinchi xabarda `AttributeError` beradi | `tests/test_texts_integrity.py` |
| 🪝 Webhook | `setWebhook` muvaffaqiyatsiz bo'lsa `5s → 15s → 30s → 60s` bilan qayta urinadi; **jarayon o'lmaydi**, API/Mini App ishlaydi | `tests/test_webhook.py` |
| 🛡️ Xato to'ri | `ErrorLoggingMiddleware` (bizning eng tashqi qatlam) xatoni kontekst bilan log qiladi; webhook marshruti uni ushlab Telegram'ga `200 {"ok":true}` qaytaradi → **retry storm yo'q** | `tests/test_webhook_error_containment.py` |
| 🧮 Filtrlar | `Command(...) \| F.text.in_(...)` ishlatilmaydi (aiogram'da dispatch paytida yiqiladi) — faqat `or_f(...)` | `tests/test_filters.py` (AST skaner) |
| 📡 Polling | Xato → `2s → 5s → 10s → 30s` backoff; API bilan bitta event loop'da, port urishi yo'q | `tests/test_startup_smoke.py` |
| 🗄️ DB / Redis | Redis bo'lmasa in-memory kesh va rate limiter; SQLite katalogi avtomatik yaratiladi | `tests/test_api.py`, `test_config.py` |

Haqiqiy jarayonni tekshirish (tashqi servis talab qilinmaydi):

```bash
# soxta token bilan to'liq startup + webhook oqimi (tarmoqqa chiqmaydi)
python -m pytest tests/test_startup_smoke.py -q

# qo'lda:
BOT_TOKEN="1:fake" DATABASE_URL="sqlite+aiosqlite:///data/app.db" \
WEBHOOK_ENABLED=true WEBHOOK_URL="https://sizning-domen" \
python main.py --mode all
curl -s localhost:8000/healthz          # OK
```

---

## Xavfsizlik eslatmalari

* Parollar **PBKDF2-HMAC-SHA256** (210 000 iteratsiya, tasodifiy salt) bilan
  saqlanadi; bcrypt talab qilinmaydi (stdlib).
* Legacy `sha256$salt$hash` yozuvlari ham qabul qilinadi va kirish paytida
  avtomatik yangi sxemaga o'tkaziladi (`needs_rehash`).
* Media **server proxy** orqali uzatiladi: `file_id` va bot tokeni
  foydalanuvchiga ko'rinmaydi, `Range` so'rovlari ishlaydi (pleer uchun).
* `initData` imzosi Telegram algoritmi bo'yicha tekshiriladi + `auth_date`
  muddati (`INIT_DATA_MAX_AGE`) — replay himoyasi.
* Har xavfsizlik hodisasi `audit_logs` ga yoziladi (kim, nima, qachon, IP).
* `.env` hech qachon Git'ga tushmasin (`.gitignore`da bor); arxivda ham
  `.env` chiqarib tashlanadi, faqat `.env.example` qoladi.

---

## Litsenziya

MIT — erkin foydalaning, lekin javobgarlik o'zingizda.
Asos: [yil-2002/telegram-cloud-bot](https://github.com/yil-2002/telegram-cloud-bot).
