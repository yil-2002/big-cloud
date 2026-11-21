# ☁️ VPS'siz deploy: GitHub Actions + Cloudflare

Bot **GitHub Actions** runner'ida ishlaydi, **Cloudflare Worker** esa doimiy
manzil beradi, baza **bepul bulutli Postgres** da (Neon yoki Supabase) turadi.
Hech qanday server ijarasi kerak emas.

```
Telegram ──(polling)──► GitHub Actions runner
                         ├─ python main.py --mode all   (bot + API + Mini App)
                         └─ cloudflared (quick tunnel) ──► https://xxxx.trycloudflare.com
                                                              ▲
Foydalanuvchi / Mini App ──► https://hugecloud.<siz>.workers.dev ──┘  (Worker proksi)
                                                   │
                         Neon/Supabase Postgres ◄──┘ (fayllar metadata, foydalanuvchilar)
```

- Har bir run **~5 soat 45 daqiqa** ishlaydi (GitHub limiti 6 soat), oxirida
  keyingi runni o'zi navbatga qo'yadi. Almashish paytida 1–2 daqiqa uzilish bo'ladi:
  bu vaqtda kelgan xabarlar yo'qolmaydi (`BOT_DROP_PENDING_UPDATES=false`),
  Mini App esa "Server qayta ishga tushmoqda" sahifasini ko'rsatadi.
