"""🧱 20 MB devori — Bot API ``getFile`` cheklovi uchun oldindan tekshiruv.

Rasmiy Bot API botga faqat ~20 MB gacha faylni yuklab beradi (yuborish 2 GB
bo'lsa ham). Undan katta fayl uchun ``/stream`` proksi baribir ishlamaydi —
shuning uchun server Telegram'ga murojaat qilmasdan **413** +
``{"code": "file_too_large_for_stream"}`` qaytaradi, Mini App esa foydalanuvchini
"Telegram'da ochish" (``btnNative``) yo'liga yo'naltiradi.

Testlar uch qatlamni qamraydi:

1. ``GET /stream/{ticket}`` — devor ticket bosqichidan keyin, Telegram'dan oldin;
2. ``GET /files/{id}/stream`` — chipta berish bosqichida ham xuddi shu devor
   (Mini App kutilmagan xato o'rniga darhol aniq javob oladi);
3. frontend shartnomasi — xato kodi lug'atda, ``btnNative`` mavjud, pleer va
   yuklab olish oqimlari devorni serverdan oldin tekshiradi.
"""

# ``stream_client`` fixture'i ``tests.test_stream`` dan import qilinadi; pytest
# uni parametr nomi orqali topadi — ruff buni "qayta ta'rif" deb o'ylaydi.
# ruff: noqa: F811

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.share import ShareService
from tests.conftest import make_file
from tests.test_stream import MEDIA_BYTES, stream_client  # noqa: F401 - fixture importi

ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "web" / "index.html"

WALL = 20 * 1024 * 1024  # 20 MB — sukut bo'yicha STREAM_MAX_SIZE


@pytest.fixture()
def share(storage, settings) -> ShareService:
    return ShareService(storage.session, settings=settings)


def _web() -> str:
    return WEB_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Konfiguratsiya
# --------------------------------------------------------------------------- #
def test_stream_max_size_default_is_20mb(settings) -> None:
    """Sukut qiymat aynan Bot API devori: 20 * 1024 * 1024."""
    assert settings.storage.stream_max_size == WALL


def test_stream_max_size_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``STREAM_MAX_SIZE`` muhitdan boshqariladi (0 = devor o'chirilgan)."""
    monkeypatch.setenv("STREAM_MAX_SIZE", "0")
    from config import StorageSettings

    assert StorageSettings().stream_max_size == 0


# --------------------------------------------------------------------------- #
# 2. ``GET /stream/{ticket}`` devori
# --------------------------------------------------------------------------- #
async def test_stream_rejects_file_over_20mb(stream_client, share, storage, user) -> None:
    """Katta fayl → 413 + ``file_too_large_for_stream`` (Telegram'ga so'rovsiz)."""
    record = await make_file(
        storage, user.telegram_id, name="kino.mp4", size=WALL + 1, category="video"
    )
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "file_too_large_for_stream"
    assert detail["size"] == WALL + 1
    assert detail["limit"] == WALL


async def test_stream_wall_applies_to_head_probe(stream_client, share, storage, user) -> None:
    """Pleer avval ``HEAD`` yuboradi — devor u yerda ham ishlashi shart."""
    record = await make_file(storage, user.telegram_id, name="kino.mp4", size=WALL + 1)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    assert client.head(f"/api/v1/stream/{ticket.token}").status_code == 413


async def test_stream_allows_file_at_exact_limit(stream_client, share, storage, user) -> None:
    """Chegara **qat'iy katta** uchun: ``size == limit`` hali ham oqadi."""
    record = await make_file(storage, user.telegram_id, name="mikro.mp4", size=WALL)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 200, response.text
    assert response.content == MEDIA_BYTES


async def test_stream_allows_unknown_size(stream_client, share, storage, user) -> None:
    """Hajmi noma'lum (0) eski yozuvlar bloklanmaydi — Telegram o'zi hal qiladi."""
    record = await make_file(storage, user.telegram_id, name="eski.bin", size=0)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200


async def test_wall_disabled_when_limit_is_zero(
    stream_client, share, storage, user, settings
) -> None:
    """``STREAM_MAX_SIZE=0`` → devor yo'q (masalan, lokal Bot API server)."""
    record = await make_file(storage, user.telegram_id, name="katta.mp4", size=WALL * 10)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    original = settings.storage.stream_max_size
    settings.storage.stream_max_size = 0
    try:
        client = stream_client()
        assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200
    finally:
        settings.storage.stream_max_size = original


# --------------------------------------------------------------------------- #
# 3. ``GET /files/{id}/stream`` — chipta bosqichidagi devor
# --------------------------------------------------------------------------- #
async def test_ticket_endpoint_rejects_large_file(
    stream_client, share, storage, user, settings, init_data
) -> None:
    """Mini App chipta so'raganda ham 413 — kutish/aylanish yo'q."""
    record = await make_file(storage, user.telegram_id, name="kino.mp4", size=WALL + 1)
    del share  # chipta berilmasligining o'zi tekshiruv predmeti

    client = stream_client()
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    token_response = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    )
    if token_response.status_code == 200:
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "file_too_large_for_stream"


