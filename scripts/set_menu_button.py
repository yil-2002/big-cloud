"""Mini App menyu tugmasini o'rnatish va **tekshirish** (bir martalik skript).

Nega kerak: ``scripts/tunnel.sh`` ``.env`` dagi ``BASE_URL`` ni yangilardi, lekin
``setChatMenuButton`` chaqirilmasdi — Telegram'dagi menyu tugmasi eski manzilga
ishora qilib qolardi. Endi bu ish har startup'da avtomatik bajariladi
(:func:`bot.setup.ensure_menu_button`); shu skript esa restart qilmasdan,
**hozir** o'rnatish va natijani Telegram'dan qayta o'qib tasdiqlash uchun.

Skript uchta qadam qiladi:

1. ``WEB_APP_URL`` manzilini HTTPS'ga tekshiradi va unga ``HEAD`` so'rovi yuboradi
   (nginx/TLS haqiqatan ishlayaptimi — Telegram'ga yozishdan oldin);
2. ``setChatMenuButton`` chaqiradi;
3. ``getChatMenuButton`` bilan **qayta o'qib** tasdiqlaydi.

Ishga tushirish::

    ./venv/bin/python scripts/set_menu_button.py            # o'rnatish + tekshiruv
    ./venv/bin/python scripts/set_menu_button.py --dry-run   # faqat ko'rsatish
    ./venv/bin/python scripts/set_menu_button.py --skip-http-check  # HEAD tekshiruvsiz

Chiqish kodi: ``0`` — muvaffaqiyat, ``1`` — o'rnatilmadi (sabab log'da).
Token hech qayerda chop etilmaydi (faqat oxirgi 4 belgi).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# ``python scripts/set_menu_button.py`` ko'rinishida loyiha ildizi sys.path'da bo'lmaydi.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from bot.setup import build_bot, ensure_menu_button
from config import Settings, get_settings

#: Bo'sh qiymat uchun ko'rinarli yorliq (py311 f-string cheklovi tufayli konstanta).
EMPTY_LABEL = "<bo'sh>"


def _mask(token: str) -> str:
    """Token'ni log'ga chiqarmaslik uchun niqoblash (7-qoida)."""
    return f"***{token[-4:]}" if token else "<bo'sh>"


async def _probe(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """``HEAD`` so'rovi: TLS va nginx haqiqatan ishlayaptimi?"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.head(url)
            if response.status_code in (405, 501):  # HEAD qo'llab-quvvatlanmasa
                response = await client.get(url)
            return response.status_code < 500, f"HTTP {response.status_code}"
    # Foydalanuvchiga aniq sabab ko'rsatamiz (TLS, DNS, timeout — hammasi bir xil yo'l).
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def run(*, dry_run: bool = False, skip_http_check: bool = False) -> int:
    settings: Settings = get_settings()
    url = (settings.web.web_app_url or "").strip()
    text = settings.web.menu_button_text

    print("Mini App menyu tugmasi")
    print(f"  BOT_TOKEN      : {_mask(settings.bot.token.get_secret_value())}")
    print(f"  BASE_URL       : {settings.app.base_url}")
    print(f"  WEB_APP_URL    : {url or EMPTY_LABEL}")
    print(f"  WEB_MOUNT_PATH : {settings.web.mount_path}")
    print(f"  Tugma matni    : {text}")
    print(f"  WEB_SET_MENU_BUTTON: {settings.web.set_menu_button}")

    if not url.startswith("https://"):
        print("\nXATO: manzil HTTPS emas — Telegram bunday Mini App'ni rad etadi.")
        print("      .env da BASE_URL yoki WEB_APP_URL ni https:// bilan bering.")
        return 1

    if not skip_http_check:
        ok, detail = await _probe(url)
        print(f"  Manzil tekshiruvi (HEAD) : {detail} -> {'OK' if ok else 'MUAMMO'}")
        if not ok:
            print("\nOGOHLANTIRISH: manzil javob bermadi. nginx/TLS ishga tushmagan bo'lishi")
            print("mumkin. Baribir davom etish uchun --skip-http-check qo'shing.")
            return 1

    if dry_run:
        print("\n--dry-run: hech narsa o'zgartirilmadi.")
        return 0

    bot = build_bot(settings)
    try:
        installed = await ensure_menu_button(bot, settings)
        if not installed:
            print("\nXATO: setChatMenuButton bajarilmadi (log'ga qarang).")
            return 1

        # DALIL: Telegram'dan qayta o'qib ko'ramiz
        current = await bot.get_chat_menu_button()
        print("\nTelegram tasdiqladi (getChatMenuButton):")
        print(f"  type : {getattr(current, 'type', current)}")
        print(f"  text : {getattr(current, 'text', '—')}")
        print(f"  url  : {getattr(getattr(current, 'web_app', None), 'url', '—')}")

        actual = getattr(getattr(current, "web_app", None), "url", None)
        if actual != url:
            print(f"\nOGOHLANTIRISH: kutilgan {url}, qaytgan {actual}")
            return 1

        print("\nTayyor. Endi BotFather'da 'Main Mini App' yoqilganini tekshiring:")
        print("  @BotFather -> /mybots -> <bot> -> Bot Settings -> Menu Button")
        print("  (getMe javobidagi has_main_web_app: true bo'lishi kerak)")
        return 0
    finally:
        await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mini App menyu tugmasini o'rnatish")
    parser.add_argument("--dry-run", action="store_true", help="faqat ko'rsatish, o'zgartirmaslik")
    parser.add_argument(
        "--skip-http-check", action="store_true", help="manzilga HEAD so'rovini o'tkazib yuborish"
    )
    args = parser.parse_args()
    return asyncio.run(run(dry_run=args.dry_run, skip_http_check=args.skip_http_check))


if __name__ == "__main__":
    raise SystemExit(main())
