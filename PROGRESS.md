# hugecloud — bajarilgan ishlar va tekshiruvlar

Belgilar: ✅ tugagan va tekshirilgan · 🚧 jarayonda · ⬜ boshlanmagan

## 1. Konfiguratsiya (`config.py`) ✅
- [x] Tipi aniq `BaseSettings` bo'limlari: `app`, `bot`, `db`, `cache`, `api`,
      `security`, `storage`, `web`, `auth`, `archive`.
- [x] Tekis env taxalluslari (`BOT_TOKEN`, `ADMIN_ID`, `PORT`, `DATABASE_URL` …).
- [x] `ADMIN_ID` → `admin_ids` birlashtirish; `is_admin()`, `all_admin_ids`.
- [x] **Cheksiz kvota**: `STORAGE_QUOTA=0` → `unlimited_label = "Cheksiz (Unlimited)"`.
- [x] Webhook sozlamalari: `WEBHOOK_ENABLED/URL/PATH/SECRET` (+ `/` prefiks validatori).
- [x] `DB_AUTO_MIGRATE` (startup'da yetishmayotgan ustunlarni qo'shish).
- [x] 🔐 `JWT_SECRET` bo'sh bo'lsa → `BOT_TOKEN` dan HMAC-SHA256 bilan hosil qilinadi
      (sukut "hammaga ma'lum" kalit yo'q).
- [x] `ADMIN_IDS=` / `CORS_ORIGINS=*` bo'sh/JSON-bo'lmagan qiymatlar (`NoDecode`).
- [x] `.env.example` — 12 bo'lim, har bir o'zgaruvchi izohlangan (test bilan tekshirilgan).

## 2. Ma'lumotlar bazasi (`db/`) ✅
- [x] `database.py` — async engine (PostgreSQL pool / SQLite), `init_db`, `dispose_engine`.
- [x] `models/user.py` — `login`, `password_hash`, `is_authenticated`, `is_blocked`,
      `failed_login_attempts`, `locked_until`, `last_seen_at`, `admin_note`,
      `storage_quota`, `settings` (JSON).
- [x] `models/password.py` — PBKDF2-HMAC-SHA256 (210k iteratsiya) + legacy `sha256$` qo'llab-quvvatlash.
- [x] `models/file.py` — `file_unique_id` (dedup), `archive_chat_id/message_id/file_id`,
      `archived_at`, `is_pinned`, `best_file_id` (arxiv nusxasini afzal ko'radi).
- [x] `models/flag.py` — `system_flags` (jonli sozlamalar: `auth_required`, `auth_allow_register`).
- [x] `models/audit.py` — `audit_logs` (SQLite'da ham avtomatik `id`: `with_variant`).
- [x] `FILE_GROUPS` — 🎬 media · 🎵 audio · 📄 document · 📦 archive · 🤖 apps.

## 3. Servislar (`services/`) ✅
- [x] `storage.py` — dedup (`StoredFile.is_new/deduplicated`), 4+2 saralash, guruh
      filtrlari, `group_counts`, `stats`, `admin_stats`, `list_users`,
      `broadcast_targets`, `reset_password`, `set_blocked`, `set_user_quota`.
- [x] `auth.py` — `register/login/logout/change_password`, urinish hisobi, qulflash, audit.
- [x] `vault.py` — 🛡️ `archive_message` (fon nusxalash + retry), `recover_file`,
      `status` (kanal admin huquqlari), `counters`, `schedule`, singleton.
- [x] `share.py`, `cache.py` (Redis→in-memory fallback), `rate_limiter.py`,
      `audit.py`, `telegram.py`.

## 4. Bot (`bot/`) ✅
- [x] Modullarga bo'lish: `handlers/{start,commands,files,browsing,upload,share,inline,admin,auth,callbacks,storage}.py`.
- [x] `middlewares/auth_guard.py` — 🔐 darvoza: faqat `/start /login /register /logout
      /cancel /help /id`; admin har doim o'tadi; FSM dialoglari to'silmaydi;
      `last_seen_at` minutiga bir marta; message/callback/inline uchun rad javobi.
- [x] `handlers/files.py` — guruhlar + saralash menyusi (`srt:<key>::<ctx>`),
      tanlov `users.settings.sort` da saqlanadi.
- [x] `handlers/admin.py` — 👑 `/diyor`: 📊 Statistika · 👥 Foydalanuvchilar ·
      📢 E'lon · ⚙️ Sozlamalar (jonli auth toggle, arxiv holati, audit).
- [x] `handlers/inline.py` — guruh prefikslari (`video`, `audio`, `doc`, `arxiv`,
      `apk`, `muhimlar` …), auth darvozasi, `best_file_id`.
- [x] `handlers/upload.py` — dedup xabari + fon arxivlash.
- [x] `texts_hugecloud.py` — 62 ta o'zbekcha matn + `install_language()` (uz → en).
- [x] `setup.py` — 11 router + middleware zanjiri (tartib test bilan tasdiqlangan).

## 5. API (`api/`) ✅
- [x] Router'lar: `auth`, `files`, `folders`, `shares`, `stream`, `system`.
- [x] `POST /auth/telegram` (initData → JWT), `POST /auth/register` (birinchi marta
      kelgan foydalanuvchi uchun qator yaratadi), `POST /auth/login`, `GET /auth/me`,
      `GET /auth/me/stats`.
- [x] `GET /files` — `group`, `sort_by`, `sort_desc`, `search`, `apps_only`, `pinned`, sahifalash.
- [x] Serializator: `unlimited`, `quota_label`, `is_pinned`, `archived`,
      `archive_message_id`, `stream_url`, `download_url`.
- [x] Anonim endpointlar **IP bo'yicha** limitlanadi (`throttle` → 429 + `Retry-After`).
- [x] `GET /` content negotiation: brauzer → Mini App HTML; `curl` → `OK`;
      JSON → batafsil holat. `GET /healthz` → har doim `OK`.

## 6. Mini App (`web/index.html`) ✅
- [x] Bitta fayl, build qadami yo'q, Telegram WebApp SDK.
- [x] Tablar: 📋 Barchasi · 🎬 Medialar · 🎵 Audiolar · 📄 Hujjatlar · 📦 Arxivlar ·
      🤖 APK/IPA · 📌 Muhimlar · 🔗 Ulashilgan · 📊 Statistika · 👤 Hisob.
- [x] 6 xil saralash, debounced qidiruv, fayl kartalari (hajm/sana/ko'rishlar),
      pin/share/delete, `<video>`/`<audio>` pleer (Range-capable ticket URL),
      login/register formasi, haptics, toast'lar.

## 7. Runtime (`main.py`) ✅
- [x] `--mode all|bot|api|migrate` (`RUN_MODE` env bilan ham).
- [x] `mode=all`: uvicorn + polling bitta event loop'da; polling xatosi **API'ni
      o'chirmaydi** (2s→5s→10s→30s backoff bilan qayta urinadi).
- [x] `verify_schema()` — startup'da yetishmayotgan ustunlarni `ALTER TABLE ADD COLUMN`.
- [x] Webhook rejimi (`WEBHOOK_ENABLED=true`) — aiohttp `SimpleRequestHandler`.
- [x] Toza shutdown: polling, bot sessiyasi, rate limiter, kesh, engine.

## 8. Migratsiya (`scripts/migrate.py`) ✅
- [x] `COLUMN_PLAN` — `users` (18) va `files` (18) ustunlari; yangi jadvallar; indekslar.
- [x] `--backup` (SQLite faylini nusxalash), `--dry-run`, idempotent.
- [x] **Ma'lumot o'chmaydi** — test bilan tasdiqlangan.

## 9. Deploy ✅
- [x] `Dockerfile` (python:3.11-slim, non-root, `/healthz` healthcheck).
- [x] `docker-compose.yml` (PostgreSQL 16 + Redis 7 + app).
- [x] `Procfile`, `Makefile`, `deploy/hugecloud.service`,
      `deploy/systemd-install.sh`, `deploy/deploy.sh`.
- [x] `.github/workflows/ping.yml` — Render free-tier keep-alive (har 10 daqiqada).
- [x] `render.yaml` — Blueprint: web service (webhook rejimi) + cron (*/10 daqiqa ping)
      + PostgreSQL; `JWT_SECRET`/`WEBHOOK_SECRET` = `generateValue: true`,
      `BOT_TOKEN`/`ADMIN_ID`/`ARCHIVE_CHANNEL_ID` = `sync: false` (maxfiy).
- [x] `LICENSE` (MIT), `.dockerignore` (`.env`/`.git` chiqariladi, `tests/` obrazda qoladi).
- [x] `scripts/package.sh` — dist arxivi (`.zip` + `.tar.gz`, `.env` chiqariladi).
- [x] `deploy/hugecloud.service` — systemd unit: `Restart=always`, `RestartSec=5`,
      `LimitNOFILE=65535`, hardening (`NoNewPrivileges`, `PrivateTmp`,
      `PrivateDevices`, `ProtectSystem=full`, `ProtectHome=read-only`,
      `RestrictSUIDSGID`); loglar `journalctl -u hugecloud` ga tushadi.
      `deploy/systemd-install.sh` papka/python yo'llarini avtomatik almashtiradi
      (`/root/big-cloud` → sizning yo'lingiz).

## 10. Testlar ✅ — `python -m pytest` → **331 passed** (21 fayl)

| Fayl | Testlar | Qamrov |
|---|---|---|
| `tests/test_password.py` | 4 | PBKDF2, salt, legacy sha256, rehash |
| `tests/test_auth.py` | 10 | register/login/lockout/block/change_password |
| `tests/test_storage.py` | 34 | cheksiz kvota, **dedup**, saralash matritsasi, guruhlar, pin, sahifalash, qidiruv |
| `tests/test_vault.py` | 14 | arxivlash, 429 retry, fon vazifa, `recover_file`, kanal holati |
| `tests/test_admin.py` | 19 | 📊 statistika, 👥 ro'yxat/blok/parol, 📢 broadcast, ⚙️ `system_flags`, audit |
| `tests/test_bot.py` | 41 | inline prefikslari, 🔐 darvoza (message/callback/inline), 🇺🇿 matnlar |
| `tests/test_setup.py` | 8 | router/middleware tartibi, API yo'llari |
| `tests/test_api.py` | 28 | initData imzosi, JWT, endpointlar, rate-limit, `/` negotiation |
| `tests/test_migrate.py` | 8 | eski sxema → yangi, ma'lumot saqlanadi, dry-run, idempotent |
| `tests/test_config.py` | 7 | `.env.example` yuklanadi, bo'sh `ADMIN_IDS`, kvota, JWT |
| `tests/test_webhook.py` | 27 | 🪝 bitta port, secret tekshiruvi, **har bir buyruq/tugma matni** dispetcherdan o'tadi, `setWebhook` retry + `max_attempts` |
| `tests/test_webhook_error_containment.py` | 8 | handler yiqilsa ham Telegram'ga `200 {"ok":true}` (retry storm yo'q), xato log'i kontekst bilan |
| `tests/test_filters.py` | 23 | 🛡️ AST regressiya: `Command(...) \| F.text.in_(...)` taqiqlangan, `or_f(...)` majburiy |
| `tests/test_stream.py` | 23 | media oqimi: ticket HMAC/tamper/muddat, Range `206` + `Content-Range`, HEAD, `?download=1`, ulashish havolalari |
| `tests/test_error_middleware.py` | 13 | `ErrorLoggingMiddleware` konteksti, `CancelledError` o'tkaziladi, AuthGuard Telegram xatosini yutadi |
| `tests/test_texts_integrity.py` | 13 | 🇺🇿 ishlatilgan **har bir** `texts.X` mavjud + `install_language` idempotent/`force` |
| `tests/test_startup_smoke.py` | 14 | **haqiqiy jarayon**: `/healthz`, Mini App, webhook 200/403/400, log formati, til o'rnatildi, update bazaga yozildi |
| `tests/test_deploy_files.py` | 11 | `render.yaml`, compose, Dockerfile, systemd, Procfile, `.env.example`, `.dockerignore` izchilligi |
| `tests/test_telegram_metadata.py` | 15 | 📤 `extract_file_info`: kategoriya, fayl nomini tozalash, foto (eng katta o'lcham), duration |
| `tests/test_undefined_names.py` | 6 | 🔎 `symtable` skaneri: aniqlanmagan nom (`NameError`) qayta kiritilmasligi |
| `tests/test_python_compat.py` | 5 | 🐍 Docker `python:3.11` ↔ ruff `target-version` ↔ README izchilligi, 3.12 f-string taqiqlangan |

Barcha testlar oflayn: Telegram, Redis va PostgreSQL talab qilinmaydi
(`test_startup_smoke.py` haqiqiy `python main.py` jarayonini ishga tushiradi,
ammo **soxta token** bilan — tarmoqqa chiqmaydi).

---

## Tekshirilgan (haqiqiy ishga tushirishlar)

```text
python -m compileall .                         → OK
python main.py --mode api                      → API 12+ router, /healthz 200, /docs 200
python main.py --mode all                      → uvicorn + polling bitta loop'da
                                                 (token noto'g'ri bo'lsa polling qayta
                                                  urinadi, API xizmat ko'rsatishda davom etadi)
WEBHOOK_ENABLED=true python main.py --mode all  → bitta port: API + POST /api/webhook
                                                 setWebhook xatosi → 5/15/30/60s retry,
                                                 jarayon TIRIK, /healthz 200
POST /api/webhook (noto'g'ri secret)            → 403 invalid secret token
POST /api/webhook (to'g'ri secret, /start)      → 200 {"ok":true} + bazaga user yozildi
POST /api/webhook (handler yiqilganda)          → 200 {"ok":true} (retry storm yo'q),
                                                 log: Unhandled error ... type=message
                                                 update_id=42 chat=777 user=777
python main.py --mode migrate                  → sxema yangilandi (6 jadval)
python scripts/migrate.py --dry-run / --backup → reja / bajarildi
GET  /            (Accept: text/html)          → 200 Mini App HTML (28 KB)
GET  /            (curl)                       → 200 "OK"
GET  /            (Accept: application/json)   → 200 {"status":"ok", ...}
GET  /healthz                                  → 200 "OK"
GET  /api/v1/system/health                     → database up, cache up
GET  /api/v1/system/version                    → quota 0, "Cheksiz (Unlimited)", page_size 5
POST /api/v1/auth/telegram                     → 200 access_token + token + user
POST /api/v1/auth/login (noma'lum)             → 404 unknown_login
python -m pytest                               → 331 passed
```

## Testlar paytida topilgan va tuzatilgan buglar

1. `audit_logs.id` SQLite'da `NOT NULL` xatosi → `BigInteger().with_variant(Integer, "sqlite")`.
2. `AuditService.safe_log()` da `ip_address` parametri yo'q edi → qo'shildi.
3. Anonim auth endpointlari (`/auth/login`, `/auth/register`) `throttle` dependency
   tufayli **401** qaytarardi → anonim so'rovlar IP bo'yicha limitlanadi.
4. `POST /auth/register` birinchi marta kelgan Mini App foydalanuvchisiga
   **404 "not registered"** qaytarardi → qator avval yaratiladi.
5. `JWT_SECRET` sukut qiymati hammaga ma'lum konstanta edi → `BOT_TOKEN` dan hosil qilinadi.
6. `.env` dagi `ADMIN_IDS=` (bo'sh) ilovani ishga tushirmasdi → `NoDecode` + validator.
7. `run_polling` `resolve_used_update_types(skip_events=True)` chaqiruvi
   `'bool' object is not iterable` xatosini berardi → `skip_events` olib tashlandi.
8. Polling xatosi butun jarayonni (API bilan birga) o'chirardi → backoff bilan qayta urinish.
9. `GET /` health-check Mini App'ni to'sib qo'ygandi (Menu Button "OK" ko'rsatardi)
   → content negotiation: brauzer → HTML, probe → `OK`; aniq health-check `/healthz`.
10. Mini App `/users/me/stats` chaqirardi, lekin route `/auth/me/stats` → to'g'irlandi.
11. **Webhook port urushi**: alohida aiohttp server uvicorn bilan bir portni band
    qilardi → `attach_webhook()` FastAPI'ning o'ziga o'rnatildi (bitta port).
12. **aiogram filtri**: `Command("x") | F.text.in_(...)` dispatch paytida
    `TypeError: unsupported operand type(s) for |: 'Command' and 'bool'` berardi
    (11 joyda) → barchasi `or_f(Command(...), F.text.in_(...))` ga almashtirildi
    va `tests/test_filters.py` AST skaneri bilan muhofaza qilindi.
13. **Stream sarlavhalari**: `Content-Range` yo'qolib qolardi (upstream `dict`-kabi
    sarlavhalar registrga sezgir) → `_upstream_header()` case-insensitive;
    `Accept-Ranges: bytes, bytes` ikkilanishi → `_PASS_THROUGH` dan olindi;
    Telegram'ga ulanish xatosi 500 emas **502** `telegram_unreachable`.
14. **`setWebhook` xatosi jarayonni o'chirardi**: `await register_webhook(...)`
    istisnosi butun `amain` ni yiqitardi → `5s → 15s → 30s → 60s` backoff,
    `max_attempts` chegarasi, konfiguratsiya xatosi (`WEBHOOK_URL` bo'sh) esa
    darhol `ValueError`.
15. **`install_language()` hech qayerda chaqirilmagan edi** (faqat test conftest'ida):
    real startup'da `bot.texts` bo'sh qolib, birinchi xabarda
    `AttributeError: module 'bot.texts' has no attribute 'auth_required_action'`
    berardi → `main()` da `install_language(settings)`, `amain()` da idempotent
    `ensure_language(settings)`; `tests/test_texts_integrity.py` AST orqali
    ishlatilgan **barcha** matn nomlarini tekshiradi.
16. **Webhook retry storm**: handler xatosi Telegram'ga `500` qaytarib, bir xil
    update cheksiz qayta yuborilardi → webhook marshruti xatoni ushlab
    `200 {"ok":true}` qaytaradi; `ErrorLoggingMiddleware` esa uni `type=…
    update_id=… chat=… user=… payload=…` konteksti bilan log qiladi.
17. **`services/telegram.py` da `File` importi yo'q edi** — `File.sanitize_filename`
    va `File.detect_category` **runtime**da chaqiriladi, ya'ni har qanday
    hujjat/rasm yuklashda `NameError: name 'File' is not defined`. Testlar bu
    yo'lni chaqirmagani uchun ko'rinmagan edi → import qo'shildi va
    `tests/test_telegram_metadata.py` (15 test) bilan mustahkamlandi.
18. **`_open_share_token(...)` `state` parametrsiz edi**, lekin himoyalangan
    havola oqimida `start_pass_code_flow(message, token, state)` chaqirilardi →
    parolli deep-link ochilganda `NameError`. `state: FSMContext` parametr qo'shildi,
    chaqiruv joyi yangilandi + statik/funksional test.
19. **Python 3.12 f-string sintaksisi `bot/texts_uz.py` da 5 joyda** ishlatilgan
    edi (`f"...{escape_html(user.first_name or "do'stim")}"`). `Dockerfile` esa
    `python:3.11-slim` → **obrazda SyntaxError, bot umuman ishga tushmaydi**.
    Satrlar 3.11'ga moslashtirildi (`_DEFAULT_NICKNAME` konstantasi) va
    `tests/test_python_compat.py` qo'shildi: ruff `--target-version py311` bilan
    butun loyihani skaner qiladi, Dockerfile/pyproject/README versiyalarini solishtiradi.
20. `api/routes/stream.py` da `Any` annotatsiyasi import qilinmagan edi →
    `get_type_hints()` (masalan `typing` introspeksiyasi) yiqilardi.
21. `make lint` `.env` bo'lmaganda (CI/toza klon) `BOT_TOKEN Field required` bilan
    yiqilardi → `.env` yo'q bo'lsa soxta token bilan import tekshiruvi.

## Lint / sifat darvozalari ✅

```text
ruff check .                       → All checks passed! (pyproject.toml sozlamalari bilan)
make lint                          → compileall + imports OK + ruff OK
make compat                        → py311 sintaksis OK (Docker obraz versiyasi)
python -m compileall .             → OK
```

`pyproject.toml` da **qasddan** o'chirilgan qoidalar (har biri izohlangan):
`BLE001` (chidamli `except Exception` — bot/API bitta xatodan o'chmasligi kerak),
`ISC004` (matn modullaridagi ko'p qatorli satrlar), `TRY401` (kontekst + traceback
bitta yozuvda), `EXE001` (shebang'li, lekin `python main.py` orqali chaqiriladi),
`E501` (uzun hujjat/log satrlari). Qolgan barcha qoidalar yoqilgan — shu sababli
`F821`/`F841`/`B006` kabi real xato sinflari avtomatik ushlanadi.

Yangi himoya qatlamlari (bu bosqichda qo'shildi):

| Fayl | Vazifasi |
|---|---|
| `bot/middlewares/error_logging.py` | Eng tashqi (bizning) xato to'ri: kontekst bilan log + qayta ko'tarish |
| `tests/test_undefined_names.py` | `symtable` skaneri: `NameError` sinfidagi bug'lar qaytmasligi |
| `tests/test_python_compat.py` | Python 3.11 (obraz) sintaksis mosligi |
| `tests/test_texts_integrity.py` | Ishlatilgan har bir `texts.X` mavjud (til o'rnatilishi shart) |
| `tests/test_startup_smoke.py` | Haqiqiy `python main.py --mode all` jarayoni (soxta token) |
| `tests/test_webhook_error_containment.py` | Webhook har doim `200` qaytaradi (retry storm yo'q) |
| `pyproject.toml` | ruff: `target-version = py311`, izohlangan istisnolar |

---

# Mini App 2.0: dizayn tizimi, pleer va bot oqimi (joriy bosqich)

## 1. Bot `/start` darvozasi va deep-link

| Qatlam | Fayl | Vazifasi |
|---|---|---|
| Sessiya qarori | `bot/session.py` | `resolve_session()` → `BLOCKED` / `REQUIRE_AUTH` / `ALLOW`; `GUEST_COMMANDS` |
| Middleware | `bot/middlewares/auth_guard.py` | Har bir update tekshiriladi; `last_seen` 60s throttle |
| Payload parking | `bot/session.py` | `park_payload()` / `take_parked_payload()` — deep-link parametri FSM'da saqlanadi |
| Tiklash | `bot/handlers/start.py` | `resume_after_auth()` → `_dispatch_start(payload, force=True)` |
| Mini App kirishlari | `bot/handlers/miniapp.py` | `extract_share_token()`: `t.me/<bot>?start=<tok>`, `/s/<tok>`, `/share/`, `/l/`, raw token |

Natija: yangi va mavjud sessiyalar bir xil darvozadan o'tadi, `?start=<token>`
parametri autentifikatsiya detour'ida **yo'qolmaydi** — kirish tugagach o'sha
havola avtomatik ochiladi.

## 2. Vizual dizayn tizimi (SaaS, og'ir gradientsiz)

Barcha ranglar **faqat** ikkita token blokida (`:root` va `:root[data-theme="dark"]`);
komponentlar `var(--…)` ishlatadi. Bu `tests/test_webapp.py` dagi
`test_palette_lives_only_in_tokens_and_theme_map` testi bilan qulflangan —
qattiq rang qo'shilsa test yiqiladi.

| Token | Yorug' | Qorong'i |
|---|---|---|
| `--bg` | `#FFFFFF` | `#0F172A` |
| `--surface` | `#FFFFFF` | `#1E293B` |
| `--brand` | `#0066FF` | `#0066FF` |
| `--media-bg` / `--knob` / `--on-media` | mavzuga bog'liq emas (video letterbox qora, kalit oq) |

Mavzu almashtirgichi:

1. **Paint'dan oldin** 6 qatorli sinxron skript `data-theme` ni qo'yadi → FOUC yo'q;
2. manbalar tartibi: `localStorage` → `Telegram.colorScheme` → `prefers-color-scheme`;
3. `applyTheme()` Telegram oynasini ham sinxronlaydi (`setHeaderColor` /
   `setBackgroundColor`, `THEME_COLORS` xaritasidan) va `PATCH /auth/me` orqali
   serverga yozadi;
4. `auto` rejimi `matchMedia(...).addEventListener("change")` bilan tizimni kuzatadi.

## 3. Zamonaviy pleer (client-side)

* Maxsus boshqaruv: play/pause, ±10s, tezliklar `[0.5 … 2]`, ovoz, fullscreen, PiP, klaviatura.
* Buferlash: `waiting`/`seeking` → spinner, `progress` → bufer zonasi; ticket muddati
  tugashidan **30s oldin** avtomatik yangilanadi (`ttl=900s`).
* Xato tiklanishi: 3 marta qayta urinish, oxirgi pozitsiyadan davom (resume),
  ortib boruvchi kechikish `1.2s → 2.4s → 4.8s` (`sleep()`).
* **Pre-flight:** `probeStream()` → `HEAD /stream/{ticket}` (hisoblagichlarni oshirmaydi).
  `4xx` = fatal (aniq sabab ko'rsatiladi, behuda urinishlar yo'q), `5xx`/tarmoq = retry.
* `Media Session` (qulf ekrani/garnitura), `sendData` orqali bot bilan bog'lanish.

## 4. Suzuvchi amallar paneli (FAB)

O'ng-pastda qat'iy joylashgan, `--sh-fab` soyali, pill uslubidagi tugma ikkita
amalni ochadi: **⬆️ Yuklash** (`{type:"hugecloud.upload", name, size, mime}`) va
**📥 Import** (`{type:"hugecloud.import", token}`). Bot tomonda ularni
`bot/handlers/miniapp.py` qabul qiladi.

## 5. API shartnomalari (aniqlashtirildi)

* `PATCH /auth/me` **tekis** maydonlar oladi: `{theme, view_mode, sort_by, sort_desc, …}`
  — ichma-ich `{"settings":{…}}` emas. Frontend `syncPrefs(patch)` orqali yuboradi;
  boot paytida `{sync:false}` bilan aks-sado (echo) oldini oladi.
* Xato kodlari: `ERROR_MESSAGES` lug'ati backend'dagi **haqiqiy** kodlardan yig'ilgan
  (`services/exceptions.py`, `api/**`, `AuthOutcome`) va
  `test_frontend_covers_every_backend_error_code` testi bilan sinxron ushlanadi.
  Auth kodlari (`no_account`, `wrong_password`, `locked`, …) `SERVER_LOCALIZED_CODES`
  da — server ular uchun aniq o'zbekcha matn qaytargani uchun qayta yozilmaydi.
* Media manzillari `normalizeStreamUrls()` orqali sahifa origin'iga **nisbiy** qilinadi:
  `BASE_URL` noto'g'ri sozlansa yoki ilova reverse-proksi/preview ortida ochilsa ham
  ijro buzilmaydi; tashqi CDN manzillariga tegilmaydi.

## 6. Ko'rinish (visibility) semantikasi — tuzatilgan

| Holat | Kim ochadi | Ro'yxatda ko'rinadi |
|---|---|---|
| `PRIVATE` | faqat egasi | ha (egasi uchun) |
| `UNLISTED` | token bilgan har kim | yo'q |
| `PUBLIC` | hamma | ha |

* `ShareService.create_link()` faylni `PRIVATE → UNLISTED` ga ko'taradi
  (`make_public=True` bo'lsa `PUBLIC`) — avval havola yaratilganda fayl `PRIVATE`
  qolar va token `410` qaytarardi.
* `StorageService.set_visibility(is_public=False)` endi **jonli havolani hisobga oladi**:
  faol havola bo'lsa `UNLISTED` (havola ishlashda davom etadi), aks holda `PRIVATE`.
  Buning uchun `_has_active_link()` qo'shildi (SQLite naive datetime saqlagani uchun
  `is_active_now` Python tomonda tekshiriladi).
* `resolve_token()` faqat `PRIVATE` ni rad etadi.

## 7. Jonli smoke-test topgan bug'lar (hammasi tuzatilgan)

| # | Bug | Tuzatish | Test |
|---|---|---|---|
| A | Sozlamalar serverga saqlanmasdi (`{"settings":{…}}` → 422/ignore) | tekis `syncPrefs(patch)` | `test_prefs_are_patched_as_flat_fields_not_nested_settings` |
| B | Havola yaratilgach fayl `PRIVATE` qolar → token `410` | `create_link` → `UNLISTED`/`PUBLIC` | `tests/test_stream.py` (8 ta yangi) |
| C | `set_visibility(false)` jonli havolani o'ldirardi | `_has_active_link()` | `test_turning_public_off_keeps_live_links_working` |
| D | `GET /files?group=pinned` **barcha** faylni qaytarardi (`FILE_GROUPS` da "pinned" yo'q) | `list_files` da `group == "pinned" → pinned_only` | `test_files_group_pinned_returns_only_pinned` |
| E | `folder_id=` (bo'sh/buzilgan) → `ValueError` → **500** | bo'sh = filtrsiz, buzilgan = **422** `validation_error` | `test_empty_folder_id_is_treated_as_unset`, `test_invalid_folder_id_returns_422_not_500` |
| F | Ulashilgan hujjat/ZIP/APK uchun `download_url` berilmasdi (faqat `is_streamable` fayllar) | `_public_payload` har qanday fayl uchun ticket beradi | `test_public_share_payload_offers_download_for_document` |
| G | Pleer o'lik ticket uchun 3 marta behuda qayta urinardi | `HEAD` pre-flight (fatal/retryable ajratish) | `test_player_probes_ticket_before_playing` |

## 8. Demo ma'lumotlari

`python scripts/seed_demo.py` — idempotent demo to'plami: `demo` / `demo1234` hisobi,
11 fayl (video/audio/hujjat/arxiv/APK/IPA), 2 papka, 2 pin, ochiq va parolli
(`2026`) havola. `file_id` lar soxta — haqiqiy ijro uchun `.env` ga o'z `BOT_TOKEN`
ingizni yozing.

## 9. Holat

```text
python -m pytest -p no:warnings   → 461 passed
ruff check .                      → All checks passed!
node --check (ikkala <script>)    → OK
Jonli API smoke (:8000)           → login, ro'yxat, guruhlar, qidiruv, saralash,
                                    statistika, havolalar, unlock, PATCH, HEAD — barchasi ✓
```