async def test_ticket_endpoint_allows_small_file(
    stream_client, share, storage, user, settings, init_data
) -> None:
    """Kichik fayl uchun chipta odatdagidek beriladi (regressiya himoyasi)."""
    record = await make_file(storage, user.telegram_id, name="qoshiq.mp3", size=5 * 1024 * 1024)
    del share

    client = stream_client()
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    token_response = client.post(
        "/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)}
    )
    if token_response.status_code == 200:
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["stream_url"]


# --------------------------------------------------------------------------- #
# 4. Frontend shartnomasi (statik — build bosqichi yo'q)
# --------------------------------------------------------------------------- #
def test_frontend_localizes_wall_error() -> None:
    """413 kodi uchun o'zbekcha matn lug'atda bor."""
    html = _web()
    assert "file_too_large_for_stream" in html
    match = re.search(r'file_too_large_for_stream: "([^"]+)"', html)
    assert match, "ERROR_MESSAGES'da tarjima yo'q"
    message = match.group(1)
    assert message.strip(), "matn bo'sh bo'lmasin"
    # Foydalanuvchi matnida texnik tafsilotlar (Telegram/Bot API/file_id) bo'lmasin.
    for forbidden in ("Telegram", "Bot API", "file_id"):
        assert forbidden not in message, f"matnda '{forbidden}' qolgan"


def test_frontend_has_btn_native() -> None:
    """`btnNative` — "Telegram'da ochish" tugmasi taklif oynasida yaratiladi."""
    html = _web()
    assert 'id: "btnNative"' in html
    assert "Ilovada ochish" in html
    assert "openTelegramLink" in html


def test_player_checks_wall_before_requesting_ticket() -> None:
    """`playFile` chipta so'rashdan **oldin** hajmni tekshiradi."""
    html = _web()
    play = html[html.index("async function playFile") : html.index("function closePlayer")]
    assert "isTooLargeForStream(file)" in play
    assert play.index("isTooLargeForStream") < play.index("ensureStream"), (
        "devor tekshiruvi serverga murojaatdan oldin bo'lishi kerak"
    )
    # Server 413 qaytarsa ham xuddi shu taklif ko'rsatiladi (zaxira yo'l).
    assert 'error.code === "file_too_large_for_stream"' in play


def test_download_flow_checks_wall() -> None:
    """`downloadFile` ham devorni yoqadi: katta faylga havola so'ralmaydi."""
    html = _web()
    download = html[
        html.index("async function downloadFile") : html.index("async function playFile")
    ]
    assert "isTooLargeForStream(file)" in download
    assert 'error.code === "file_too_large_for_stream"' in download


