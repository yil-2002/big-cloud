"""MTProto katta-fayl oqimi testlari (tarmoqqa chiqmaydi — hammasi mock).

Nega alohida: 20 MB devori faqat **Bot API** (``getFile``) uchun; MTProto orqali
bot 2 GB gacha yuklab oladi. Route shu sababli ``MT_MEDIA_ENABLED=true`` +
``archive_message_id`` bo'lganda Telegram o'rniga MTProto'dan ``offset/limit``
bo'yicha bo'laklab o'qiydi. Bu testlar aynan shu shartnomani qo'riqlaydi:
Range -> offset/limit, 206 + ``Content-Range``, va MTProto o'chiq/ulanmagan
holatda eski xulq (413 / 503).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.share import ShareService
from tests.conftest import make_file
from tests.test_stream import stream_client  # noqa: F401  (fixture shu yerdan olinadi)


@pytest.fixture()
def share(storage, settings) -> ShareService:
    """Stream ticketlarini beruvchi xizmat (haqiqiy imzo, soxta baza)."""
    return ShareService(storage.session, settings=settings)


def _enable_mt(settings) -> None:
    settings.mt.enabled = True
    settings.mt.api_id = 123
    settings.mt.api_hash = "abc"
    settings.mt.chunk_size = 1024 * 1024


async def _big_file(storage, user, name: str, *, archive_message_id: int | None):
    return await make_file(
        storage,
        user.telegram_id,
        name=name,
        size=30 * 1024 * 1024,          # 20 MB devoridan katta
        archive_message_id=archive_message_id,
    )


@pytest.mark.asyncio
async def test_mtproto_range_maps_to_offset_and_limit(stream_client, share, settings, storage, user):
    """``Range: bytes=100-199`` -> ``iter_range(offset=100, limit=100)`` + 206."""
    record = await _big_file(storage, user, "large.mp4", archive_message_id=42)
    _enable_mt(settings)
    token = share.issue_stream_ticket(record.id, user.telegram_id).token
    size = int(record.file_size)

    def range_body(_client, _chat_id, _message_id, _offset, _limit, chunk=None):
        del chunk

        async def gen():
            yield b"x" * 100

        return gen()

    with patch("services.mtproto.get_client", new=AsyncMock(return_value=MagicMock())) as get_client, \
         patch("services.mtproto.iter_range", new=MagicMock(side_effect=range_body)) as iter_range:
        client = stream_client()

        explicit = client.get(f"/api/v1/stream/{token}", headers={"Range": "bytes=100-199"})
        assert explicit.status_code == 206
        assert explicit.headers["Content-Range"] == f"bytes 100-199/{size}"
        assert explicit.headers["Content-Length"] == "100"
        assert iter_range.call_args.args[3] == 100        # offset
        assert iter_range.call_args.args[4] == 100        # limit
        # Eslatma: ``get_client`` startup'da ham chaqiriladi (warm-up), shuning
        # uchun muhim shartnoma — ``iter_range`` chaqiruvlari.
        assert iter_range.call_count == 1

        open_ended = client.get(f"/api/v1/stream/{token}", headers={"Range": "bytes=1000-"})
        assert open_ended.status_code == 206
        assert open_ended.headers["Content-Range"] == f"bytes 1000-{size - 1}/{size}"
        assert iter_range.call_args.args[3] == 1000
        assert iter_range.call_args.args[4] == size - 1000


@pytest.mark.asyncio
async def test_mtproto_disabled_or_unarchived_still_returns_413(
    stream_client, share, settings, storage, user
):
    """MTProto yo'q bo'lsa — eski 413 shartnomasi saqlanadi."""
    _enable_mt(settings)
    client = stream_client()

    no_archive = await _big_file(storage, user, "no-archive.mp4", archive_message_id=None)
    token = share.issue_stream_ticket(no_archive.id, user.telegram_id).token
    response = client.get(f"/api/v1/stream/{token}")
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large_for_stream"

    archived = await _big_file(storage, user, "archived.mp4", archive_message_id=42)
    token = share.issue_stream_ticket(archived.id, user.telegram_id).token
    settings.mt.enabled = False
    response = client.get(f"/api/v1/stream/{token}")
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_mtproto_client_unavailable_returns_503(stream_client, share, settings, storage, user):
    """Klient ko'tarilmasa — aniq 503 ``mtproto_unavailable`` (jim yiqilish emas)."""
    record = await _big_file(storage, user, "offline.mp4", archive_message_id=42)
    _enable_mt(settings)
    token = share.issue_stream_ticket(record.id, user.telegram_id).token

    with patch("services.mtproto.get_client", new=AsyncMock(return_value=None)):
        client = stream_client()
        response = client.get(f"/api/v1/stream/{token}")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "mtproto_unavailable"


@pytest.mark.asyncio
async def test_small_file_keeps_bot_api_path(stream_client, share, settings, storage, user):
    """Kichik fayl MTProto'ga umuman tegmaydi (eski yo'l o'zgarmagan)."""
    record = await make_file(storage, user.telegram_id, name="small.mp4", size=1024 * 1024)
    _enable_mt(settings)
    token = share.issue_stream_ticket(record.id, user.telegram_id).token

    with patch("services.mtproto.iter_range", new=MagicMock()) as iter_range:
        client = stream_client()
        response = client.get(f"/api/v1/stream/{token}")
    assert response.status_code == 200
    # Kichik fayl MTProto'ga umuman tegmaydi (iter_range chaqirilmaydi).
    assert iter_range.call_count == 0


class _FakeDownloadClient:
    """Har chaqiruvda 1 MB'lik bo'laklar beradigan soxta Telethon klienti."""

    def __init__(self, total_chunks: int = 10) -> None:
        self.total_chunks = total_chunks
        self.calls: list[dict] = []

    async def get_messages(self, _chat_id, ids=None):  # noqa: ARG002
        class _Msg:
            media = object()
        return _Msg()

    def iter_download(self, _media, *, offset=0, request_size=None):  # noqa: ARG002
        self.calls.append({"offset": offset, "request_size": request_size})

        async def gen():
            for _ in range(self.total_chunks):
                yield b"y" * (1024 * 1024)

        return gen()


@pytest.mark.asyncio
async def test_iter_range_never_exceeds_limit_bytes():
    """Regressiya: Telethon ``limit`` bo'lak soni — chegarani biz hisoblaymiz.

    Ilgari ``iter_range(limit=1 MiB)`` butun faylni oqizib yuborardi
    (45 s da 85 MB), ya'ni Range so'rovi HTTP shartnomasini buzardi.
    """
    from services.mtproto import iter_range

    client = _FakeDownloadClient(total_chunks=10)
    pieces = [p async for p in iter_range(client, -100, 26, 0, 2500 * 1024, chunk=1024 * 1024)]
    assert sum(len(p) for p in pieces) == 2500 * 1024
    assert len(pieces) == 3                      # 2 to'liq + 1 kesilgan bo'lak
    assert len(pieces[-1]) == 2500 * 1024 - 2 * 1024 * 1024
    assert client.calls[0]["offset"] == 0
