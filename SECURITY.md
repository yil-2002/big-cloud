# 🔐 Xavfsizlik

## Ma'lumotlar qayerda va qanday himoyalangan

| Ma'lumot | Qayerda | Himoya |
|---|---|---|
| Bot tokeni, API kalitlar | GitHub **Repository secrets** | Shifrlangan; loglarda avtomatik yashiriladi; kodda yo'q |
| Baza (foydalanuvchilar, papkalar, `file_id`, ulashish havolalari, parol xeshlari) | Yopiq kanal: pin qilingan `hugecloud-db.enc` | **AES-256-GCM**, kalit = scrypt(`DB_ENCRYPTION_KEY`). Kalitsiz o'qib bo'lmaydi |
| Media fayllar | Yopiq arxiv kanali (Telegram bulutida) | Kanal yopiq; izohlarda **user ID va fayl nomi yo'q** (anonim belgi) |
| Foydalanuvchi parollari | Baza ichida | Xesh (ochiq matn saqlanmaydi) |
| Mini App manzili | Runner | Ommaviy Actions loglarida yashiriladi |

## Cheklovlar (ochiq aytamiz)

- **Media fayllarning o'zi uchdan-uchga shifrlanmagan.** Telegram bulutidagi kanal
  xabarlari Telegram serverida shifrlangan saqlanadi, lekin Telegram ularni o'qiy oladi
  (oddiy chatlar kabi). Faylni bot tomonida shifrlash Telegram'ning o'z pleerini,
  previewlarni va "Telegram'da ochish" funksiyasini o'chiradi.
- Arxiv kanaliga nusxalangan **hujjatlarning fayl nomi** Telegram atributi sifatida
  qoladi (izohdan olib tashlangan, lekin hujjat nomini `copy_message` o'zgartira olmaydi).
- Runner ishlayotgan paytda baza **runner xotirasida ochiq** turadi (ilova ishlashi uchun).
  Runner GitHub'ning vaqtinchalik, izolyatsiyalangan mashinasi; ish tugagach o'chiriladi.

## Avtomatik tekshiruvlar (GitHub)

| Workflow | Nima qiladi | Qachon |
|---|---|---|
| `security.yml` → gitleaks | Butun git tarixida maxfiy kalit/token qidiradi | Har push, PR, haftalik |
| `security.yml` → pip-audit | Kutubxonalardagi ma'lum zaifliklar (CVE) | Har push, PR, haftalik |
| `security.yml` → bandit | Python kodidagi xavfli naqshlar | Har push, PR, haftalik |
| `security.yml` → tests | To'liq test to'plami | Har push, PR |
| `codeql.yml` | Python, JavaScript va workflow'lar tahlili | Har push, PR, haftalik |
| `dependabot.yml` | Zaif kutubxonalar uchun avtomatik PR | Haftalik |

## GitHub sozlamalarida yoqing (qo'lda)

**Settings → Code security** (*Security & analysis*):
- **Secret scanning** va **Push protection** — tasodifan push qilingan tokenni bloklaydi;
- **Dependabot alerts** va **Dependabot security updates**;
- **Private vulnerability reporting**.

**Settings → Actions → General → Workflow permissions**:
- **Read repository contents** (standart: faqat o'qish) ni tanlang. `bot.yml` o'ziga
  kerak bo'lgan `actions: write` huquqini o'zi so'raydi.

## Zaiflik haqida xabar berish
Ommaviy issue ochmang — **Security → Report a vulnerability** orqali yozing.