def test_native_link_uses_file_deep_link_grammar() -> None:
    """Deep-link bot bilan bir xil grammatikada: ``?start=file_<uuid.hex>``."""
    html = _web()
    assert '"?start=file_" + hex' in html
    # Bot tomonidagi qabul qiluvchi (bot/handlers/start.py) shu prefiksni kutadi.
    start_py = (ROOT / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
    assert '_PREFIX_FILE = "file_"' in start_py


def test_config_pre_blocks_only_above_telegram_ceiling() -> None:
    """Mijoz faqat 2 GB dan yuqorisini oldindan to'xtatadi; qolganini server hal qiladi.

    Ilgari bu yerda 20 MB turardi va MTProto yoqilgan bo'lsa ham Mini App katta
    faylni ochishdan bosh tortardi (jonli xato, 2026-09-23).
    """
    html = _web()
    assert re.search(r"streamMaxSize:\s*2147483648", html), "CONFIG.streamMaxSize = 2 GiB emas"

# --------------------------------------------------------------------------- #
# 5. Avval o'ynasin - keyin arxivlansin
# --------------------------------------------------------------------------- #
from unittest.mock import AsyncMock

from aiogram.types import Document, Message


async def test_ticket_endpoint_archives_on_the_fly_and_returns_200(
    stream_client, share, storage, user, settings, init_data, monkeypatch
) -> None:
    # 1. Arxivlanmagan >20 MB fayl + MT_MEDIA_ENABLED + arxiv sozlangan
    record = await make_file(storage, user.telegram_id, name="katta.mp4", size=WALL + 1)
    
    settings.mt.enabled = True
    settings.mt.api_id = 123
    settings.mt.api_hash = "abc"
    settings.archive.channel_id = "-100123"
    
    from aiogram.types import Chat
    bot_mock = AsyncMock()
    bot_mock.send_document.return_value = Message(
        message_id=42,
        date=123,
        chat=Chat(id=-100123, type="channel"),
        document=Document(file_id="channel_fid", file_unique_id="channel_uid")
    )
    
    client = stream_client()
    client.app.state.bot = bot_mock
    
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    token_response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)})
    if token_response.status_code == 200:
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    # First request
    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 200, response.text
    assert bot_mock.send_document.call_count == 1
    
    record = await storage.get_file(record.id, user.telegram_id)
    await storage.session.refresh(record)
    assert record.archive_message_id == 42
    
    # 2. Second request -> send_document is not called again
    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 200
    assert bot_mock.send_document.call_count == 1

async def test_ticket_endpoint_archive_failure_returns_413(
    stream_client, share, storage, user, settings, init_data, monkeypatch
) -> None:
    # 3. send_document fails -> 413 and archive_message_id remains None
    record = await make_file(storage, user.telegram_id, name="katta2.mp4", size=WALL + 1)
    
    settings.mt.enabled = True
    settings.mt.api_id = 123
    settings.mt.api_hash = "abc"
    settings.archive.channel_id = "-100123"
    
    bot_mock = AsyncMock()
    bot_mock.send_document.side_effect = Exception("Network error")
    
    client = stream_client()
    client.app.state.bot = bot_mock
    
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    token_response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)})
    if token_response.status_code == 200:
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large_for_stream"
    
    record = await storage.get_file(record.id, user.telegram_id)
    await storage.session.refresh(record)
    assert record.archive_message_id is None

async def test_ticket_endpoint_small_file_skips_archive(
    stream_client, share, storage, user, settings, init_data, monkeypatch
) -> None:
    # 4. Small file -> archiving skipped
    record = await make_file(storage, user.telegram_id, name="kichik.mp4", size=WALL - 1)
    
    settings.mt.enabled = True
    settings.mt.api_id = 123
    settings.mt.api_hash = "abc"
    settings.archive.channel_id = "-100123"
    
    bot_mock = AsyncMock()
    
    client = stream_client()
    client.app.state.bot = bot_mock
    
    headers = {"X-Telegram-Init-Data": init_data(user.telegram_id)}
    token_response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(user.telegram_id)})
    if token_response.status_code == 200:
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

    response = client.get(f"/api/v1/files/{record.id}/stream", headers=headers)
    assert response.status_code == 200
    assert bot_mock.send_document.call_count == 0

