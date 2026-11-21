"""HugeCloud Mini App pleeri — haqiqiy brauzer E2E (Playwright + Chromium).

Nima tekshiriladi (haqiqiy kod yo'li bilan, mock DOM emas):
  1. Ilova yuklanadi, ``/api/v1/files`` javobi intercept qilinadi.
  2. Audio kartaga bosiladi -> ilovaning **o'z** ``playFile(file)`` funksiyasi ishlaydi.
  3. Yangi premium muqova: rasm preview (thumbnail), ambient yorug'lik,
     90° burilish animatsiyasi — DOM va hisoblangan (computed) CSS bo'yicha tasdiqlanadi.
  4. ``prefers-reduced-motion: reduce`` da animatsiyalar o'chishini tekshiradi.
  5. Skrinshot oladi (vizual ko'rik uchun).

Ishlatish: /root/.e2e-venv/bin/python /root/big-cloud/scripts/e2e_player_premium.py
"""
from __future__ import annotations

import base64
import json
import struct
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path("/root/deepseek/shots/premium-player")

# ── Muqova uchun SVG (tarmoqsiz, tashqi resurs yo'q) ─────────────────────────
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#7c3aed"/><stop offset="0.55" stop-color="#0ea5e9"/>
<stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>
<rect width="192" height="192" fill="url(#g)"/>
<circle cx="96" cy="96" r="52" fill="rgba(0,0,0,0.28)"/>
<path d="M84 132V73l46-9v52" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round"/>
<circle cx="74" cy="132" r="11" fill="#fff"/><circle cx="120" cy="116" r="11" fill="#fff"/>
</svg>"""
THUMB = "data:image/svg+xml;base64," + base64.b64encode(SVG.encode()).decode()

def _silent_wav(seconds: float = 0.4, rate: int = 8000) -> bytes:
    """Kichik, haqiqiy WAV — brauzer media elementiga yuklanadi (mock JSON emas)."""
    data = b"\x00\x00" * int(seconds * rate)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


SILENT_WAV = _silent_wav()

AUDIO_FILE = {
    "id": "a1",
    "name": "Premium Test Audio.mp3",
    "size": 5_242_880,
    "category": "audio",
    "mime_type": "audio/wav",
    "duration": 214,
    "thumbnail_url": THUMB,
    "stream_url": "/api/v1/stream/a1",
}
FILES_PAYLOAD = {"items": [AUDIO_FILE], "total": 1, "pages": 1, "page": 1}
#: ``isAuthenticated()`` ``user.is_authenticated`` ni talab qiladi — usiz ilova
#: "hisob tasdiqlanmagan" deb kirish ekranida qoladi.
E2E_USER = {"id": 1, "telegram_id": 1, "username": "e2e", "first_name": "E2E",
            "plan": "pro", "is_authenticated": True, "is_admin": False,
            "storage_used": 0, "storage_limit": 1 << 40}

TG_STUB = """
window.Telegram = { WebApp: {
  initData: "e2e-test", initDataUnsafe: { user: { id: 1, first_name: "E2E" } },
  version: "7.0", platform: "android", colorScheme: "dark",
  themeParams: {}, viewportHeight: 844, viewportStableHeight: 844,
  isExpanded: true, ready: function () {}, expand: function () {}, close: function () {},
  onEvent: function () {}, offEvent: function () {}, sendData: function () {},
  openLink: function () {}, setHeaderColor: function () {}, setBackgroundColor: function () {},
  enableClosingConfirmation: function () {}, disableVerticalSwipes: function () {},
  lockOrientation: function () {}, unlockOrientation: function () {},
  isVersionAtLeast: function () { return true; },
  HapticFeedback: { impactOccurred: function () {}, notificationOccurred: function () {},
                    selectionChanged: function () {} },
  BackButton: { show: function () {}, hide: function () {}, onClick: function () {} },
  MainButton: { show: function () {}, hide: function () {}, setText: function () {},
                onClick: function () {}, offClick: function () {} },
} };
window.TelegramWebviewProxy = { postEvent: function () {} };
"""

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'XATO'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def run(reduced_motion: bool) -> dict:
    label = "reduced-motion" if reduced_motion else "oddiy"
    print(f"\n=== {label} rejimi ===")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            color_scheme="dark",
            reduced_motion="reduce" if reduced_motion else "no-preference",
            locale="uz-UZ",
        )
        context.add_init_script(TG_STUB)

        def route_api(route):
            url = route.request.url
            if "/auth/telegram" in url or "/auth/login" in url or "/auth/register" in url:
                body = {"access_token": "e2e-token", "user": E2E_USER}
            elif "/auth/me" in url:
                body = E2E_USER
            elif "/files/" in url and "/stream" in url:
                # Pleer `stream_url` ni shu javobdan oladi; usiz "oqimli ijro
                # qilinmaydi" xatosi chiqib, pleer darhol yopiladi.
                body = {"stream_url": "/api/v1/stream/a1", "download_url": "/api/v1/download/a1",
                        "ticket_expires_in": 3600, "mime_type": "audio/wav"}
            elif "/folders" in url:
                body = []                      # ilova to'g'ridan-to'g'ri massiv kutadi
            elif "/files" in url:
                body = FILES_PAYLOAD
            else:
                body = {"items": [], "total": 0, "pages": 1, "page": 1, "counts": {}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        def route_stream(route):
            route.fulfill(status=200, content_type="audio/wav", body=SILENT_WAV)

        context.route("**/api/v1/**", route_api)
        context.route("**/api/v1/stream/**", route_stream)
        # Telegram SDK tashqi CDN'dan keladi va brauzerda "Telegram ichida emas"
        # deb aniqlanadi (`initData` bo'sh) -> ilova login darvozasini ko'rsatadi.
        # Uni bo'sh javob bilan almashtiramiz: shunda yuqoridagi stub kuchda qoladi.
        context.route("**/telegram-web-app.js",
                      lambda r: r.fulfill(status=200, content_type="application/javascript",
                                          body="/* e2e: stub */"))
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/index.html", wait_until="load")
        page.wait_for_timeout(2500)

        check("sahifa xatosiz yuklandi", not errors, "; ".join(errors[:2]))

        # ── "Fayllar" bo'limiga o'tamiz (ilova ro'yxatni shunda yuklaydi) ────
        nav = page.locator('#bottomNav button[data-view="files"]')
        if nav.count():
            nav.first.click()
            page.wait_for_timeout(1800)

        # ── Audio kartani topib, bosamiz (ilovaning o'z playFile yo'li) ──────
        card = page.locator("text=Premium Test Audio").first
        check("audio karta ro'yxatda ko'rindi", card.count() > 0)
        if card.count() == 0:
            browser.close()
            return {"failures": failures, "errors": errors}
        card.click()
        page.wait_for_timeout(1600)

        check("pleer ochildi (#playerBackdrop.is-open)",
              page.evaluate("!!document.querySelector('#playerBackdrop.is-open')"))
        check("audio rejimi (#playerStage.audio-only)",
              page.evaluate("!!document.querySelector('#playerStage.audio-only')"))

        # ── Yangi premium elementlar ────────────────────────────────────────
        check("art o'rami mavjud (#artTurn)",
              page.evaluate("!!document.getElementById('artTurn')"))
        check("ambient qatlam mavjud (#artAmbient)",
              page.evaluate("!!document.getElementById('artAmbient')"))
        check("rasm preview elementi (#artLabel)",
              page.evaluate("!!document.getElementById('artLabel')"))

        state = page.evaluate("""() => {
          const turn = document.getElementById('artTurn');
          const art  = document.getElementById('audioArt');
          const lbl  = document.getElementById('artLabel');
          const amb  = document.getElementById('artAmbient');
          const cs   = (el) => el ? getComputedStyle(el) : null;
          return {
            turnClasses: turn ? [...turn.classList] : [],
            turnAnim: cs(turn) ? cs(turn).animationName : '',
            turnAnimDur: cs(turn) ? cs(turn).animationDuration : '',
            artAnim: cs(art) ? cs(art).animationName : '',
            labelAnim: cs(lbl) ? cs(lbl).animationName : '',
            artPlayingState: cs(art) ? cs(art).animationPlayState : '',
            labelSrc: lbl ? (lbl.getAttribute('src') || '').slice(0, 40) : '',
            hasArt: art ? art.classList.contains('has-art') : false,
            labelVisible: lbl ? cs(lbl).opacity : '',
            ambientBg: amb ? (cs(amb).backgroundImage || '') : '',
            ambientBlur: amb ? cs(amb).filter : '',
            artTurnHidden: turn ? turn.classList.contains('hidden') : true,
          };
        }""")
        print("   holat:", json.dumps(state, ensure_ascii=False)[:400])

        check("rasm preview yuklandi (thumbnail -> img.src)",
              "data:image/svg" in state["labelSrc"], state["labelSrc"][:40])
        check("rasm sinfi qo'llandi (.has-art)", state["hasArt"])
        check("dumaloq yorliq aylanadi (spin)", "spin" in state["labelAnim"],
              state["labelAnim"])
        # Regressiya qo'riqchisi: karta aylanmasligi shart, aks holda pauzada
        # kvadrat qiyshiq qotib qoladi (aynan shu nuqson tuzatilgan edi).
        check("kvadrat karta aylanmaydi (pauzada qiyshiq qotmasin)",
              state["artAnim"] == "none", state["artAnim"])
        check("ambient rasm fonga ega", "url(" in state["ambientBg"],
              state["ambientBg"][-70:])
        check("ambient xiralashtirilgan (blur)", "blur" in state["ambientBlur"], state["ambientBlur"])

        if reduced_motion:
            # Ilovada reduced-motion markazlashgan: `animation-duration: 0.01ms`
            # (nomi qoladi, lekin harakat amalda bajarilmaydi).
            dur = state["turnAnimDur"].rstrip("s") or "0"
            try:
                dur_val = float(dur.split(",")[0])
            except ValueError:
                dur_val = 0.0
            check("reduced-motion: 90° burilish o'chirilgan (davomiylik ~0)",
                  dur_val <= 0.001, state["turnAnimDur"])
        else:
            check("90° burilish animatsiyasi yoqilgan (art-turn-in yoki art-flip)",
                  state["turnAnim"] in ("art-turn-in", "art-flip"), state["turnAnim"])

        OUT.mkdir(parents=True, exist_ok=True)
        shot = OUT / ("reduced.png" if reduced_motion else "player.png")
        page.screenshot(path=str(shot))
        check(f"skrinshot saqlandi ({shot.name})", shot.exists() and shot.stat().st_size > 5000,
              f"{shot.stat().st_size if shot.exists() else 0} bayt")

        browser.close()
    return {"failures": failures, "errors": errors}


if __name__ == "__main__":
    normal = run(reduced_motion=False)
    reduced = run(reduced_motion=True)
    print("\n" + "=" * 60)
    if failures:
        print(f"NATIJA: XATO ({len(failures)} ta): {failures}")
        sys.exit(1)
    print("NATIJA: OK — barcha E2E tekshiruvlari o'tdi")
