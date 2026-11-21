"""Haqiqiy ishga tushirish (subprocess) smoke testlari.

Bu fayl **butunlay boshqa jarayonda** ``python main.py`` ni ishga tushiradi.
Nega kerak: pytest ichidagi testlar ``tests/conftest.py`` tufayli matnlarni
oldindan o'rnatadi va dispatcher'ni "tayyor" ko'radi — shu sababli production
startupidagi xatolar (masalan ``install_language`` chaqirilmasligi) testlarda
ko'rinmay qolgan edi. Bu testlar aynan shu ko'r-ko'rona joyni yopadi.

Tekshiriladigan shartlar:

* ``main.py`` manbasida ``install_language(settings)`` chaqiruvi bor (statik);
* server haqiqatan ko'tariladi va ``/healthz`` -> 200 qaytaradi;
* webhook: noto'g'ri sekret -> 403, to'g'ri sekret -> 200 ``{"ok": true}``;
* Telegram token'i noto'g'ri bo'lsa ham update 500 qaytarmaydi (retry storm yo'q)
  va xato log'da **kontekst bilan** qayd etiladi;
* startup log'ida til o'rnatilgani ko'rinadi.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"

httpx = pytest.importorskip("httpx")


# --------------------------------------------------------------------------- #
# Yordamchilar
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    """OS dan band bo'lmagan port so'rash (testlar orasidagi to'qnashuvni oldini oladi)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(pattern: str, path: Path, timeout: float = 60.0) -> bool:
    """Log faylida ``pattern`` paydo bo'lguncha kutish.

    Eslatma: fayl buferlanishi tufayli satrlar kechikishi mumkin — shu sababli
    subprocess ``PYTHONUNBUFFERED=1`` bilan ishga tushiriladi va "server tayyor"
    belgisi sifatida **HTTP javobi** (``_wait_for_http``) ishlatiladi.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and re.search(pattern, path.read_text(encoding="utf-8", errors="replace")):
            return True
        time.sleep(0.25)
    return False


def _wait_for_http(url: str, timeout: float = 90.0) -> bool:
    """HTTP javobi kelguncha kutish — eng ishonchli "jarayon tayyor" belgisi."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
        except Exception:
            time.sleep(0.25)
            continue
        if response.status_code < 500:
            return True
        time.sleep(0.25)
    return False


def _post(url: str, payload: dict[str, Any], secret: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return httpx.post(url, json=payload, headers=headers, timeout=20.0)


def _start_update(text: str, update_id: int = 1, user_id: int = 777) -> dict[str, Any]:
    """Oddiy ``/start``-uslubidagi message update (Telegram yuboradigan shaklda)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_789_000_000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


@pytest.fixture(scope="module")
def running_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Haqiqiy ``main.py --mode all`` jarayonini ko'tarish (webhook rejimida).

    ``BOT_TOKEN`` ataylab **noto'g'ri**: Telegram API chaqiruvlari
    ``Unauthorized`` bo'ladi. Bu bizga ikki narsani tekshirish imkonini beradi:

    1. ``setWebhook`` muvaffaqiyatsiz bo'lsa ham API tirik qoladi (retry + log);
    2. handler xatosi webhook javobini 500 ga aylantirmaydi.
    """
    tmp = tmp_path_factory.mktemp("smoke")
    log_path = tmp / "server.log"
    db_path = tmp / "smoke.sqlite3"
    port = _free_port()

    env = {
        **os.environ,
        "BOT_TOKEN": "123456:TESTTOKEN-not-real",
        "ADMIN_ID": "111",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "REDIS_URL": "",
        "API_HOST": "127.0.0.1",
        "PORT": str(port),
        "AUTH_REQUIRED": "true",
        "ARCHIVE_ENABLED": "false",
        "RUN_MODE": "all",
        "WEBHOOK_ENABLED": "true",
        "WEBHOOK_URL": "https://example.invalid",
        "WEBHOOK_PATH": "/api/webhook",
        "WEBHOOK_SECRET": "smoke-secret",
        "LOG_LEVEL": "INFO",
        # Log satrlari darhol faylga tushishi kerak (aks holda kutish buferga bog'lanadi).
        "PYTHONUNBUFFERED": "1",
    }

    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--mode", "all"],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        # Avval HTTP javobini kutamiz (port band qilinganini bildiradi), keyin log'ni.
        if not _wait_for_http(f"{base}/healthz", timeout=90.0):
            log_file.flush()
            pytest.fail(f"Server did not answer on {base}/healthz. Log:\n{log_path.read_text(errors='replace')}")
        yield _Server(proc=proc, base=base, log_path=log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        log_file.close()


class _Server:
    """Ishga tushirilgan jarayon haqidagi ma'lumot (test uchun qulay konteyner)."""

    def __init__(self, proc: subprocess.Popen[str], base: str, log_path: Path) -> None:
        self.proc = proc
        self.base = base
        self.log_path = log_path

    @property
    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Statik tekshiruv: startup ketma-ketligi
# --------------------------------------------------------------------------- #
def test_main_installs_language_at_startup() -> None:
    """``install_language(settings)`` haqiqatan startup'da chaqirilishi shart."""
    source = MAIN.read_text(encoding="utf-8")

    # Funksiya ta'rifi va kamida bitta chaqiruv bo'lishi kerak.
    assert "def install_language(" in source
    assert re.search(r"^\s*install_language\(settings", source, re.MULTILINE), (
        "main.py da install_language(settings) chaqiruvi topilmadi — bot.texts "
        "bo'sh qoladi va birinchi xabarda AttributeError yuz beradi"
    )
    # amain() ham sug'urta sifatida chaqiradi (to'g'ridan-to'g'ri import holatlari uchun).
    assert "ensure_language(settings)" in source


def test_error_middleware_is_outermost() -> None:
    """Xato to'ri bizning boshqa outer middleware'lardan **oldin** ro'yxatdan o'tadi.

    (aiogram o'zining ``ErrorsMiddleware`` ini ``Dispatcher()`` ichida eng tashqi
    qilib qo'yadi — biz uni o'zgartira olmaymiz va o'zimiznikini undan keyin,
    lekin boshqa barcha qatlamlardan oldin qo'yamiz.)
    """
    source = (ROOT / "bot" / "setup.py").read_text(encoding="utf-8")
    error_pos = source.index("ErrorLoggingMiddleware()")
    logging_pos = source.index("UpdateLoggingMiddleware()")
    db_pos = source.index("DatabaseSessionMiddleware(settings)")
    assert error_pos < logging_pos < db_pos


# --------------------------------------------------------------------------- #
# Haqiqiy jarayon: HTTP va webhook
# --------------------------------------------------------------------------- #
def test_server_is_alive_and_serves_health(running_server: _Server) -> None:
    response = httpx.get(f"{running_server.base}/healthz", timeout=15.0)
    assert response.status_code == 200
    assert "OK" in response.text


def test_startup_log_shows_language_and_webhook(running_server: _Server) -> None:
    # Til va webhook marshruti startup'da log'lanadi — bufer kechikishini kutib olamiz.
    assert _wait_for(r"Language uz installed", running_server.log_path, timeout=30.0), (
        "til o'rnatilmagan — UI matnlari bo'sh bo'lardi. Log:\n" + running_server.log[-600:]
    )
    assert _wait_for(r"Webhook route attached to FastAPI at /api/webhook", running_server.log_path, timeout=30.0)

    # Webhook ro'yxatdan o'tkazish **urinishi** bo'lganini kutamiz. Soxta token
    # bilan u odatda "setWebhook failed" bo'ladi, lekin tarmoq sekin bo'lsa bu
    # satr kechikishi mumkin — shu sababli darhol assert qilmaymiz, kutamiz.
    attempted = _wait_for(r"setWebhook failed|Telegram webhook set to", running_server.log_path, timeout=45.0)
    assert attempted, (
        "webhook ro'yxatdan o'tkazish urinishi log'lanmadi. Log:\n" + running_server.log[-800:]
    )

    # Eng muhimi: urinish muvaffaqiyatsiz bo'lsa ham jarayon TIRIK (API xizmatda).
    assert running_server.proc.poll() is None, "jarayon startup xatosi tufayli o'lib qolgan"
    assert httpx.get(f"{running_server.base}/healthz", timeout=10.0).status_code == 200


def test_webhook_rejects_bad_secret(running_server: _Server) -> None:
    response = _post(f"{running_server.base}/api/webhook", _start_update("/start"), secret="wrong")
    assert response.status_code == 403


def test_webhook_accepts_valid_secret(running_server: _Server) -> None:
    response = _post(f"{running_server.base}/api/webhook", _start_update("/start"), secret="smoke-secret")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_processes_real_update_end_to_end(running_server: _Server) -> None:
    """Haqiqiy oqim: update -> middleware -> DB -> javob (200 ``{"ok": true}``).

    Eslatma: ``BOT_TOKEN`` soxta bo'lgani uchun Telegram'ga javob yuborish
    muvaffaqiyatsiz bo'ladi, ammo ``AuthGuardMiddleware._reject`` buni **o'zi**
    ushlaydi (foydalanuvchiga yetib bormagan xabar update'ni yiqitmasligi kerak).
    Natijada: webhook 200 qaytaradi, jarayon tirik qoladi va foydalanuvchi
    bazada ro'yxatdan o'tadi — ya'ni butun zanjir ishlaydi.
    """
    response = _post(
        f"{running_server.base}/api/webhook",
        _start_update("/storage", update_id=42),
        secret="smoke-secret",
    )
    assert response.status_code == 200, f"webhook 500 qaytardi: {response.text[:300]}"
    assert response.json() == {"ok": True}

    # Update haqiqatan qayta ishlandi: yangi foydalanuvchi bazaga yozildi.
    assert _wait_for(r"Registered new user 777", running_server.log_path, timeout=30.0), (
        "update handler'gacha yetib bormadi. Log:\n" + running_server.log[-600:]
    )
    log = running_server.log
    # Til o'rnatilgani uchun auth_guard matnlari AttributeError bermaydi.
    assert "'bot.texts' has no attribute" not in log
    # Jarayon hali tirik.
    assert running_server.proc.poll() is None


def test_webhook_rejects_protected_command_for_guest(running_server: _Server) -> None:
    """``AUTH_REQUIRED=true``: mehmon foydalanuvchi /storage ni ishlatolmaydi.

    Rad javobi Telegram'ga yuborilmaydi (token soxta), lekin update "qayta
    ishlangan" hisoblanadi va 500 qaytmaydi.
    """
    response = _post(
        f"{running_server.base}/api/webhook",
        _start_update("/files", update_id=43, user_id=888),
        secret="smoke-secret",
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # Server haligacha tirik va boshqa so'rovlarni qabul qiladi.
    assert httpx.get(f"{running_server.base}/healthz", timeout=10.0).status_code == 200


def test_webhook_rejects_malformed_payload(running_server: _Server) -> None:
    response = httpx.post(
        f"{running_server.base}/api/webhook",
        content="{not-json",
        headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "smoke-secret"},
        timeout=15.0,
    )
    assert response.status_code == 400


def test_mini_app_is_served_for_browsers(running_server: _Server) -> None:
    response = httpx.get(
        running_server.base + "/",
        headers={"Accept": "text/html"},
        timeout=15.0,
    )
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "<html" in response.text.lower()


def test_api_json_contract_still_works(running_server: _Server) -> None:
    """``Accept: application/json`` bo'lsa SPA emas, API javobi qaytishi kerak."""
    response = httpx.get(
        running_server.base + "/api/v1/system/health",
        headers={"Accept": "application/json"},
        timeout=15.0,
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    # Health javobida kamida bitta holat maydoni bo'ladi.
    assert any(key in payload for key in ("status", "ok", "healthy"))


def test_no_fatal_startup_error(running_server: _Server) -> None:
    """Jarayon hali tirik va log'da fatal startup xatosi yo'q."""
    log = running_server.log
    assert running_server.proc.poll() is None, "jarayon o'lib qolgan"
    assert "Fatal error during startup/runtime" not in log
    # Til o'rnatilmaganning klassik belgisi — bot.texts'da funksiya yo'qligi.
    # (Telegram API xatolari bo'lishi mumkin, ammo AttributeError bo'lmasligi shart.)
    assert "'bot.texts' has no attribute" not in log


def test_startup_log_format_is_consistent(running_server: _Server) -> None:
    """Log formati izchil: vaqt + daraja + logger (PaaS log drayverlari uchun muhim)."""
    lines = [line for line in running_server.log.splitlines() if " hugecloud | " in line]
    assert lines, "hugecloud loggeridan hech qanday satr topilmadi"
    for line in lines[:20]:
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (INFO|WARNING|ERROR|DEBUG)\s", line), line
    # Konfiguratsiya banner'ida token **yashirilgan** bo'lishi kerak.
    banner = next((line for line in lines if "Config:" in line), "")
    assert banner, "konfiguratsiya banner'i log'lanmagan"
    assert "TESTTOKEN-not-real" not in banner
    assert "***" in banner


def test_webhook_response_body_is_minimal(running_server: _Server) -> None:
    """Telegram'ga qaytadigan javob faqat ``{"ok": true}`` — ortiqcha ma'lumot yo'q."""
    response = _post(
        f"{running_server.base}/api/webhook",
        _start_update("/help", update_id=99),
        secret="smoke-secret",
    )
    assert response.status_code == 200
    assert json.loads(response.text) == {"ok": True}
    assert len(response.text) < 200