- `concurrency` tufayli bir vaqtda faqat **bitta** bot ishlaydi (409 Conflict yo'q).
- Soatlik zaxira cron: run yiqilsa ham bot ko'pi bilan 1 soatda qaytadi.
- Fayllarning o'zi baribir Telegram'da (arxiv kanalida) saqlanadi — bazada faqat `file_id`.

---

## ⚠️ Avval bilib qo'ying

1. **Repo ommaviy (public) bo'lishi kerak.** Private repoda bepul limit oyiga
   2000 daqiqa (~33 soat) — 24/7 uchun ~44 000 daqiqa kerak. Public repoda
   standart runner'lar bepul va cheksiz.
   - Kod hammaga ko'rinadi, **secret'lar ko'rinmaydi** (GitHub Secrets).
   - **Actions loglari ham hammaga ko'rinadi.** Shuning uchun `LOG_LEVEL=WARNING`
     sukut qilib qo'yilgan va ilova logi konsolga emas, faylga yoziladi
     (faqat xatoda oxirgi qatorlari chiqadi).
   - `.env` faylini **hech qachon** commit qilmang (`.gitignore` da bor).
2. **GitHub qoidalari:** GitHub Actions asosan CI/CD uchun mo'ljallangan;
   uzoq ishlaydigan servis sifatida ishlatish [Actions shartlari](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
   bo'yicha "kulrang zona" — GitHub bunday workflow'larni o'chirib qo'yishi mumkin.
   Shaxsiy/kichik loyiha uchun ishlaydi, lekin kafolat yo'q.
3. Public repoda 60 kun commit bo'lmasa, **cron** o'chib qoladi. Runlar o'zini
   o'zi navbatga qo'ygani uchun bot ishlashda davom etadi, lekin vaqti-vaqti
   bilan repoga biror commit qiling.
4. Quick tunnel (`trycloudflare.com`) — Cloudflare'ning bepul, akkauntsiz
   xizmati; kafolati yo'q. Barqarorroq variant — pastdagi "O'z domeningiz" bo'limi.

---

## 1. Postgres baza (Neon — tavsiya)

1. <https://neon.tech> → ro'yxatdan o'ting → **New project** (region: Frankfurt).
2. **Connection string** ni nusxalang (`postgresql://...neon.tech/neondb?sslmode=require`).
   Kod `sslmode`/`channel_binding` ni o'zi asyncpg formatiga o'giradi.

> Supabase ishlatsangiz: **Session pooler** (port `5432`) manzilini oling.
> Transaction pooler (port `6543`) asyncpg bilan ishlamaydi.

### Eski bazani ko'chirish (VPS'dagi SQLite)

VPS hali ishlayotgan bo'lsa, **avval botni to'xtating** (`systemctl stop bigcloud`),
keyin VPS'da:

```bash
cd /root/big-cloud && git pull   # yoki yangi fayllarni nusxalang
```

```bash
python scripts/sqlite_to_postgres.py --source "sqlite:///./data/hugecloud.sqlite3" --target "postgresql://USER:PASS@ep-xxx.neon.tech/neondb?sslmode=require"
```

(`--source` dagi yo'lni `.env` dagi `DATABASE_URL` dan oling.)
Skript barcha jadvallarni ko'chiradi va id ketma-ketliklarini to'g'rilaydi.

> ❗ Bitta bot tokeni bilan **faqat bitta** joyda bot ishlashi mumkin.
> GitHub'da yoqishdan oldin VPS'dagi botni o'chiring.

---

## 2. Cloudflare Worker (doimiy manzil)

1. <https://dash.cloudflare.com> → bepul akkaunt.
2. **Account ID**: Dashboard → Workers & Pages → o'ng tomonda "Account ID".
3. **API token**: My Profile → API Tokens → Create Token →
   **"Edit Cloudflare Workers"** shabloni → Create.
4. Birinchi marta Workers & Pages bo'limiga kirib `workers.dev` subdomenini tanlang
   (masalan `diyor` → `https://hugecloud.diyor.workers.dev`).

---

## 3. GitHub repo va secret'lar

1. GitHub'da **public** repo yarating va loyihani push qiling:

```bash
git remote add origin https://github.com/<siz>/hugecloud.git
```

```bash
git push -u origin main
```

2. **Settings → Secrets and variables → Actions → Secrets** ga qo'shing:

| Secret | Qiymat | Majburiy |
|---|---|---|
| `BOT_TOKEN` | @BotFather tokeni | ✅ |
| `DATABASE_URL` | Neon/Supabase connection string | ✅ |
| `ORIGIN_SECRET` | uzun tasodifiy satr (`openssl rand -hex 32`) | ✅ |
| `CLOUDFLARE_API_TOKEN` | 2-bosqichdagi token | ✅ |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID | ✅ |
| `ADMIN_ID` | sizning Telegram ID | tavsiya |
| `ARCHIVE_CHANNEL_ID` | `-100…` arxiv kanali | tavsiya |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | my.telegram.org (20 MB+ oqim uchun) | ixtiyoriy |
| `ADMIN_PASSWORD` | `/diyor` paroli | ixtiyoriy |
| `JWT_SECRET` | ≥48 belgi (bo'sh = BOT_TOKEN'dan hosil qilinadi) | ixtiyoriy |
| `CF_TUNNEL_TOKEN` | faqat o'z domeningiz bo'lsa (pastga qarang) | ixtiyoriy |

3. **Variables** bo'limiga:

| Variable | Qiymat |
|---|---|
| `PUBLIC_URL` | `https://hugecloud.<subdomain>.workers.dev` |
| `BOT_USERNAME` | `Huge_cloud_bot` |
| `LOG_LEVEL` | ixtiyoriy, sukut `WARNING` |

---

## 4. Ishga tushirish

1. **Actions** → `deploy-worker` → **Run workflow**. Logda Worker manzili
   chiqadi — u `PUBLIC_URL` bilan bir xil bo'lishi kerak.
2. **Actions** → `bot` → **Run workflow**. 1–2 daqiqadan so'ng:
   - `https://hugecloud.<subdomain>.workers.dev/__hc/origin` → `{"online":true,…}`
   - botga `/start` yozing; menyu tugmasi (☁️ Bulut) Mini App'ni ochadi.

Bot menyu tugmasini har ishga tushishda `PUBLIC_URL/index.html` ga o'rnatadi —
manzil o'zgarmaydi, shuning uchun eski havolalar ham ishlayveradi.

---

## O'z domeningiz bo'lsa (ixtiyoriy, barqarorroq)

Domen Cloudflare'da bo'lsa, quick tunnel o'rniga **named tunnel** ishlating:

1. Zero Trust → Networks → Tunnels → **Create tunnel** (Cloudflared) →
   tokenni nusxalang → `CF_TUNNEL_TOKEN` secret'iga qo'ying.
2. **Public hostname**: `cloud.sizningdomen.uz` → `http://127.0.0.1:8000`.
3. `PUBLIC_URL` = `https://cloud.sizningdomen.uz`.

Bu holda Worker kerak emas (`deploy-worker` ni ishlatmasangiz ham bo'ladi).

---

## Muammolar

| Belgi | Sabab / yechim |
|---|---|
| `bot` run darhol `BOT_TOKEN secret berilmagan` | Secret nomini tekshiring (katta harflar). |
| `TelegramConflictError` / 409 | Bot boshqa joyda ham ishlayapti (VPS). Uni o'chiring. |
| Mini App "Server qayta ishga tushmoqda" uzoq turadi | Actions → `bot` ishlayaptimi? `__hc/origin` → `online:false` bo'lsa, run tugagan/yiqilgan. |
| `Worker'ga tunnel manzilini yozib bo'lmadi` | `PUBLIC_URL` noto'g'ri yoki Worker'dagi `ORIGIN_SECRET` boshqa — `deploy-worker` ni qayta ishga tushiring. |
| Baza xatosi `sslmode` | Yangi `config.py` kerak (u avtomatik `ssl=` ga o'giradi). |
| Runlar to'xtab qoldi | Actions → `bot` → Run workflow. 60 kunlik cron o'chishi bo'lsa — biror commit qiling. |
