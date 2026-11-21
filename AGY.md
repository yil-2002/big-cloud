# AGY.md — ish papkasi konteksti (har vazifadan oldin o'qi)

## Loyiha
- Nomi: **big-cloud / Huge Cloud** — Telegram bot + FastAPI + Mini App.
- Ish papkasi: `/root/agy/work/big-cloud` (bu **nusxa**). Jonli: `/root/big-cloud`.
- Asosiy fayl: `web/index.html` — Mini App (bitta fayl, vanilla JS, inline CSS, build yo'q).

## Fayl xaritasi (qisqa)
| Fayl | Nima |
|---|---|
| `web/index.html` | Mini App: CSS + markup + JS (pleer, lightbox, upload, navbar) |
| `api/routes/files.py` | `/files` API: listing, register, **/upload** (Mini App relay), stream ticket |
| `api/routes/stream.py` | `/stream/{ticket}` — Range proxy; thumbnail; Content-Type |
| `api/app.py` | FastAPI yig'ish, Mini App mount, no-cache sarlavhalari |
| `bot/handlers/*.py` | aiogram handlerlar (`admin.py` = /diyor panel) |
| `bot/middlewares/auth_guard.py` | darvoza; `ALLOWED_COMMANDS` (ichida `/diyor`) |
| `services/storage.py` | DB amallari (`add_file`, `list_files`, kvota) |
| `tests/test_webapp.py` | **Mini App shartnomasi** — ID'lar, tokenlar, sintaksis |

## O'zgarmas qoidalar
1. Tashqi kutubxona/CDN/shrift **yo'q** (React/framer-motion/lucide mumkin emas) — faqat inline SVG + vanilla JS.
2. Komponent CSS'ida **hex rang yo'q** — `var(--...)` yoki `rgba(...)`.
3. JS'da `$("id")` ishlatilsa, o'sha ID markupda **bo'lishi shart**.
4. Mavjud ID/funksiyalarni buzmang (pleer: `mediaEl, btnPlay, progress, ...`).
5. `node --check` toza; CSS qavslari balansda.
6. Emoji faqat kategoriya belgisi sifatida; chrome ikonkalar — inline SVG.
7. **`python`/`pip`/`venv`/`systemctl`/`git commit` ISHLATILMAYDI.** Tekshiruv: `./verify.sh`.

## Tekshiruv
```bash
cd /root/agy/work/big-cloud && ./verify.sh --fast
```
`verify.sh`: JS sintaksisi + CSS balansi + ID mosligi + hex rang tekshiruvi (+ ixtiyoriy pytest).

## Tunnel guard (cloudflared avto-tuzatish)
`tunnel_guard.py` — `cloudflared{,-bigcloud,-hermes}` tunnellarini kuzatadi,
nosoz bo'lsa restart qiladi va yangi linkni Telegram'ga yuboradi (oxirgi qatorda
qaysi loyiha ekani). Batafsil: `TUNNEL_GUARD.md`.
```bash
python3 /root/deepseek/agy/tunnel_guard.py status|check|fix|notify-test
```
Timer: `tunnel-guard.timer` (60 s), jurnal: `journalctl -u tunnel-guard`.
Faqat nosoz `cloudflared*` unit'lari restart qilinadi.

## Uslub
- O'zbekcha izohlar, mavjud kod uslubiga mos.
- O'zgarishni **minimal va aniq** qil; butun faylni qayta yozma.
- Oxirida: nima o'zgardi (fayl/qator), qaysi ID/funksiya qo'shildi, `verify.sh` natijasi.

## ⛔ Qamrov qoidasi (majburiy)
- **Faqat briefda sanab o'tilgan fayllarni** o'zgartir. Ro'yxatda yo'q faylga **TEGMA**.
- **Global find/replace qilmang** va yordamchi skript (`fix_*.py`, `update_*.py`) **yozmang/ishlatmang**.
- **Kod izohlari/docstring/README** matniga tegma — faqat **foydalanuvchi ko'radigan** matn
  (Mini App ekrani, bot xabarlari) o'zgartiriladi.
- So'zlarni ko'r-ko'rona almashtirmang (`20 MB` → `Maksimal hajm` kabi ma'nosiz natija bo'lmasin).
- Vaqtinchalik fayl qoldirmang.
