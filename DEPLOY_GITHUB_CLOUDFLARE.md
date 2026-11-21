# ☁️ VPS'siz deploy: GitHub Actions + yopiq kanal

Bot **GitHub Actions** runner'ida ishlaydi. Fayllar va bazaning nusxasi
**yopiq Telegram kanalingizda** saqlanadi. Server, Postgres yoki pullik
xizmat kerak emas.

```
Telegram ──(polling)──► GitHub Actions runner
                         ├─ python main.py --mode all   (bot + API + Mini App)
                         ├─ cloudflared quick tunnel ──► https://xxxx.trycloudflare.com (Mini App)
                         └─ data/hugecloud.sqlite3  ◄──► yopiq kanal: pin qilingan "hugecloud-db.sqlite3.gz"
                                                         + barcha yuklangan fayllar (file_id)
```

- **Har bir run ~5 soat 45 daqiqa ishlaydi**, oxirida keyingisini o'zi navbatga qo'yadi.
  Almashish paytida 1–2 daqiqa uzilish bo'ladi, bu vaqtda kelgan xabarlar yo'qolmaydi.
- **Bazada faqat metadata turadi**: foydalanuvchilar, papkalar va `file_id`'lar. Bot uni
  siqib, kanalga **bitta pin qilingan xabar** sifatida yuboradi. Nusxa har 5 daqiqada
  (o'zgarish bo'lsa) va to'xtashdan oldin yangilanadi. Keyingi run shu xabardan tiklanadi.
- **Ma'lumot ustidan yozilmaydi.** Bazani kanaldan tiklab bo'lmasa (tarmoq xatosi yoki
  kanalda boshqa xabar pin qilingan bo'lsa), bot ishga **tushmaydi**.

---

## Minimal sozlash (3 ta secret)

### 1. Kanal
- Bot yopiq kanalda **admin** bo'lishi kerak. Unga quyidagi huquqlarni bering:
  **xabar yuborish**, **xabarlarni tahrirlash** va **pin qilish**.
- Kanalda **boshqa pin qilingan xabar bo'lmasin**. Bo'lsa, pindan oling.

### 2. Repository secrets
**Settings → Secrets and variables → Actions → Repository secrets → New repository secret**

> ⚠️ **Environment secrets** ham, **Codespaces** yoki **Dependabot** bo'limi ham
> to'g'ri kelmaydi — workflow ularni ko'rmaydi.

| Nomi | Qiymati |
|---|---|
| `BOT_TOKEN` | @BotFather tokeni |
| `ARCHIVE_CHANNEL_ID` | yopiq kanal ID'si, `-100…` bilan boshlanadi |
| `ADMIN_ID` | sizning Telegram ID'ingiz |

Ixtiyoriy secret'lar: `ADMIN_PASSWORD` (`/diyor` paroli), hamda `TELEGRAM_API_ID` va
`TELEGRAM_API_HASH` (20 MB dan katta videolarni Mini App'da ko'rish uchun).

### 3. Ishga tushirish
**Actions → bot → Run workflow**. 1–2 daqiqadan so'ng:
- run logida `Mini App manzili: https://….trycloudflare.com/index.html` chiqadi;
- kanalda `hugecloud-db.sqlite3.gz` xabari paydo bo'ladi va pin qilinadi;
- botga `/start` yozing, **☁️ Bulut** tugmasi Mini App'ni ochadi.

---

## ⚠️ Bilib qo'ying

1. **Repo ommaviy (public) bo'lishi kerak.** Private repoda bepul limit oyiga
   ~33 soat, 24/7 uchun bu yetmaydi. Kod va Actions loglari hammaga ko'rinadi,
   secret'lar esa ko'rinmaydi. Shuning uchun log darajasi `WARNING` qilib qo'yilgan.
2. **GitHub qoidalari:** Actions CI/CD uchun mo'ljallangan. Uni doimiy servis sifatida
   ishlatish "kulrang zona", GitHub workflow'ni o'chirib qo'yishi mumkin.
3. **Kanaldagi `hugecloud-db.sqlite3.gz` xabarini o'chirmang va pindan olmang.**
   Kanalda boshqa xabarni pin qilsangiz, bot to'xtaydi. U holda baza xabarini qayta pin qiling.
4. **Run kutilmaganda yiqilsa**, oxirgi ≤5 daqiqadagi o'zgarishlar yo'qolishi mumkin
   (`DB_SYNC_SECONDS` o'zgaruvchisi bilan oraliqni kamaytirish mumkin).
5. **Mini App manzili har ~6 soatda o'zgaradi.** Bot menyu tugmasini o'zi yangilaydi,
   lekin eski ulashish havolalari ishlamay qoladi. Doimiy manzil kerak bo'lsa,
   pastdagi Cloudflare Worker bo'limiga qarang.
6. **Public repoda 60 kun commit bo'lmasa, cron o'chadi.** Runlar bir-birini navbatga
   qo'ygani uchun bot ishlashda davom etadi, lekin vaqti-vaqti bilan biror commit qiling.

---

## Ixtiyoriy: doimiy manzil (Cloudflare Worker)

1. https://dash.cloudflare.com saytida bepul akkaunt oching va **Workers & Pages** bo'limida
   `workers.dev` subdomenini tanlang.
2. **My Profile → API Tokens → Create Token** ga kiring va **"Edit Cloudflare Workers"**
   shablonidan token yarating.
3. Quyidagi Secrets'ni qo'shing: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` va
   `ORIGIN_SECRET` (uzun tasodifiy satr).
4. **Variables** bo'limiga `PUBLIC_URL` = `https://hugecloud.<subdomain>.workers.dev` ni qo'shing.
5. **Actions → deploy-worker → Run workflow** ni bosing, keyin `bot` workflow'ini qayta ishga tushiring.

O'z domeningiz bo'lsa: Zero Trust → Tunnels bo'limida tunnel yarating, uning tokenini
`CF_TUNNEL_TOKEN` secret'iga, domeningizni esa `PUBLIC_URL` ga qo'ying.

## Ixtiyoriy: Postgres
`DATABASE_URL` secret'i berilsa (Neon yoki Supabase **Session pooler**), baza kanal
o'rniga o'sha yerda saqlanadi. Eski SQLite bazani ko'chirish uchun
`scripts/sqlite_to_postgres.py` skriptidan foydalaning.

---

## Muammolar

| Belgi | Sabab va yechim |
|---|---|
| `BOT_TOKEN secret topilmadi` | Secret Repository secrets'da emas (masalan, Environment'da) yoki nomi xato yozilgan. |
| `ARCHIVE_CHANNEL_ID secret kerak` | Kanal ID'sini secret sifatida qo'shing. |
| `Kanalda boshqa xabar pin qilingan` | Boshqa pinni oling yoki `hugecloud-db.sqlite3.gz` xabarini pin qiling. |
| `pinChatMessage: … not enough rights` | Botga kanalda **pin qilish** va **tahrirlash** huquqini bering. |
| `TelegramConflictError` / 409 | Bot boshqa joyda ham ishlayapti (VPS yoki Render). Uni o'chiring. |
| Runlar to'xtab qolgan | **Actions → bot → Run workflow** ni bosing. |
